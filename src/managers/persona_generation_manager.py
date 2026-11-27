"""Manager for the persona generation workflow"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

from pydantic import BaseModel, Field

from ..models.authz import Permission, Principal
from ..models.errors import CodedError, ErrorCode
from ..models.persona import Persona
from ..models.persona_candidate import (
    MAX_CANDIDATE_ITEM_BYTES,
    BehaviorDatasetCandidate,
    CandidateClaim,
    CandidateSaveResult,
    PersonaCandidate,
)
from ..config import config
from ..services.stores.persona_candidate_store import (
    CandidateUnavailable,
    PersonaCandidateStore,
)
from .components.access_policy import AccessPolicy
from .components.data_agent_policy import DataAgentPolicy
from .shared.structured_log import log_event
from ..services.agent_service import (
    AgentService,
    AgentServiceError,
    GenerationCapacityError,
)
from ..services.service_factory import service_factory
from ..prompts.persona_generation_prompts import (
    CSV_ANALYSIS_INSTRUCTIONS,
    CUSTOM_PROMPT_SECTION,
    DATA_TYPE_PROMPTS,
    DWH_AUTO_LINK_INSTRUCTIONS,
    PERSONA_GENERATION_SYSTEM_PROMPT_TEMPLATE,
    STRUCTURED_OUTPUT_PROMPT,
    USER_PROMPT_TEMPLATE,
)
from .shared.file_utils import (
    analyze_csv_schema,
    cleanup_temp_files,
    detect_binding_key,
    extract_text_from_bytes,
    get_csv_preview,
    infer_behavior_data_type,
    save_temp_csv,
)

if TYPE_CHECKING:
    from ..services.dataset_analysis.service import DatasetAnalysisService


logger = logging.getLogger(__name__)


# Same field set as the create_new() parameters of Persona (src/models/persona.py).
# Why: Strands SDK's structured_output() requires a Pydantic BaseModel, but Persona is a dataclass and cannot be passed directly.
# Keep this in sync when Persona's fields change.
class _PersonaOutput(BaseModel):
    name: str = Field(description="名前（国・地域に即した自然な名前。日本語表記）")
    age: int = Field(description="年齢")
    gender: str | None = Field(
        default=None, description="性別（male / female / other）"
    )
    country: str | None = Field(
        default=None,
        description="居住国（ISO 3166-1 alpha-2の2文字コード。例: JP, US）",
    )
    city: str | None = Field(
        default=None, description="居住都市名（日本語。不明なら省略）"
    )
    occupation: str = Field(description="職業")
    background: str = Field(description="背景・経歴")
    values: list[str] = Field(description="価値観（データから導出できるもの）")
    pain_points: list[str] = Field(description="課題・悩み（データから導出できるもの）")
    goals: list[str] = Field(description="目標・願望（データから導出できるもの）")


class _PersonaListOutput(BaseModel):
    personas: list[_PersonaOutput] = Field(description="生成されたペルソナのリスト")


class PersonaGenerationManagerError(CodedError):
    pass


class PersonaGenerationCapacityError(PersonaGenerationManagerError):
    """Error when the generation count or input volume is too large to process.

    Converted from the Service layer's GenerationCapacityError (output token
    limit exceeded, timeout).
    """

    code = ErrorCode.GENERATION_CAPACITY_EXCEEDED


@dataclass(frozen=True)
class GenerationResult:
    """What one generation returns to the Router.

    Attributes:
        personas: The generated personas. Their ``id`` is the candidate ID the
            screen sends back on save (never the saved persona's ID).
        thinking_log: The generation's thinking log.
        behavior_datasets: Behaviour CSV candidates of a single DWH persona.
    """

    personas: list[Persona]
    thinking_log: list[dict[str, str]]
    behavior_datasets: tuple[BehaviorDatasetCandidate, ...] = ()


class _RecordingQueue:
    """Forwards data-agent events and keeps the CSV URLs they report.

    The data agent tool reports ``csv_url`` events right after the
    ``tool_call`` that asked for the CSV; the call's detail is the CSV's label.
    """

    def __init__(self, inner: Any = None) -> None:
        self._inner = inner
        self._last_detail = ""
        self.csv_urls: list[str] = []
        self.csv_labels: list[str] = []

    def put(self, event: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        kind = event.get("type")
        if kind == "tool_call":
            self._last_detail = str(event.get("detail", ""))
        elif kind == "csv_url" and event.get("url"):
            self.csv_urls.append(str(event["url"]))
            self.csv_labels.append(self._last_detail)
        if self._inner is not None:
            self._inner.put(event, *args, **kwargs)


_UNSET: Any = object()


class PersonaGenerationManager:
    """Orchestration of the whole persona generation workflow.

    Generated candidates are placed outside the app (PersonaCandidates) together
    with the user who generated them, and only that user can save them. There is
    no in-app cache.
    """

    def __init__(
        self,
        agent_service: AgentService | None = None,
        dataset_analysis_service: "DatasetAnalysisService | None" = None,
        candidate_store: "PersonaCandidateStore | None" = None,
        s3_service: Any = _UNSET,
        clock: Callable[[], datetime] | None = None,
    ):
        """
        Args:
            agent_service: Generation agents (optional, singleton when omitted)
            dataset_analysis_service: CSV analysis tools (optional)
            candidate_store: ``PersonaCandidates`` (optional, built on first use)
            s3_service: The temporary CSV area; ``None`` disables behaviour
                datasets (optional, the configured bucket when omitted)
            clock: Aware-UTC clock (tests)
        """
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.dataset_analysis_service = (
            dataset_analysis_service or service_factory.get_dataset_analysis_service()
        )
        self._candidate_store = candidate_store
        self._s3 = s3_service
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        #: The recording queue of the generation in progress (tests read it)
        self._recording_queue = _RecordingQueue()

    @property
    def _persona_candidate_store(self) -> "PersonaCandidateStore":
        if self._candidate_store is None:
            self._candidate_store = service_factory.get_persona_candidate_store()
        return self._candidate_store

    @property
    def _staging(self) -> Any:
        if self._s3 is _UNSET:
            self._s3 = service_factory.get_optional_s3_service()
        return self._s3

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_data_agent_allowed(self, principal: Principal) -> None:
        """Refuse a DWH generation before the router starts streaming.

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            PersonaGenerationManagerError: ``DATA_AGENT_NOT_ALLOWED``.
        """
        AccessPolicy.require(principal, Permission.AI_EXECUTE)
        DataAgentPolicy.ensure_allowed(PersonaGenerationManagerError)

    def generate(
        self,
        principal: Principal,
        *,
        file_contents: list[tuple[bytes, str]],
        data_type: str,
        persona_count: int,
        data_description: str | None = None,
        custom_prompt: str | None = None,
        event_queue: Any = None,
        auto_link_behavior: bool = False,
    ) -> GenerationResult:
        """Generate personas and store them as the user's candidates.

        For a DWH generation with ``auto_link_behavior`` the behaviour CSVs the
        data agent exported are staged under ``candidates/``. Every
        candidate is measured before anything is written; one over 350KB fails
        the whole generation.

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            PersonaGenerationManagerError: Validation, generation, size
                (``GENERATION_RESULT_TOO_LARGE``) or the write
                (``GENERATION_OPERATION_FAILED``) failed.
        """
        AccessPolicy.require(principal, Permission.AI_EXECUTE)
        if data_type == "dwh":
            # Closed network: before any data agent tool is built
            DataAgentPolicy.ensure_allowed(PersonaGenerationManagerError)
        self._validate_generation_input(
            data_type, persona_count, file_contents, data_description
        )
        recorder = _RecordingQueue(event_queue)
        self._recording_queue = recorder

        if data_type == "dwh":
            if auto_link_behavior:
                persona_count = 1
            personas, thinking_log = self._generate_from_dwh(
                analysis_angle=data_description or "",
                persona_count=persona_count,
                custom_prompt=custom_prompt,
                event_queue=recorder,
                auto_link_behavior=auto_link_behavior,
            )
        else:
            personas, thinking_log = self._generate_from_files(
                file_contents=file_contents,
                data_type=data_type,
                persona_count=persona_count,
                data_description=data_description,
                custom_prompt=custom_prompt,
            )

        for persona in personas:
            self._validate_generated_persona(persona)

        gen_ctx = self._build_generation_context(
            data_type=data_type,
            data_description=data_description,
            custom_prompt=custom_prompt,
            source_files=[fn for _, fn in file_contents],
            persona_count=persona_count,
            auto_link_behavior=auto_link_behavior,
        )
        for persona in personas:
            persona.generation_log = thinking_log
            persona.generation_context = gen_ctx

        principal_sub = principal.sub
        now = self._clock()
        behavior: tuple[BehaviorDatasetCandidate, ...] = ()
        if (
            data_type == "dwh"
            and auto_link_behavior
            and len(personas) == 1
            and recorder.csv_urls
        ):
            behavior = self._stage_behavior_datasets(
                principal_sub, personas[0], thinking_log, recorder
            )

        generation_id = str(uuid.uuid4())
        candidates = [
            PersonaCandidate.from_persona(
                persona,
                owner_sub=principal_sub,
                generation_id=generation_id,
                ttl_seconds=config.PERSONA_CACHE_TTL_SECONDS,
                now=now,
                generation_log=thinking_log,
                generation_context=gen_ctx,
                behavior_datasets=behavior if len(personas) == 1 else (),
            )
            for persona in personas
        ]
        if any(
            PersonaCandidateStore.item_size(c) > MAX_CANDIDATE_ITEM_BYTES
            for c in candidates
        ):
            self._delete_temp_csvs(candidates, step="temp_csv")
            raise PersonaGenerationManagerError(
                "a generated candidate exceeds the item size limit",
                code=ErrorCode.GENERATION_RESULT_TOO_LARGE,
            )
        try:
            self._persona_candidate_store.put_all(candidates)
        except Exception as e:
            logger.error("Failed to store generated candidates", exc_info=True)
            self._delete_temp_csvs(candidates, step="temp_csv")
            raise PersonaGenerationManagerError(
                f"candidate write failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e

        return GenerationResult(
            personas=personas, thinking_log=thinking_log, behavior_datasets=behavior
        )

    def claim_candidates(
        self, principal: Principal, candidate_ids: list[str]
    ) -> CandidateClaim:
        """Hold the user's candidates for one save request.

        Each candidate is claimed with one conditional update (owner, expiry,
        not saved, no live claim). If any cannot be claimed, the ones already
        claimed are released and nothing is saved. Someone else's, expired,
        being-saved and missing candidates get the same answer.

        Raises:
            PersonaGenerationManagerError: ``PERSONA_SELECTION_REQUIRED`` (no
                IDs), ``GENERATION_PERSONA_CACHE_EXPIRED`` (unavailable), or
                ``GENERATION_OPERATION_FAILED`` (the store failed).
        """
        ids = list(dict.fromkeys(i for i in candidate_ids if i))
        if not ids:
            raise PersonaGenerationManagerError(
                "no candidate selected", code=ErrorCode.PERSONA_SELECTION_REQUIRED
            )
        token = CandidateClaim.new_token()
        now = self._clock()
        claimed: list[PersonaCandidate] = []
        try:
            for candidate_id in ids:
                claimed.append(
                    self._persona_candidate_store.claim(
                        candidate_id, principal.sub, token, now
                    )
                )
        except CandidateUnavailable as e:
            self._release_all(claimed, token)
            # Not an authorization denial: one INFO line, no IDs of others
            log_event("persona.candidate_unavailable", level="INFO", count=len(ids))
            raise PersonaGenerationManagerError(
                "candidate unavailable", code=ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED
            ) from e
        except Exception as e:
            self._release_all(claimed, token)
            logger.error("Failed to claim candidates", exc_info=True)
            raise PersonaGenerationManagerError(
                f"candidate claim failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        return CandidateClaim(
            token=token, owner_sub=principal.sub, candidates=tuple(claimed)
        )

    def finish_candidates(
        self, claim: CandidateClaim, result: CandidateSaveResult | None
    ) -> None:
        """Clean up after a save, whatever its outcome. Never raises.

        Saved candidates lose their item and every temporary CSV (selected or
        not); when the item cannot be deleted it is marked saved so it cannot
        be saved twice. Failed and unprocessed candidates are released. Every
        cleanup failure is one WARNING line; TTL, the lifecycle rule
        and the 5-minute claim timeout clean up later.
        """
        saved = set(result.saved_ids) if result is not None else set()
        for candidate in claim.candidates:
            if candidate.candidate_id in saved:
                self._delete_temp_csvs([candidate], step="temp_csv")
                self._delete_saved(candidate, claim.token)
            else:
                self._release(candidate, claim.token)

    # --- candidate cleanup helpers ------------------------------------------

    def _release_all(self, candidates: list[PersonaCandidate], token: str) -> None:
        for candidate in candidates:
            self._release(candidate, token)

    def _release(self, candidate: PersonaCandidate, token: str) -> None:
        try:
            self._persona_candidate_store.release(candidate.candidate_id, token)
        except Exception as e:
            self._cleanup_failed(candidate.candidate_id, "release", e)

    def _delete_saved(self, candidate: PersonaCandidate, token: str) -> None:
        try:
            if self._persona_candidate_store.delete(candidate.candidate_id, token):
                return
        except Exception as e:
            self._cleanup_failed(candidate.candidate_id, "delete", e)
        try:
            self._persona_candidate_store.mark_saved(
                candidate.candidate_id, token, self._clock()
            )
        except Exception as e:
            self._cleanup_failed(candidate.candidate_id, "mark_saved", e)

    def _delete_temp_csvs(
        self, candidates: list[PersonaCandidate], *, step: str
    ) -> None:
        s3 = self._staging
        for candidate in candidates:
            for dataset in candidate.behavior_datasets:
                if s3 is None:
                    continue
                try:
                    s3.delete_file(s3.path_of(dataset.temp_s3_key))
                except Exception as e:
                    self._cleanup_failed(candidate.candidate_id, step, e)

    @staticmethod
    def _cleanup_failed(candidate_id: str, step: str, error: BaseException) -> None:
        log_event(
            "persona.candidate_cleanup_failed",
            level="WARNING",
            candidate_id=candidate_id,
            step=step,
            error_type=type(error).__name__,
        )

    def _stage_behavior_datasets(
        self,
        owner_sub: str,
        persona: Persona,
        thinking_log: list[dict[str, str]],
        recorder: _RecordingQueue,
    ) -> tuple[BehaviorDatasetCandidate, ...]:
        """Fetch the behaviour CSVs and place them under ``candidates/``.

        A CSV that cannot be fetched, parsed or placed is left out (logged);
        the generation itself goes on.
        """
        s3 = self._staging
        if s3 is None:
            logger.warning("Behaviour datasets skipped: S3 is not configured")
            return ()
        parsed = self._build_behavior_dataset_candidates(
            csv_urls=recorder.csv_urls,
            persona_name=persona.name,
            thinking_log=thinking_log,
            csv_url_labels=recorder.csv_labels,
        )
        staged: list[BehaviorDatasetCandidate] = []
        for item in parsed:
            key = BehaviorDatasetCandidate.temp_key(
                owner_sub, persona.id, item["temp_id"]
            )
            try:
                s3.upload_file(item["csv_bytes"], key, content_type="text/csv")
            except Exception as e:
                logger.warning(
                    "Behaviour CSV could not be staged: %s", type(e).__name__
                )
                continue
            staged.append(
                BehaviorDatasetCandidate(
                    temp_id=item["temp_id"],
                    name=item["name"],
                    data_type_label=item["data_type_label"],
                    temp_s3_key=key,
                    columns=tuple(item["columns"]),
                    row_count=int(item["row_count"]),
                    binding_key_column=item.get("binding_key_column") or None,
                    binding_key_value=item.get("binding_key_value") or None,
                )
            )
        return tuple(staged)

    # ------------------------------------------------------------------
    # Generation workflow internals
    # ------------------------------------------------------------------

    def _validate_generation_input(
        self,
        data_type: str,
        persona_count: int,
        file_contents: list[tuple[bytes, str]],
        data_description: str | None,
    ) -> None:
        if persona_count < 1 or persona_count > 10:
            raise PersonaGenerationManagerError(
                f"persona_count {persona_count} out of range 1-10",
                code=ErrorCode.GENERATION_PERSONA_COUNT_INVALID,
            )
        if data_type == "dwh":
            if not data_description or not data_description.strip():
                raise PersonaGenerationManagerError(
                    "data_description is blank for dwh generation",
                    code=ErrorCode.GENERATION_DATA_DESCRIPTION_REQUIRED,
                )
        else:
            if not file_contents:
                raise PersonaGenerationManagerError(
                    "file_contents is empty for file-based generation",
                    code=ErrorCode.GENERATION_FILES_REQUIRED,
                )

    def _generate_from_files(
        self,
        file_contents: list[tuple[bytes, str]],
        data_type: str,
        persona_count: int,
        data_description: str | None,
        custom_prompt: str | None,
    ) -> tuple[list[Persona], list[dict[str, str]]]:
        """File-based persona generation"""
        logger.info(
            f"ファイルベースペルソナ生成開始 (data_type={data_type}, count={persona_count}, files={len(file_contents)})"
        )

        # Extraction cleans up its temp files in its own finally (a failure does not
        # reach the caller's finally). After a successful extraction, the try/finally
        # below is the sole owner of the temp files.
        combined_text, source_descriptors = self._extract_file_texts(file_contents)
        csv_temp_paths = [d["path"] for d in source_descriptors]

        # Wrap all processing in try/finally right after extraction so no temp file is left
        # even if tool construction (build_source_tools) fails.
        try:
            # Enabled only when the global kill switch AND the presence of CSVs both hold.
            enable_dataset_analysis = (
                bool(source_descriptors) and config.ENABLE_DATASET_ANALYSIS
            )
            active_descriptors = source_descriptors if enable_dataset_analysis else []

            system_prompt = self._build_system_prompt(
                data_type, data_description, custom_prompt
            )
            tools = self._determine_tools(
                data_type, active_descriptors, event_queue=None
            )
            user_prompt = self._build_user_prompt(
                combined_text, persona_count, active_descriptors or None
            )

            agent = self.agent_service.create_generation_agent(
                system_prompt=system_prompt,
                tools=tools if tools else None,
            )
            result, thinking_log = self.agent_service.run_persona_generation(
                agent=agent,
                prompt=user_prompt,
                structured_prompt=STRUCTURED_OUTPUT_PROMPT,
                output_schema=_PersonaListOutput,
            )
            personas = self._convert_to_personas(result)
        except GenerationCapacityError as e:
            raise PersonaGenerationCapacityError(
                "persona generation capacity exceeded (file input)",
                context={"persona_count": persona_count},
            ) from e
        except AgentServiceError as e:
            raise PersonaGenerationManagerError(
                f"agent service failed during file-based generation "
                f"({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        except PersonaGenerationManagerError:
            raise
        except Exception as e:
            raise PersonaGenerationManagerError(
                f"file-based persona generation failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        finally:
            cleanup_temp_files(csv_temp_paths)

        logger.info(f"ファイルベースペルソナ生成完了: {len(personas)}個")
        return personas, thinking_log

    def _generate_from_dwh(
        self,
        analysis_angle: str,
        persona_count: int,
        custom_prompt: str | None = None,
        event_queue: Any = None,
        auto_link_behavior: bool = False,
    ) -> tuple[list[Persona], list[dict[str, str]]]:
        """DWH-integrated persona generation"""
        logger.info(
            f"DWH ペルソナ生成開始 (angle={analysis_angle!r}, count={persona_count}, auto_link={auto_link_behavior})"
        )

        callback_handler = None
        if event_queue is not None:

            def _queue_callback(**kwargs: Any) -> None:
                data = kwargs.get("data", "")
                complete = kwargs.get("complete", False)
                if data:
                    event_queue.put({"type": "thinking", "content": data})
                if complete and data:
                    event_queue.put({"type": "thinking_done", "content": ""})

            callback_handler = _queue_callback

        data_text = f"分析の切り口: {analysis_angle}"
        if auto_link_behavior:
            data_text += DWH_AUTO_LINK_INSTRUCTIONS

        system_prompt = self._build_system_prompt("dwh", analysis_angle, custom_prompt)
        tools = self._determine_tools("dwh", [], event_queue=event_queue)
        user_prompt = self._build_user_prompt(data_text, persona_count)

        try:
            agent = self.agent_service.create_generation_agent(
                system_prompt=system_prompt,
                tools=tools if tools else None,
                callback_handler=callback_handler,
            )
            result, thinking_log = self.agent_service.run_persona_generation(
                agent=agent,
                prompt=user_prompt,
                structured_prompt=STRUCTURED_OUTPUT_PROMPT,
                output_schema=_PersonaListOutput,
            )
            personas = self._convert_to_personas(result)
        except GenerationCapacityError as e:
            raise PersonaGenerationCapacityError(
                "persona generation capacity exceeded (dwh)",
                context={"persona_count": persona_count},
            ) from e
        except AgentServiceError as e:
            raise PersonaGenerationManagerError(
                f"data agent integration failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        except Exception as e:
            raise PersonaGenerationManagerError(
                f"dwh persona generation failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e

        logger.info(f"DWH ペルソナ生成完了: {len(personas)}個")
        return personas, thinking_log

    def _convert_to_personas(self, result: _PersonaListOutput) -> list[Persona]:
        """Convert Pydantic output to the Persona domain model"""
        from ..models.demographics import sanitize_gender
        from ..services.country_service import sanitize_country

        personas: list[Persona] = []
        for p in result.personas:
            persona = Persona.create_new(
                name=p.name,
                age=p.age,
                occupation=p.occupation,
                background=p.background,
                values=p.values,
                pain_points=p.pain_points,
                goals=p.goals,
                gender=sanitize_gender(p.gender),
                country=sanitize_country(p.country),
                city=p.city,
            )
            personas.append(persona)
        return personas

    def _build_system_prompt(
        self,
        data_type: str,
        data_description: str | None,
        custom_prompt: str | None,
    ) -> str:
        """Build the system_prompt according to the data type"""
        role_prompt = DATA_TYPE_PROMPTS.get(data_type)
        if role_prompt is None:
            role_prompt = f"以下は「{data_description or 'ユーザー提供データ'}」です。データ内容を分析してペルソナを生成してください。"

        system_prompt = PERSONA_GENERATION_SYSTEM_PROMPT_TEMPLATE.format(
            role_prompt=role_prompt
        )

        if custom_prompt:
            system_prompt += CUSTOM_PROMPT_SECTION.format(custom_prompt=custom_prompt)

        return system_prompt

    def _build_user_prompt(
        self,
        data_text: str,
        persona_count: int,
        source_descriptors: list[dict[str, Any]] | None = None,
    ) -> str:
        """Build the user-facing prompt"""
        prompt = USER_PROMPT_TEMPLATE.format(
            persona_count=persona_count, data_text=data_text
        )

        if source_descriptors:
            # Show the LLM only aliases (dataset_id) and column names with inferred types (no paths or SQL).
            csv_info = "\n".join(
                f'- dataset_id "{d["alias"]}"（列: {self._format_source_columns(d)}）'
                for d in source_descriptors
            )
            prompt += CSV_ANALYSIS_INSTRUCTIONS.format(csv_info=csv_info)

        prompt += f"\n{persona_count}個のペルソナを生成してください。"
        return prompt

    @staticmethod
    def _format_source_columns(descriptor: dict[str, Any]) -> str:
        """List the columns of a source descriptor as `name (type)` (name only if no type)."""
        detail = descriptor.get("columns_detail")
        if detail:
            return ", ".join(
                f"{c['name']} ({c['data_type']})" if c.get("data_type") else c["name"]
                for c in detail
            )
        return ", ".join(descriptor.get("columns", []))

    def _determine_tools(
        self,
        data_type: str,
        source_descriptors: list[dict[str, Any]],
        event_queue: Any,
    ) -> list[Any]:
        """Decide the tool list used for generation"""

        tools: list[Any] = []

        if data_type == "dwh":
            from ..services.data_agent_service import create_data_agent_tool

            if not config.DATA_AGENT_RUNTIME_ARN:
                raise PersonaGenerationManagerError(
                    "DATA_AGENT_RUNTIME_ARN is not configured",
                    code=ErrorCode.DATA_AGENT_NOT_CONFIGURED,
                )
            data_agent_tool = create_data_agent_tool(
                config.DATA_AGENT_RUNTIME_ARN,
                config.DATA_AGENT_REGION,
                event_queue=event_queue,
            )
            tools.append(data_agent_tool)

        if source_descriptors:
            tools.extend(
                self.dataset_analysis_service.build_source_tools(source_descriptors)
            )

        return tools

    def _extract_file_texts(
        self, file_contents: list[tuple[bytes, str]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Extract text from files. Returns: (combined_text, source_descriptors)

        source_descriptors is {"alias", "path", "columns"} per CSV. The alias is
        numbered here by the Manager (source_1, source_2, ...) and is the only
        identifier passed to the Service (original file names and paths are not
        shown to the LLM).
        """
        texts: list[str] = []
        csv_temp_paths: list[str] = []
        source_descriptors: list[dict[str, Any]] = []

        # If validation raises, the temp CSV created here would not reach the caller's
        # finally, so clean it up ourselves before raising.
        try:
            source_index = 0
            for content, filename in file_contents:
                if filename.lower().endswith(".csv"):
                    source_index += 1
                    alias = f"source_{source_index}"
                    csv_path = save_temp_csv(content)
                    csv_temp_paths.append(csv_path)
                    # Infer columns from the UTF-8 normalized temp file (supports Shift_JIS/EUC-JP).
                    with open(csv_path, "rb") as f:
                        utf8_bytes = f.read()
                    try:
                        columns, _ = analyze_csv_schema(utf8_bytes)
                    except ValueError as e:
                        # Reject empty/duplicate headers because they diverge from the analyze_dataset query columns.
                        raise PersonaGenerationManagerError(
                            f"invalid CSV header in {filename!r}: {e}",
                            code=ErrorCode.FILE_CSV_HEADER_INVALID,
                        ) from e
                    source_descriptors.append(
                        {
                            "alias": alias,
                            "path": csv_path,
                            # Column names only, for the allowlist (used by build_source_tools).
                            "columns": [c.name for c in columns],
                            # Name + inferred type for prompt display (CSVs have no descriptions, so type only).
                            "columns_detail": [
                                {"name": c.name, "data_type": c.data_type}
                                for c in columns
                            ],
                        }
                    )
                    preview = get_csv_preview(content, max_lines=20)
                    # The heading is limited to "first 20 rows preview". Whether analyze_dataset
                    # can access all data is claimed only in CSV_ANALYSIS_INSTRUCTIONS
                    # (_build_user_prompt), which follows the enable check, so a tool that does not
                    # exist is not hinted at when disabled.
                    texts.append(
                        f"--- {alias} (CSV, 先頭20行プレビュー) ---\n{preview}"
                    )
                else:
                    # Insufficient content can only be judged after extraction (PDF/DOCX character
                    # counts are unknown at the byte stage). A CSV keeps only the 20-row preview and
                    # all data goes through the DuckDB tool, so it is excluded from this check.
                    text = extract_text_from_bytes(content, filename)
                    if len(text.strip()) < 10:
                        raise PersonaGenerationManagerError(
                            f"extracted content of {filename!r} below minimum 10 chars",
                            code=ErrorCode.INTERVIEW_FILE_CONTENT_TOO_SHORT,
                        )
                    texts.append(f"--- {filename} ---\n{text}")

            combined_text = "\n\n".join(texts)

            # Total characters = extracted text length = input token budget is the real limit.
            # Totals across multiple files and via PDF/Word are naturally covered here too.
            if len(combined_text) > config.PERSONA_SOURCE_MAX_CHARS:
                raise PersonaGenerationCapacityError(
                    f"combined source text {len(combined_text)} chars exceeds "
                    f"{config.PERSONA_SOURCE_MAX_CHARS}",
                    context={"max_chars": config.PERSONA_SOURCE_MAX_CHARS},
                )
        except Exception:
            # Besides PersonaGenerationManagerError, leave no temp CSV even on unexpected
            # exceptions such as _csv.Error and UnicodeDecodeError raised by
            # analyze_csv_schema / get_csv_preview (the caller's finally is not reached
            # because the return value is not yet determined).
            cleanup_temp_files(csv_temp_paths)
            raise

        return combined_text, source_descriptors

    def _build_generation_context(
        self,
        data_type: str,
        data_description: str | None,
        custom_prompt: str | None,
        source_files: list[str],
        persona_count: int,
        auto_link_behavior: bool,
    ) -> dict[str, Any]:
        """Build the generation context (metadata)"""
        ctx: dict[str, Any] = {
            "data_type": data_type,
            "data_description": data_description,
            "custom_prompt": custom_prompt,
            "source_files": source_files,
            "persona_count": persona_count,
            "generated_at": datetime.now().isoformat(),
        }
        if auto_link_behavior:
            ctx["auto_link_behavior"] = True
        return ctx

    def _validate_generated_persona(self, persona: Persona) -> None:
        """Basic validation of the generated persona"""
        if not persona.name or not persona.name.strip():
            raise PersonaGenerationManagerError(
                "generated persona has no name",
                code=ErrorCode.PERSONA_INVALID,
            )
        if not persona.id:
            raise PersonaGenerationManagerError(
                "generated persona has no id",
                code=ErrorCode.PERSONA_INVALID,
            )

    # ------------------------------------------------------------------
    # Behaviour data linking
    # ------------------------------------------------------------------

    def _build_behavior_dataset_candidates(
        self,
        csv_urls: list[str],
        persona_name: str,
        thinking_log: list[dict[str, str]],
        csv_url_labels: list[str],
    ) -> list[dict[str, Any]]:
        """Build dataset candidates from the CSV URL list"""
        from ..services.data_agent_service import DataAgentService

        fallback_col, fallback_val = self._extract_user_id_from_log(thinking_log)

        candidates: list[dict[str, Any]] = []
        type_counter: int = 0
        label_counts: dict[str, int] = {}

        for idx, url in enumerate(csv_urls):
            try:
                csv_bytes = DataAgentService.download_csv(url)
                columns, row_count = analyze_csv_schema(csv_bytes)
                if row_count == 0:
                    continue

                col_names = [c.name for c in columns]

                data_type_label = ""
                if csv_url_labels and idx < len(csv_url_labels):
                    data_type_label = self._extract_label_from_tool_call(
                        csv_url_labels[idx]
                    )

                if not data_type_label:
                    data_type_label = infer_behavior_data_type(col_names)

                if not data_type_label:
                    type_counter += 1
                    data_type_label = f"行動データ{type_counter}"

                binding_key_col, binding_key_val = detect_binding_key(
                    col_names, csv_bytes
                )
                if not binding_key_col and fallback_col:
                    binding_key_col, binding_key_val = fallback_col, fallback_val

                label_counts[data_type_label] = label_counts.get(data_type_label, 0) + 1
                if label_counts[data_type_label] > 1:
                    dataset_name = f"{persona_name}_{data_type_label}{label_counts[data_type_label]}"
                else:
                    dataset_name = f"{persona_name}_{data_type_label}"

                candidates.append(
                    {
                        "temp_id": str(uuid.uuid4()),
                        "name": dataset_name,
                        "data_type_label": data_type_label,
                        "csv_bytes": csv_bytes,
                        "columns": columns,
                        "row_count": row_count,
                        "binding_key_column": binding_key_col,
                        "binding_key_value": binding_key_val,
                    }
                )
            except Exception as e:
                logger.warning(f"行動データCSVダウンロード/解析エラー: {e}")
                continue

        return candidates

    def _extract_label_from_tool_call(self, detail: str) -> str:
        """Extract the data type label from the tool_call detail"""
        m = re.search(r"の(.+?)を.*CSV", detail)
        if m:
            return m.group(1).strip()
        m = re.search(r"(.+?)をCSV", detail)
        if m:
            label = m.group(1).strip()
            if len(label) > 20:
                parts = re.split(r"の", label)
                if parts:
                    label = parts[-1]
            return label
        return ""

    def _extract_user_id_from_log(
        self, thinking_log: list[dict[str, str]]
    ) -> tuple[str, str]:
        """Extract customer_id/user_id and its value from the thinking log"""
        patterns = [
            r"customer_id\s*[=:]\s*['\"]?([a-zA-Z0-9\-_]+)['\"]?",
            r"user_id\s*[=:]\s*['\"]?([a-zA-Z0-9\-_]+)['\"]?",
        ]
        all_text = " ".join(
            entry.get("content", "") + " " + entry.get("detail", "")
            for entry in thinking_log
        )
        for pattern in patterns:
            matches = re.findall(pattern, all_text)
            if matches:
                col_name = "customer_id" if "customer_id" in pattern else "user_id"
                return col_name, matches[-1]
        return "", ""
