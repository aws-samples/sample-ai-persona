"""
SurveyExecutionManager
アンケート実行制御を担当するマネージャークラス。

v1.0.0 U7 (``functional-design/rules.md``):

- BR2.1: starting a survey checks, in order, ``ai:execute``, that surveys
  can run here (a state machine is configured, BR9.1), the template's
  ``ensure_readable`` and the persona data (Nemotron downloaded, or custom
  data the user may use); then the count (10 or more, BR1.1〜BR1.3) and the
  size (350KB). Nothing is written unless every check passes.
- BR1.1 / BR2.7: the runner owns the survey; it starts unshared and carries a
  snapshot of the template (name, questions, image references) and the
  custom data's name.
- FR3.1: an accepted survey is saved ``queued``; ``SurveyRunManager`` (the
  survey runner) starts it on Step Functions when it is first in line.
- BR8.1: a ``running`` / ``pending`` survey from before the switch (no
  execution, no queue entry) is turned into ``SURVEY_EXECUTION_INTERRUPTED``
  when a list or the detail reads it.
- BR1.2 / BR1.3: lists are tabbed; detail, download and images need read.
- BR2.8: result images are only the snapshot's, signed after the read check.
- BR1.8 / BR7: delete removes the body and its share items, stops a running
  execution (a failure is logged), removes the run location, then the CSV
  (three tries, then ``survey.cleanup_failed``) and the cached CSV.

Logs: ``survey.run_stop_failed`` and ``survey.cleanup_failed`` (IDs and codes
only, NFR3.13).
"""

import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol

from ..config import config
from ..models.authz import Permission, Principal, ResourceType
from ..models.custom_persona_data import PersonaDataRef
from ..models.errors import CodedError, ErrorCode, LookupUnavailableError
from ..models.sharing import ListTab, TabbedList
from ..models.survey import Survey, SurveyDetail, SurveyStatus, TemplateSnapshot
from ..services.stores.custom_persona_data_store import CustomPersonaDataStore
from ..services.s3_service import S3Service
from ..services.service_factory import service_factory
from ..services.survey_batch_service import SurveyBatchService
from ..services.stores.survey_store import SurveyStore, SurveyStoreError
from ..services.stores.survey_template_store import (
    SurveyTemplateStore,
    SurveyTemplateStoreError,
)
from ..models.asset_delivery import AssetHandle, DeliveryResult, SizeLimit
from .components.access_policy import AccessPolicy
from .components.asset_delivery import AssetDelivery
from .components.owner_directory import OwnerDirectory
from .components.resource_lookup import ResourceLookup
from .components.resource_sharing import SEARCH_LIMIT, ResourceSharing
from .components.sharing_runtime import access_parts_from, sharing_parts_from
from .components.survey_persona_source import MIN_SURVEY_PERSONAS, SurveyPersonaSource
from .shared.cleanup import run_cleanup
from .shared.structured_log import log_event
from .shared.result_csv_cache import shared_result_cache

logger = logging.getLogger(__name__)

_SURVEY = ResourceType.SURVEY
_TEMPLATE = ResourceType.SURVEY_TEMPLATE
#: Largest survey body written, snapshot and report included (NFR2.25).
MAX_SURVEY_BYTES = 350 * 1024
#: Owner label of a survey without owner (before v1.0.0).
OWNER_UNKNOWN = "所有者不明"
#: Most rows a search returns.
SEARCH_RESULT_LIMIT = 100
#: Lifetimes of the presigned URLs of a result CSV and a snapshot image
#: (unchanged from before closed network U4).
RESULT_CSV_URL_EXPIRATION_SECONDS = 300
RESULT_IMAGE_URL_EXPIRATION_SECONDS = 3600
#: Types a snapshot image may be served as. The type in the snapshot came from
#: the template form, so anything else is sent as opaque bytes (U4 review).
_RESULT_IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg"})


class SurveyExecutionManagerError(CodedError):
    """SurveyExecutionManager層の基底例外"""

    pass


class SurveyExecutionValidationError(SurveyExecutionManagerError):
    """バリデーションエラー"""

    pass


class SurveyExecutionError(SurveyExecutionManagerError):
    """アンケート実行エラー"""

    pass


class ResultCache(Protocol):
    """What deleting needs from ``ResultCsvCache`` (shared with the analysis)."""

    def evict(self, survey_id: str) -> None: ...


@dataclass(frozen=True)
class SurveyStart:
    """A survey that passed every start check (BR2.1).

    Attributes:
        survey: The saved survey (``queued``). The caller wakes the survey
            runner, which starts it when it is first in line (WF1).
    """

    survey: Survey


class SurveyExecutionManager:
    """アンケート実行制御"""

    #: アンケートの最小人数（FR4.1、FR4.3）。希望数だけでなく、作成時と開始時に
    #: 「実際に集まる人数」もこの値で確かめる（BR1.2、BR1.4）。
    MIN_SURVEY_PERSONAS = MIN_SURVEY_PERSONAS

    def __init__(
        self,
        survey_store: Optional[SurveyStore] = None,
        survey_template_store: Optional[SurveyTemplateStore] = None,
        survey_batch_service: Optional[SurveyBatchService] = None,
        s3_service: Optional[S3Service] = None,
        resource_sharing: Optional[ResourceSharing] = None,
        owners: Optional[OwnerDirectory] = None,
        resource_lookup: Optional[ResourceLookup] = None,
        custom_persona_data_store: Optional[CustomPersonaDataStore] = None,
        result_cache: Optional[ResultCache] = None,
        sleep: Callable[[float], None] = time.sleep,
        asset_delivery: Optional[AssetDelivery] = None,
        runner: Optional[Any] = None,
        runner_configured: Callable[[], bool] = lambda: config.survey_runner_configured,
    ) -> None:
        """Create the manager.

        Args:
            survey_store: Survey bodies.
            survey_template_store: Template bodies (copied into new surveys).
            survey_batch_service: Persona sampling (DuckDB).
            s3_service: Result CSVs, images and custom data files.
            resource_sharing: The per-task ``ResourceSharing`` (tabs, delete).
            owners: The owner-name copy (display only).
            resource_lookup: The per-task ``ResourceLookup`` (decisions).
            custom_persona_data_store: Custom persona data records.
            result_cache: The per-task cache of result CSVs (evicted on delete).
            sleep: Wait between cleanup attempts (tests).
            asset_delivery: Serves result CSVs and images (closed network U4;
                built from ``s3_service`` and ``Config.ASSET_DELIVERY`` when
                omitted).
            runner: ``SurveyRunnerService`` (stops an execution on delete;
                from ``service_factory`` when omitted and configured).
            runner_configured: Whether surveys can run here (BR9.1).
        """
        self._surveys = survey_store or service_factory.get_survey_store()
        self._templates = (
            survey_template_store or service_factory.get_survey_template_store()
        )
        self.batch_service = (
            survey_batch_service or service_factory.get_survey_batch_service()
        )
        self.s3_service: S3Service = s3_service or service_factory.get_s3_service()
        self._sharing = resource_sharing
        self._owner_directory = owners
        self._lookup = resource_lookup
        self._custom_store = custom_persona_data_store
        self._cache = result_cache
        self._sleep = sleep
        self._delivery = asset_delivery
        self._runner = runner
        self._runner_configured = runner_configured
        self._persona_source = SurveyPersonaSource(self.batch_service, self.s3_service)

    # --- parts (built lazily) -------------------------------------------------------

    @property
    def _survey_runner(self) -> Optional[Any]:
        """The Step Functions runner, or ``None`` where surveys do not run."""
        if self._runner is None and self._runner_configured():
            self._runner = service_factory.get_survey_runner_service()
        return self._runner

    @property
    def _asset_delivery(self) -> AssetDelivery:
        if self._delivery is not None:
            return self._delivery
        return AssetDelivery(self.s3_service, config.ASSET_DELIVERY)

    @property
    def _resource_sharing(self) -> ResourceSharing:
        if self._sharing is None:
            self._sharing = sharing_parts_from(service_factory).sharing
        return self._sharing

    @property
    def _owners(self) -> OwnerDirectory:
        if self._owner_directory is None:
            self._owner_directory = sharing_parts_from(service_factory).owners
        return self._owner_directory

    @property
    def _resource_lookup(self) -> ResourceLookup:
        if self._lookup is None:
            self._lookup = access_parts_from(service_factory).lookup
        return self._lookup

    @property
    def result_cache(self) -> ResultCache:
        """The per-process result CSV cache (shared with the analysis, BR1.8)."""
        if self._cache is None:
            self._cache = shared_result_cache()
        return self._cache

    @property
    def _custom_persona_data(self) -> CustomPersonaDataStore:
        if self._custom_store is None:
            self._custom_store = service_factory.get_custom_persona_data_store()
        return self._custom_store

    # =========================================================================
    # 開始（BR2.1、BR1.1、BR2.7）
    # =========================================================================

    def create_survey(
        self,
        principal: Principal,
        template_id: str,
        name: Optional[str] = None,
        description: str = "",
        persona_count: int = 100,
        filters: Optional[Dict[str, Any]] = None,
        datasource: Optional[str] = None,
    ) -> SurveyStart:
        """Check every start condition, then save a ``queued`` survey (BR2.1, WF1).

        Returns:
            The saved survey; the caller wakes the survey runner.

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            CodedError: ``SURVEY_RUNNER_NOT_CONFIGURED``,
                ``SURVEY_TEMPLATE_NOT_FOUND``,
                ``CUSTOM_PERSONA_DATA_NOT_FOUND``, ``LOOKUP_UNAVAILABLE``,
                ``SURVEY_DATASET_NOT_DOWNLOADED``, count validation codes,
                ``SURVEY_TEMPLATE_TOO_LARGE`` or ``SURVEY_OPERATION_FAILED``.
        """
        AccessPolicy.require(principal, Permission.AI_EXECUTE, resource_type=_SURVEY)
        if not self._runner_configured():
            raise SurveyExecutionError(
                "no survey state machine is configured",
                code=ErrorCode.SURVEY_RUNNER_NOT_CONFIGURED,
            )
        template_target = self._resource_lookup.get(_TEMPLATE, template_id)
        AccessPolicy.ensure_readable(
            principal, template_target, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
        )
        ref, datasource_name = self._usable_datasource(principal, datasource)
        template = self._read_template(template_id)
        if template is None:
            raise SurveyExecutionManagerError(
                "survey template is gone", code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
            )
        self._validate_persona_count(persona_count, bool(template.images))
        self._validate_available_personas(filters, ref.value)
        if not name or not name.strip():
            name = self.generate_default_survey_name(template.name)
        survey = Survey.create_new(
            name=name,
            description=description,
            template_id=template_id,
            persona_count=persona_count,
            filters=filters,
            datasource=ref.value,
            owner_sub=principal.sub,
            template_snapshot=TemplateSnapshot.from_template(template),
            datasource_name=datasource_name,
        )
        if self._surveys.item_size(survey.to_dict()) > MAX_SURVEY_BYTES:
            raise SurveyExecutionValidationError(
                "the survey body exceeds 350KB",
                code=ErrorCode.SURVEY_TEMPLATE_TOO_LARGE,
            )
        try:
            self._surveys.put_new(survey)
        except SurveyStoreError as e:
            logger.error("Survey save failed: %s", type(e).__name__)
            raise SurveyExecutionError(
                "survey save failed", code=ErrorCode.SURVEY_OPERATION_FAILED
            ) from e
        logger.info("Survey queued: %s", survey.id)
        return SurveyStart(survey=survey)

    def _usable_datasource(
        self, principal: Principal, datasource: Optional[str]
    ) -> tuple[PersonaDataRef, Optional[str]]:
        """The persona data reference and the custom data's name (BR4.2)."""
        try:
            ref = PersonaDataRef.parse(datasource)
        except ValueError as e:
            raise SurveyExecutionValidationError(
                "datasource is not usable", code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND
            ) from e
        if ref.custom_id is None:
            return ref, None
        target = self._resource_lookup.get(
            ResourceType.CUSTOM_PERSONA_DATA, ref.custom_id
        )
        if not AccessPolicy.can_use_asset(principal, target):
            raise SurveyExecutionValidationError(
                "custom persona data is not usable",
                code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND,
            )
        return ref, target.display_name

    def _read_template(self, template_id: str) -> Any:
        try:
            return self._templates.get(template_id, consistent=True)
        except SurveyTemplateStoreError as e:
            raise LookupUnavailableError("template body unreadable") from e

    # =========================================================================
    # 読み書き
    # =========================================================================

    def _read_survey(self, survey_id: str) -> Optional[Survey]:
        try:
            return self._surveys.get(survey_id, consistent=True)
        except SurveyStoreError as e:
            raise LookupUnavailableError("survey body unreadable") from e

    def _ensure_parquet_uri(self, datasource: str) -> None:
        """nemotron使用時、batch_serviceにS3 URIをセットする。

        The key is ``Config.NEMOTRON_DATASET_KEY`` (closed network U3 BR2.1);
        its presence is checked through ``S3Service.exists``. A missing file
        never triggers a download: it stops with ``NEMOTRON_DATASET_NOT_PLACED``
        when the file is placed beforehand (``s3``), else
        ``SURVEY_DATASET_NOT_DOWNLOADED`` (BR2.4).
        """
        self._persona_source.ensure_dataset(datasource, SurveyExecutionError)

    def _settle_legacy(self, survey: Survey) -> Survey:
        """A running / pending survey from batch inference becomes an error (BR8.1).

        The write is conditional on the status read; even when it fails the
        survey is shown as interrupted.
        """
        if not survey.is_legacy_in_flight:
            return survey
        code = ErrorCode.SURVEY_EXECUTION_INTERRUPTED
        try:
            self._surveys.transition(
                survey.id,
                expected=survey.status,
                fields={
                    "status": SurveyStatus.ERROR.value,
                    "error_code": code.value,
                    "updated_at": datetime.now().isoformat(),
                },
                remove=("error_context",),
            )
        except SurveyStoreError:
            logger.warning("Marking an interrupted survey failed", exc_info=True)
        return replace(
            survey,
            status=SurveyStatus.ERROR.value,
            error_code=code.value,
            error_context=None,
        )

    # =========================================================================
    # 一覧（BR1.2）
    # =========================================================================

    def list_surveys(
        self,
        principal: Principal,
        tab: Optional[str],
        cursor: Optional[str],
        search: Optional[str] = None,
    ) -> TabbedList[Survey]:
        """One page of a tab, or a search inside the tab (BR1.2).

        Raises:
            CodedError: ``LOOKUP_UNAVAILABLE`` when the index or the bodies
                cannot be read.
        """
        sharing = self._resource_sharing
        active = sharing.effective_tab(principal, ListTab.parse(tab) or ListTab.MINE)
        query = (search or "").strip()
        if query:
            ids, truncated = self._tab_ids(principal, active)
            needle = query.lower()
            matched = [
                s
                for s in self._readable_bodies(principal, ids)
                if needle in s.name.lower()
            ][:SEARCH_RESULT_LIMIT]
            return self._listing(
                principal,
                active,
                matched,
                counts={active.value: None if truncated else len(ids)},
                search_truncated=truncated,
            )
        page = sharing.list_page(principal, _SURVEY, active, cursor)
        surveys = self._readable_bodies(principal, [t.resource_id for t in page.items])
        counts = self._counts(principal, {active: page.total}) if cursor is None else {}
        return self._listing(
            principal, active, surveys, next_cursor=page.next_cursor, counts=counts
        )

    def _readable_bodies(
        self, principal: Principal, survey_ids: List[str]
    ) -> List[Survey]:
        if not survey_ids:
            return []
        try:
            found = self._surveys.get_many(survey_ids)
        except SurveyStoreError as e:
            raise LookupUnavailableError("survey bodies unreadable") from e
        bodies = [found[i] for i in dict.fromkeys(survey_ids) if i in found]
        return [
            self._settle_legacy(s)
            for s in bodies
            if self._resource_sharing.can_read_body(principal, _SURVEY, s.to_dict())
        ]

    def _tab_ids(self, principal: Principal, active: ListTab) -> tuple[List[str], bool]:
        sharing = self._resource_sharing
        if active is not ListTab.ALL:
            return sharing.collect_ids(principal, _SURVEY, active)
        ids: List[str] = []
        cursor: Optional[str] = None
        while len(ids) <= SEARCH_LIMIT:
            page = sharing.list_page(principal, _SURVEY, ListTab.ALL, cursor)
            ids += [t.resource_id for t in page.items]
            cursor = page.next_cursor
            if not cursor:
                break
        return ids[:SEARCH_LIMIT], len(ids) > SEARCH_LIMIT or bool(cursor)

    def _counts(
        self, principal: Principal, known: Dict[ListTab, Optional[int]]
    ) -> Dict[str, Optional[int]]:
        sharing = self._resource_sharing
        tabs = [ListTab.MINE, ListTab.SHARED]
        if sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL:
            tabs.append(ListTab.ALL)
        return {
            tab.value: known[tab]
            if known.get(tab) is not None
            else sharing.count(principal, _SURVEY, tab)
            for tab in tabs
        }

    def _listing(
        self,
        principal: Principal,
        active: ListTab,
        surveys: List[Survey],
        *,
        next_cursor: Optional[str] = None,
        counts: Dict[str, Optional[int]],
        search_truncated: bool = False,
    ) -> TabbedList[Survey]:
        sharing = self._resource_sharing
        owners: Dict[str, Optional[str]] = {}
        if active is not ListTab.MINE and surveys:
            labels = self._owners.labels({s.owner_sub for s in surveys if s.owner_sub})
            owners = {
                s.id: None
                if labels is None
                else (
                    labels.get(s.owner_sub, OWNER_UNKNOWN)
                    if s.owner_sub
                    else OWNER_UNKNOWN
                )
                for s in surveys
            }
        shared_with = {
            s.id: sharing.department_names(
                sorted(
                    s.shared_group_ids & principal.group_ids
                    if active is ListTab.SHARED
                    else s.shared_group_ids
                )
            )
            for s in surveys
        }
        return TabbedList(
            tab=active,
            items=surveys,
            next_cursor=next_cursor,
            counts=counts,
            show_all=sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL,
            owners=owners,
            shared_with=shared_with,
            manageable=frozenset(
                s.id
                for s in surveys
                if sharing.can_manage_body(principal, _SURVEY, s.to_dict())
            ),
            no_department=not principal.group_ids,
            search_truncated=search_truncated,
        )

    # =========================================================================
    # 読む（BR1.3、BR2.6〜BR2.10）
    # =========================================================================

    def _readable_survey(self, principal: Principal, survey_id: str) -> Survey:
        target = self._resource_lookup.get(_SURVEY, survey_id)
        AccessPolicy.ensure_readable(principal, target, ErrorCode.SURVEY_NOT_FOUND)
        survey = self._read_survey(survey_id)
        if survey is None:
            raise SurveyExecutionManagerError(
                "survey is gone", code=ErrorCode.SURVEY_NOT_FOUND
            )
        return survey

    def get_survey_detail(self, principal: Principal, survey_id: str) -> SurveyDetail:
        """The survey with what the reader may do and see (BR2.7, BR2.10, BR2.11).

        Raises:
            CodedError: ``SURVEY_NOT_FOUND``.
        """
        survey = self._settle_legacy(self._readable_survey(principal, survey_id))
        manageable = self._resource_sharing.can_manage_body(
            principal, _SURVEY, survey.to_dict()
        )
        if survey.template_snapshot is not None:
            return SurveyDetail(
                survey=survey,
                manageable=manageable,
                template_name=survey.template_snapshot.name,
                analysis_available=True,
            )
        template_target = self._resource_lookup.get(_TEMPLATE, survey.template_id)
        template = (
            self._read_template(survey.template_id)
            if AccessPolicy.can_read(principal, template_target)
            else None
        )
        return SurveyDetail(
            survey=survey,
            manageable=manageable,
            template_name=template.name if template is not None else None,
            analysis_available=template is not None,
        )

    def deliver_result_csv(
        self, principal: Principal, survey_id: str
    ) -> DeliveryResult:
        """Serve the result CSV after the read check (BR2.6, U4 BR3.2).

        Raises:
            CodedError: ``SURVEY_NOT_FOUND`` / ``SURVEY_RESULT_NOT_READY``
                (also when the object is gone), ``ASSET_TOO_LARGE`` or
                ``S3_OPERATION_FAILED``.
        """
        survey = self._readable_survey(principal, survey_id)
        handle = AssetHandle(
            s3_path=self._result_path(survey),
            content_type="text/csv",
            download_name=f"{survey.name}.csv",
            size_limit=SizeLimit.SURVEY_CSV,
            not_found_code=ErrorCode.SURVEY_RESULT_NOT_READY,
            presigned_expiration=RESULT_CSV_URL_EXPIRATION_SECONDS,
        )
        return self._asset_delivery.deliver(
            handle, error_type=SurveyExecutionManagerError
        )

    @staticmethod
    def _result_path(survey: Survey) -> str:
        if survey.status != SurveyStatus.COMPLETED or not survey.s3_result_path:
            raise SurveyExecutionManagerError(
                "survey has no result yet", code=ErrorCode.SURVEY_RESULT_NOT_READY
            )
        return survey.s3_result_path

    def deliver_result_image(
        self, principal: Principal, survey_id: str, image_id: str
    ) -> DeliveryResult:
        """Serve a snapshot image after the read check (BR2.8, U4 BR3.3).

        Raises:
            CodedError: ``SURVEY_NOT_FOUND`` (unreadable survey),
                ``FILE_NOT_FOUND`` (not in the snapshot, or deleted),
                ``ASSET_TOO_LARGE`` or ``S3_OPERATION_FAILED``.
        """
        survey = self._readable_survey(principal, survey_id)
        snapshot = survey.template_snapshot
        image = snapshot.image(image_id) if snapshot is not None else None
        if image is None or not self._is_survey_image_upload(image.file_path):
            raise SurveyExecutionManagerError(
                "image is not in the snapshot", code=ErrorCode.FILE_NOT_FOUND
            )
        content_type = (
            image.mime_type
            if image.mime_type in _RESULT_IMAGE_CONTENT_TYPES
            else "application/octet-stream"
        )
        handle = AssetHandle(
            s3_path=image.file_path,
            content_type=content_type,
            download_name=image.original_filename,
            size_limit=SizeLimit.UPLOAD,
            not_found_code=ErrorCode.FILE_NOT_FOUND,
            presigned_expiration=RESULT_IMAGE_URL_EXPIRATION_SECONDS,
            check_exists=True,
        )
        return self._asset_delivery.deliver(
            handle, error_type=SurveyExecutionManagerError
        )

    def _is_survey_image_upload(self, file_path: str) -> bool:
        """Whether a snapshot path is a survey image upload in this bucket.

        The snapshot copies the template's paths, which came from the form, so
        only ``uploads/{owner}/survey_image/{file}`` is served (U4 review).
        """
        key = self.s3_service.key_of(file_path or "")
        parts = (key or "").split("/")
        return (
            len(parts) == 4
            and parts[0] == "uploads"
            and parts[2] == "survey_image"
            and all(parts)
        )

    # =========================================================================
    # 削除（BR1.8）
    # =========================================================================

    def delete_survey(self, principal: Principal, survey_id: str) -> None:
        """Delete the body and its shares, then the CSV and the cache (BR1.8).

        A running survey's execution is stopped (BR7.1; a failure is logged
        and the delete goes on, the time limit ends the execution). A queued
        survey just leaves the queue with its body (BR7.2). The run location
        is removed for both (BR10.1); the caller wakes the survey runner.

        Raises:
            CodedError: ``SURVEY_NOT_FOUND`` / ``SURVEY_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        deleted = self._resource_sharing.delete_with_shares(
            principal, _SURVEY, survey_id
        )
        if deleted is None:
            raise SurveyExecutionManagerError(
                "nothing to delete", code=ErrorCode.SURVEY_NOT_FOUND
            )
        self._stop_run(survey_id, deleted)
        result_path = deleted.get("s3_result_path")
        if result_path:
            self._cleanup(
                survey_id,
                "results",
                lambda: self.s3_service.delete_file(str(result_path)),
            )
        self.result_cache.evict(survey_id)
        logger.info("Survey deleted: %s", survey_id)

    def _stop_run(self, survey_id: str, deleted: Dict[str, Any]) -> None:
        """Stop the execution of a deleted running survey; remove its run location."""
        status = deleted.get("status")
        if status not in (SurveyStatus.QUEUED, SurveyStatus.RUNNING):
            return
        runner = self._survey_runner
        if runner is None:
            return
        arn = deleted.get("execution_arn")
        if status == SurveyStatus.RUNNING and arn:
            try:
                runner.stop_execution(str(arn))
                log_event("survey.run_stopped", survey_id=survey_id, reason="deleted")
            except Exception as e:
                log_event(
                    "survey.run_stop_failed",
                    level="WARNING",
                    survey_id=survey_id,
                    error_type=type(e).__name__,
                )
        self._cleanup(survey_id, "run_location", lambda: runner.delete_run(survey_id))

    def _cleanup(self, survey_id: str, step: str, action: Callable[[], object]) -> None:
        run_cleanup(
            action,
            resource_type=_SURVEY.value,
            resource_id=survey_id,
            step=step,
            sleep=self._sleep,
        )

    # =========================================================================
    # フィルタ正規化（Router層から移動）
    # =========================================================================

    @staticmethod
    def normalize_filters(raw: dict) -> Optional[dict]:
        """フィルタJSONから空値を除去して返す。空なら None。"""
        cleaned: Dict[str, Any] = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                range_cleaned: Dict[str, int] = {}
                for kk, vv in v.items():
                    if vv is not None and vv != "":
                        try:
                            range_cleaned[kk] = int(float(vv))
                        except (ValueError, TypeError):
                            continue  # 数値変換できない値はフィルタから除外
                if range_cleaned:
                    cleaned[k] = range_cleaned
            elif isinstance(v, list):
                if len(v) > 0:
                    cleaned[k] = v
            elif isinstance(v, str) and v:
                cleaned[k] = v
        return cleaned or None

    # =========================================================================
    # バリデーション
    # =========================================================================

    @staticmethod
    def _validate_persona_count(count: int, has_images: bool = False) -> None:
        """ペルソナ数のバリデーション（下限 10、BR1.1〜BR1.3）。"""
        minimum = MIN_SURVEY_PERSONAS
        if not isinstance(count, int) or count < minimum:
            raise SurveyExecutionValidationError(
                f"target count {count} below minimum {minimum}",
                code=ErrorCode.SURVEY_TARGET_COUNT_TOO_LOW,
                context={"min_count": minimum},
            )
        if has_images and count > 1000:
            raise SurveyExecutionValidationError(
                f"target count {count} exceeds 1000 for image survey",
                code=ErrorCode.SURVEY_TARGET_COUNT_TOO_HIGH_WITH_IMAGES,
                context={"max_count": 1000},
            )
        if not has_images and count > 10000:
            raise SurveyExecutionValidationError(
                f"target count {count} exceeds 10000",
                code=ErrorCode.SURVEY_TARGET_COUNT_TOO_HIGH,
                context={"max_count": 10000},
            )

    def _validate_available_personas(
        self, filters: Optional[Dict[str, Any]], datasource: Optional[str]
    ) -> None:
        """フィルタ適用後に実際に集められるペルソナ数を検証する。

        ``persona_count`` は「希望数」でしかなく、サンプリングSQLの ``LIMIT`` は
        上限にすぎない。条件に合致する行が少なければそれだけしか集まらないため、
        ここで実件数を確かめ、アンケートの最小人数を下回るなら作らない（BR1.2）。
        """
        source = datasource or "nemotron"
        self._ensure_parquet_uri(source)
        try:
            available = self.batch_service.get_filtered_count(filters, source)
        except SurveyExecutionManagerError:
            # _ensure_parquet_uri 由来のコード付き例外はそのまま通す。
            raise
        except Exception as e:
            # Service層の例外・生の例外（DuckDB/S3障害）をここで自ドメインへ変換
            # する。素通りさせると Router の except を抜けて未処理500になり、
            # 例外文に載った S3 パスがそのまま応答に出る。
            logger.error("Failed to count available personas (%s)", type(e).__name__)
            raise SurveyExecutionError(
                f"available persona count failed ({type(e).__name__})",
                code=ErrorCode.SURVEY_OPERATION_FAILED,
            ) from e

        # フィルタありなら「条件を緩める」、なしならデータセットが小さいと案内する
        SurveyPersonaSource.require_enough(
            available, filters, SurveyExecutionValidationError
        )

    @staticmethod
    def generate_default_survey_name(template_name: str) -> str:
        """デフォルトのアンケート名を生成する（テンプレート名 + 日付）。"""
        date_str = datetime.now().strftime("%Y%m%d")
        return f"{template_name} {date_str}"
