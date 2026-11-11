"""Run the migration: read v0, convert, and (with ``apply``) write v1.

Every write goes through the v1 stores' conditional writes, so an item that
is already in v1 is counted as migrated and left as it is; a run stopped
half-way is simply run again. Children are written before their parent
(messages and reports before the discussion body, objects before the item
that points at them), so a parent present in v1 means it is complete.
Departments are set afterwards with ``ResourceShareStore.write_shares``,
which writes the body column and the share index in one transaction.

v0 is only read: ``Scan``, ``GetObject`` / ``HeadObject`` and ``ListUsers``.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import polars as pl
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from src.models.authz import ResourceType, Role
from src.models.custom_persona_data import CustomPersonaData
from src.models.errors import CodedError
from src.models.persona_binding import KB_TARGET_KEY
from src.models.sharing import SHAREABLE_TYPES
from src.services.stores.custom_persona_data_store import (
    CustomPersonaDataConditionFailed,
    CustomPersonaDataNameTaken,
    CustomPersonaDataStore,
)
from src.services.stores.dataset_store import DatasetStore
from src.services.stores.discussion_store import DiscussionStore
from src.services.stores.knowledge_base_store import KnowledgeBaseStore
from src.services.stores.persona_binding_store import PersonaBindingStore
from src.services.stores.persona_store import PersonaStore
from src.services.stores.resource_share_store import ResourceShareStore
from src.services.stores.survey_store import SurveyStore
from src.services.stores.survey_template_store import SurveyTemplateStore
from src.services.stores.uploaded_file_store import UploadedFileStore

from . import convert
from .discover import Environment
from .owners import MappingError, OwnerMapping, OwnerResolver
from .report import KindReport, Report
from .selection import KINDS
from .users import V0User, normalize_email, plan_users

logger = logging.getLogger(__name__)

#: Group every re-created user joins (admins are switched by hand afterwards).
MEMBER_GROUP = f"role:{Role.MEMBER.value}"
_DEPARTMENT_PREFIX = "group:"
_CUSTOM_PREFIX = "persona-dataset/custom/"


class SourceMissing(RuntimeError):
    """An object the item points at is not in the v0 bucket."""


@dataclass(frozen=True)
class Clients:
    """boto3 clients of both sides (v0 is only read)."""

    v0_dynamodb: Any
    v0_s3: Any
    v0_cognito: Any
    v1_dynamodb: Any
    v1_s3: Any
    v1_cognito: Any


@dataclass
class _Present:
    """IDs present in v1 after (or, in a dry run, as if after) this run."""

    ids: dict[str, set[str]] = field(default_factory=dict)

    def add(self, kind: str, item_id: str) -> None:
        self.ids.setdefault(kind, set()).add(item_id)

    def has(self, kind: str, item_id: str) -> bool:
        return item_id in self.ids.get(kind, set())


class Migration:
    """One run of the migration.

    Args:
        v0: The v0 environment.
        v1: The v1 environment.
        clients: boto3 clients.
        kinds: The kinds to migrate (:func:`selection.select_kinds`).
        mapping: The mapping file.
        default_owner: Email of the default owner (a v1 user).
        apply: Write to v1; a dry run only reads.
        invite: Send the invitation email to re-created users. The closed
            deployment passes ``False``: users are created without a message
            and the admin hands out a temporary password later.
        clock: Epoch seconds (tests).
    """

    def __init__(
        self,
        *,
        v0: Environment,
        v1: Environment,
        clients: Clients,
        kinds: tuple[str, ...],
        mapping: OwnerMapping,
        default_owner: str,
        apply: bool,
        invite: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.v0 = v0
        self.v1 = v1
        self.c = clients
        self.kinds = kinds
        self.mapping = mapping
        self.default_owner = default_owner
        self.apply = apply
        self.invite = invite
        self.now_epoch = int(clock())
        self.report = Report(KINDS, kinds)
        self.present = _Present()
        self._deserializer = TypeDeserializer()
        self._serializer = TypeSerializer()
        self._custom_ids: dict[str, str] = {}
        ddb, region, t = clients.v1_dynamodb, v1.region, v1.table
        self.personas = PersonaStore(
            region=region, table_name=t("personas"), client=ddb
        )
        self.datasets = DatasetStore(
            region=region, table_name=t("datasets"), client=ddb
        )
        self.kbs = KnowledgeBaseStore(
            region=region, table_name=t("knowledge_bases"), client=ddb
        )
        self.custom = CustomPersonaDataStore(
            region=region, table_name=t("custom_persona_data"), client=ddb
        )
        self.bindings = PersonaBindingStore(
            region=region, table_name=t("persona_bindings"), client=ddb
        )
        self.templates = SurveyTemplateStore(
            region=region, table_name=t("survey_templates"), client=ddb
        )
        self.surveys = SurveyStore(region=region, table_name=t("surveys"), client=ddb)
        self.files = UploadedFileStore(
            region=region, table_name=t("uploaded_files"), client=ddb
        )
        self.discussions = DiscussionStore(
            region=region,
            discussions_table=t("discussions"),
            messages_table=t("discussion_messages"),
            reports_table=t("discussion_reports"),
            client=ddb,
        )
        self.shares = ResourceShareStore(
            region=region, shares_table=t("resource_shares"), client=ddb
        )
        self.resolver: OwnerResolver | None = None

    # --- entry point ---------------------------------------------------------------

    def run(self) -> Report:
        """Migrate the selected kinds and return the report.

        Raises:
            MappingError: The default owner, a mapped owner or a mapped
                department is not in the v1 pool (nothing is written).
        """
        self._check_departments()
        v1_users = self._v1_users()
        planned: set[str] = set()
        if "users" in self.kinds:
            planned = self._users(set(v1_users))
            if self.apply:
                v1_users = self._v1_users()
                planned = set()
        self.resolver = OwnerResolver(
            v1_users,
            default_email=self.default_owner,
            mapping=self.mapping,
            planned_emails=planned,
        )
        steps: Mapping[str, Callable[[], None]] = {
            "datasets": self._datasets,
            "knowledge_bases": self._knowledge_bases,
            "custom_persona_data": self._custom_persona_data,
            "personas": self._personas,
            "bindings": self._bindings,
            "survey_templates": self._survey_templates,
            "surveys": self._surveys,
            "discussions": self._discussions,
        }
        for kind in self.kinds:
            if kind in steps:
                steps[kind]()
        return self.report

    @property
    def owner_resolver(self) -> OwnerResolver:
        assert self.resolver is not None
        return self.resolver

    # --- users ---------------------------------------------------------------------

    def _check_departments(self) -> None:
        wanted = self.mapping.group_ids()
        if not wanted:
            return
        names: set[str] = set()
        paginator = self.c.v1_cognito.get_paginator("list_groups")
        for page in paginator.paginate(UserPoolId=self.v1.user_pool_id):
            names.update(g["GroupName"] for g in page.get("Groups") or [])
        missing = sorted(g for g in wanted if f"{_DEPARTMENT_PREFIX}{g}" not in names)
        if missing:
            raise MappingError(
                "departments not in the v1 pool (create group:<slug> first): "
                + ", ".join(missing)
            )

    def _v1_users(self) -> dict[str, str]:
        """Normalized email -> ``sub`` of every user in the v1 pool."""
        subs: dict[str, str] = {}
        paginator = self.c.v1_cognito.get_paginator("list_users")
        for page in paginator.paginate(UserPoolId=self.v1.user_pool_id):
            for user in page.get("Users") or []:
                attrs = {a["Name"]: a["Value"] for a in user.get("Attributes") or []}
                email = normalize_email(attrs.get("email"))
                if email and attrs.get("sub"):
                    subs[email] = attrs["sub"]
        return subs

    def _v0_users(self) -> list[V0User]:
        users: list[V0User] = []
        paginator = self.c.v0_cognito.get_paginator("list_users")
        for page in paginator.paginate(UserPoolId=self.v0.user_pool_id):
            for user in page.get("Users") or []:
                attrs = {a["Name"]: a["Value"] for a in user.get("Attributes") or []}
                users.append(
                    V0User(
                        username=str(user.get("Username") or ""),
                        email=attrs.get("email"),
                        enabled=bool(user.get("Enabled", True)),
                    )
                )
        return users

    def _users(self, v1_emails: set[str]) -> set[str]:
        """Create the missing users; returns the emails that would be created."""
        rep = self.report["users"]
        v0_users = self._v0_users()
        rep.source = len(v0_users)
        plan = plan_users(v0_users, v1_emails)
        rep.existing = len(plan.existing)
        if plan.existing:
            rep.messages.append(
                "既存のためスキップ（作成もグループの変更もしない）: "
                + ", ".join(plan.existing)
            )
        for username, reason in plan.invalid:
            rep.skip(reason, username)
        for email in plan.create:
            if not self.apply:
                rep.created += 1
                continue
            try:
                delivery: dict[str, Any] = (
                    {"DesiredDeliveryMediums": ["EMAIL"]}
                    if self.invite
                    else {"MessageAction": "SUPPRESS"}
                )
                self.c.v1_cognito.admin_create_user(
                    UserPoolId=self.v1.user_pool_id,
                    Username=email,
                    UserAttributes=[
                        {"Name": "email", "Value": email},
                        {"Name": "email_verified", "Value": "true"},
                    ],
                    **delivery,
                )
            except ClientError as e:
                if _code(e) == "UsernameExistsException":
                    rep.existing += 1
                    rep.messages.append(f"既存のためスキップ: {email}")
                else:
                    rep.fail(f"{email}: {_code(e)}")
                continue
            try:
                self.c.v1_cognito.admin_add_user_to_group(
                    UserPoolId=self.v1.user_pool_id,
                    Username=email,
                    GroupName=MEMBER_GROUP,
                )
            except ClientError as e:
                rep.fail(f"{email}: {MEMBER_GROUP} に追加できません（{_code(e)}）")
                continue
            rep.created += 1
        return set(plan.create)

    # --- organization assets ------------------------------------------------------

    def _datasets(self) -> None:
        rep = self.report["datasets"]
        for item in self._scan_v0("datasets", rep):
            item_id = str(item["id"])
            if self._mapped_skip("dataset", item_id, rep):
                continue
            found = convert.dataset(
                item, src_bucket=self.v0.bucket, dst_bucket=self.v1.bucket
            )
            if found is None:
                rep.skip("not_in_s3", f"{item_id}（{item.get('s3_path')}）")
                continue
            plan: convert.DatasetPlan = found
            groups = self.mapping.groups("dataset", item_id)

            def write(
                plan: convert.DatasetPlan = plan, groups: frozenset[str] = groups
            ) -> None:
                self._copy_all(plan.copies)
                self.datasets.put_new(plan.dataset)
                if groups:
                    self.datasets.update_scope(
                        plan.dataset.id, add=groups, remove=frozenset()
                    )

            self._migrate("datasets", item_id, rep, write, shared=bool(groups))

    def _knowledge_bases(self) -> None:
        rep = self.report["knowledge_bases"]
        items = list(self._scan_v0("knowledge_bases", rep))
        if items and self.v0.region != self.v1.region:
            rep.warn(
                f"Bedrock のナレッジベースはリージョンごとの資源です。v1（{self.v1.region}）から"
                f"v0（{self.v0.region}）のナレッジベースは使えないため、v1 のリージョンに作り直して"
                "登録を更新してください"
            )
        for item in items:
            item_id = str(item["id"])
            if self._mapped_skip("knowledge_base", item_id, rep):
                continue
            kb = convert.knowledge_base(item)
            groups = self.mapping.groups("knowledge_base", item_id)

            def write(kb: Any = kb, groups: frozenset[str] = groups) -> None:
                self.kbs.put_new(kb)
                if groups:
                    self.kbs.update_scope(kb.id, add=groups, remove=frozenset())

            self._migrate("knowledge_bases", item_id, rep, write, shared=bool(groups))

    def _custom_persona_data(self) -> None:
        rep = self.report["custom_persona_data"]
        objects = self._v0_custom_objects()
        rep.source = len(objects)
        for key, modified in objects:
            name = convert.custom_name_from_key(key)
            if name is None:
                continue
            if self._mapped_skip("custom_persona_data", name, rep):
                continue
            try:
                record = convert.custom_persona_data(
                    name,
                    meta=None,
                    columns=[],
                    row_count=0,
                    registered_by=self.owner_resolver.default_sub,
                    registered_at=modified,
                )
            except CodedError:
                rep.skip("name_invalid", name)
                continue
            if self._exists("custom_persona_data", record.id):
                rep.existing += 1
                self._custom_ids[name] = record.id
                continue
            if not self.apply:
                rep.created += 1
                self._custom_ids[name] = record.id
                continue
            groups = self.mapping.groups("custom_persona_data", name)
            try:
                full = self._read_custom(key, name, modified, groups)
                self._copy(convert.Copy(key, full.s3_key))
                self.custom.put_new(full)
            except CustomPersonaDataConditionFailed:
                rep.existing += 1
            except CustomPersonaDataNameTaken:
                rep.skip("name_taken", f"{name}（v1 に同じ名前の別のデータがあります）")
                continue
            except Exception as e:  # noqa: BLE001 - reported per item, the run goes on
                rep.fail(f"{name}: {_describe(e)}")
                continue
            else:
                rep.created += 1
                if groups:
                    rep.shared += 1
            self._custom_ids[name] = record.id

    def _v0_custom_objects(self) -> list[tuple[str, str]]:
        """``(key, last modified ISO)`` of the v0 custom data files."""
        objects: list[tuple[str, str]] = []
        paginator = self.c.v0_s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.v0.bucket, Prefix=_CUSTOM_PREFIX):
            for obj in page.get("Contents") or []:
                key = str(obj["Key"])
                if convert.custom_name_from_key(key) is None:
                    continue
                modified = obj.get("LastModified")
                at = (
                    modified.astimezone().replace(tzinfo=None)
                    if isinstance(modified, datetime)
                    else datetime.now()
                )
                objects.append((key, at.isoformat()))
        return objects

    def _read_custom(
        self, key: str, name: str, modified: str, groups: frozenset[str]
    ) -> CustomPersonaData:
        """The full record: columns and rows read from the parquet file."""
        meta: Mapping[str, Any] | None = None
        meta_key = f"{_CUSTOM_PREFIX}{name}.meta.json"
        try:
            body = self.c.v0_s3.get_object(Bucket=self.v0.bucket, Key=meta_key)["Body"]
            meta = json.loads(body.read().decode("utf-8"))
        except ClientError as e:
            if _code(e) not in ("NoSuchKey", "404"):
                raise
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.parquet"
            self.c.v0_s3.download_file(self.v0.bucket, key, str(path))
            columns = list(pl.read_parquet_schema(path).keys())
            row_count = int(pl.scan_parquet(path).select(pl.len()).collect().item())
        record = convert.custom_persona_data(
            name,
            meta=meta,
            columns=columns,
            row_count=row_count,
            registered_by=self.owner_resolver.default_sub,
            registered_at=modified,
        )
        if groups:
            record = replace(record, shared_group_ids=groups)
        return record

    # --- personas and bindings -----------------------------------------------------

    def _personas(self) -> None:
        rep = self.report["personas"]
        for item in self._scan_v0("personas", rep):
            item_id = str(item["id"])
            if self._mapped_skip("persona", item_id, rep):
                continue
            try:
                plan = convert.persona(
                    item,
                    owner_sub=self.owner_resolver.owner("persona", item_id),
                    src_bucket=self.v0.bucket,
                    dst_bucket=self.v1.bucket,
                )
            except Exception as e:  # noqa: BLE001 - a broken v0 item is reported
                rep.fail(f"{item_id}: {_describe(e)}")
                continue
            for warning in plan.warnings:
                rep.warn(f"{item_id}: {warning}")

            def write(plan: convert.PersonaPlan = plan) -> None:
                self._copy_all(plan.copies)
                self.personas.put_new(plan.persona)

            self._migrate(
                "personas",
                item_id,
                rep,
                write,
                share=(ResourceType.PERSONA, "personas", "persona"),
            )

    def _bindings(self) -> None:
        rep = self.report["bindings"]
        rows: list[tuple[Any, str, str]] = []
        for item in self._scan_v0("persona_dataset_bindings", rep):
            rows.append((convert.dataset_binding(item), "datasets", "dataset"))
        for item in self._scan_v0("persona_kb_bindings", rep):
            rows.append((convert.kb_binding(item), "knowledge_bases", "knowledge_base"))
        # v0 never checked that a binding's persona exists, so a binding may
        # name a persona that only v1 has; only v0 personas get bindings
        v0_personas = {str(p["id"]) for p in self._scan_v0_quiet("personas")}
        existing: dict[str, set[str]] = {}
        for binding, target_kind, target_type in rows:
            label = f"{binding.persona_id} → {binding.target_key}"
            if binding.persona_id not in v0_personas:
                rep.skip("persona_not_from_v0", label)
                continue
            if self.mapping.skipped(
                "persona", binding.persona_id
            ) or self.mapping.skipped(target_type, binding.target_id):
                rep.skip("mapping_skip", label)
                continue
            if not self._present_or_exists("personas", binding.persona_id):
                rep.skip("persona_missing", label)
                continue
            if not self._present_or_exists(target_kind, binding.target_id):
                rep.skip("target_missing", label)
                continue
            if binding.persona_id not in existing:
                existing[binding.persona_id] = {
                    b.target_key
                    for b in self.bindings.list_for_persona(binding.persona_id)
                }
            keys = existing[binding.persona_id]
            if binding.target_key in keys:
                rep.existing += 1
                continue
            if binding.target_key == KB_TARGET_KEY and KB_TARGET_KEY in keys:
                rep.existing += 1
                continue
            if self.apply:
                try:
                    self.bindings.put(binding)
                except Exception as e:  # noqa: BLE001
                    rep.fail(f"{label}: {_describe(e)}")
                    continue
            keys.add(binding.target_key)
            rep.created += 1

    # --- surveys -------------------------------------------------------------------

    def _survey_templates(self) -> None:
        rep = self.report["survey_templates"]
        by_path = {
            str(f["file_path"]): f
            for f in self._scan_v0_quiet("uploaded_files")
            if f.get("file_type") == "survey_image" and f.get("file_path")
        }
        for item in self._scan_v0("survey_templates", rep):
            item_id = str(item["id"])
            if self._mapped_skip("survey_template", item_id, rep):
                continue
            try:
                plan = convert.template(
                    item,
                    owner_sub=self.owner_resolver.owner("survey_template", item_id),
                    files_by_path=by_path,
                    src_bucket=self.v0.bucket,
                    dst_bucket=self.v1.bucket,
                    now_epoch=self.now_epoch,
                )
            except Exception as e:  # noqa: BLE001
                rep.fail(f"{item_id}: {_describe(e)}")
                continue
            for warning in plan.warnings:
                rep.warn(f"{item_id}: {warning}")

            def write(plan: convert.TemplatePlan = plan) -> None:
                self._write_files(plan.files)
                self.templates.put_new(plan.template)

            self._migrate(
                "survey_templates",
                item_id,
                rep,
                write,
                share=(
                    ResourceType.SURVEY_TEMPLATE,
                    "survey_templates",
                    "survey_template",
                ),
            )

    def _surveys(self) -> None:
        rep = self.report["surveys"]
        if "custom_persona_data" not in self.kinds:
            self._custom_ids = self._existing_custom_ids()
        for item in self._scan_v0("surveys", rep):
            item_id = str(item["id"])
            if self._mapped_skip("survey", item_id, rep):
                continue
            try:
                plan = convert.survey(
                    item,
                    owner_sub=self.owner_resolver.owner("survey", item_id),
                    custom_ids=self._custom_ids,
                    src_bucket=self.v0.bucket,
                    dst_bucket=self.v1.bucket,
                )
            except Exception as e:  # noqa: BLE001
                rep.fail(f"{item_id}: {_describe(e)}")
                continue
            for warning in plan.warnings:
                rep.warn(f"{item_id}: {warning}")

            def write(plan: convert.SurveyPlan = plan) -> None:
                self._copy_all(plan.copies)
                self.surveys.put_new(plan.survey)

            self._migrate(
                "surveys",
                item_id,
                rep,
                write,
                share=(ResourceType.SURVEY, "surveys", "survey"),
            )

    def _existing_custom_ids(self) -> dict[str, str]:
        """Names of the v0 custom data whose migrated record is in v1."""
        ids: dict[str, str] = {}
        for key, modified in self._v0_custom_objects():
            name = convert.custom_name_from_key(key)
            if not name:
                continue
            try:
                record = convert.custom_persona_data(
                    name,
                    meta=None,
                    columns=[],
                    row_count=0,
                    registered_by="",
                    registered_at=modified,
                )
            except CodedError:
                continue
            if self._exists("custom_persona_data", record.id):
                ids[name] = record.id
        return ids

    # --- discussions ---------------------------------------------------------------

    def _discussions(self) -> None:
        rep = self.report["discussions"]
        files = {
            str(f["id"]): f
            for f in self._scan_v0_quiet("uploaded_files")
            if f.get("file_type") == "discussion_document"
        }
        for item in self._scan_v0("discussions", rep):
            item_id = str(item["id"])
            if self._mapped_skip("discussion", item_id, rep):
                continue
            try:
                plan = convert.discussion(
                    item,
                    owner_sub=self.owner_resolver.owner("discussion", item_id),
                    files=files,
                    src_bucket=self.v0.bucket,
                    dst_bucket=self.v1.bucket,
                    now_epoch=self.now_epoch,
                )
                size = DiscussionStore.item_size(plan.discussion.to_dict())
            except Exception as e:  # noqa: BLE001
                rep.fail(f"{item_id}: {_describe(e)}")
                continue
            if size > 400 * 1024:
                rep.fail(f"{item_id}: 本体が 400KB を超えます（{size} bytes）")
                continue
            for warning in plan.warnings:
                rep.warn(f"{item_id}: {warning}")

            def write(plan: convert.DiscussionPlan = plan) -> None:
                self._write_files(plan.files)
                if plan.messages:
                    self.discussions.write_messages(plan.messages)
                for report in plan.reports:
                    self._put_plain(
                        self.v1.table("discussion_reports"), report.to_dict()
                    )
                self.discussions.put_new(plan.discussion.to_dict())

            self._migrate(
                "discussions",
                item_id,
                rep,
                write,
                share=(ResourceType.DISCUSSION, "discussions", "discussion"),
            )

    # --- shared steps --------------------------------------------------------------

    def _migrate(
        self,
        kind: str,
        item_id: str,
        rep: KindReport,
        write: Callable[[], None],
        *,
        share: tuple[ResourceType, str, str] | None = None,
        shared: bool = False,
    ) -> None:
        """Write one item unless v1 has it, then set its departments.

        Args:
            share: ``(resource type, table key, mapping type)`` of a shareable
                body; its departments are set after the write.
            shared: The write itself sets a usage scope (counted only).
        """
        groups = self.mapping.groups(share[2], item_id) if share else frozenset()
        if self._exists(kind, item_id):
            rep.existing += 1
        elif not self.apply:
            rep.created += 1
        else:
            try:
                write()
            except SourceMissing as e:
                rep.fail(
                    f"{item_id}: {e}（対応表で skip にするか、v0 側を直してください）"
                )
                return
            except Exception as e:  # noqa: BLE001 - reported per item, the run goes on
                if self._exists(kind, item_id):
                    rep.existing += 1
                else:
                    rep.fail(f"{item_id}: {_describe(e)}")
                    return
            else:
                rep.created += 1
        self.present.add(kind, item_id)
        if shared:
            rep.shared += 1
        if share and groups:
            self._share(rep, share[0], share[1], item_id, groups)

    def _share(
        self,
        rep: KindReport,
        resource_type: ResourceType,
        table_key: str,
        item_id: str,
        groups: frozenset[str],
    ) -> None:
        assert resource_type in SHAREABLE_TYPES
        table = self.v1.table(table_key)
        if not self.apply:
            rep.shared += 1
            return
        current = self.shares.get_item(
            table, item_id, ["shared_group_ids", "owner_sub", "created_at"]
        )
        if current is None:
            rep.fail(f"{item_id}: 共有を設定する本体が v1 にありません")
            return
        now = frozenset(current.get("shared_group_ids") or ())
        if now == groups:
            return
        if now:
            rep.warn(
                f"{item_id}: v1 の共有先（{', '.join(sorted(now))}）が対応表と違うため変更しません"
            )
            return
        if not current.get("owner_sub"):
            rep.warn(f"{item_id}: 所有者がないため共有できません")
            return
        try:
            self.shares.write_shares(
                table,
                resource_type,
                item_id,
                expected=frozenset(),
                new=groups,
                created_at=str(current["created_at"]),
                owner_sub=str(current["owner_sub"]),
            )
        except Exception as e:  # noqa: BLE001
            rep.fail(f"{item_id}: 共有の設定に失敗（{_describe(e)}）")
            return
        rep.shared += 1

    def _mapped_skip(self, resource_type: str, item_id: str, rep: KindReport) -> bool:
        if self.mapping.skipped(resource_type, item_id):
            rep.skip("mapping_skip")
            return True
        return False

    def _present_or_exists(self, kind: str, item_id: str) -> bool:
        return self.present.has(kind, item_id) or self._exists(kind, item_id)

    def _exists(self, kind: str, item_id: str) -> bool:
        response = self.c.v1_dynamodb.get_item(
            TableName=self.v1.table(kind),
            Key={"id": {"S": item_id}},
            ProjectionExpression="#i",
            ExpressionAttributeNames={"#i": "id"},
            ConsistentRead=True,
        )
        return "Item" in response

    def _put_plain(self, table: str, data: Mapping[str, Any]) -> None:
        item = {
            k: self._serializer.serialize(v) for k, v in data.items() if v is not None
        }
        self.c.v1_dynamodb.put_item(TableName=table, Item=item)

    def _write_files(self, files: Sequence[convert.FilePlan]) -> None:
        for planned in files:
            self._copy(planned.copy)
            if self.files.get(planned.uploaded.id) is None:
                self.files.put(planned.uploaded)

    def _copy_all(self, copies: Sequence[convert.Copy]) -> None:
        for copy in copies:
            self._copy(copy)

    def _copy(self, copy: convert.Copy) -> None:
        """Copy one object unless v1 has it with the same size.

        Raises:
            SourceMissing: The object is not in the v0 bucket.
        """
        try:
            src = self.c.v0_s3.head_object(Bucket=self.v0.bucket, Key=copy.src_key)
        except ClientError as e:
            if _code(e) in ("404", "NoSuchKey", "NotFound"):
                raise SourceMissing(
                    f"s3://{self.v0.bucket}/{copy.src_key} がありません"
                ) from e
            raise
        try:
            dst = self.c.v1_s3.head_object(Bucket=self.v1.bucket, Key=copy.dst_key)
            if dst.get("ContentLength") == src.get("ContentLength"):
                return
        except ClientError as e:
            if _code(e) not in ("404", "NoSuchKey", "NotFound"):
                raise
        self.c.v1_s3.copy(
            {"Bucket": self.v0.bucket, "Key": copy.src_key},
            self.v1.bucket,
            copy.dst_key,
            SourceClient=self.c.v0_s3,
        )

    def _scan_v0(self, key: str, rep: KindReport) -> Iterator[dict[str, Any]]:
        items = list(self._scan_v0_quiet(key))
        rep.source += len(items)
        return iter(items)

    def _scan_v0_quiet(self, key: str) -> Iterator[dict[str, Any]]:
        paginator = self.c.v0_dynamodb.get_paginator("scan")
        for page in paginator.paginate(TableName=self.v0.table(key)):
            for item in page.get("Items") or []:
                yield {k: self._deserializer.deserialize(v) for k, v in item.items()}


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _describe(error: Exception) -> str:
    if isinstance(error, ClientError):
        return _code(error)
    code = getattr(error, "code", None)
    if code is not None:
        return f"{type(error).__name__}({getattr(code, 'value', code)})"
    return f"{type(error).__name__}: {error}"
