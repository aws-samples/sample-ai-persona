"""Run the migration: read v0, convert, and (with ``apply``) write v1.

Every write goes through the v1 stores' conditional writes, so an item that
is already in v1 is counted as migrated and left as it is; a run stopped
half-way is simply run again. Children are written before their parent
(messages and reports before the discussion body, objects before the item
that points at them), so a parent present in v1 means it is complete.
Departments and usage scopes are set after the body, and again on a re-run
while the body still has none: shares with ``ResourceShareStore.write_shares``
(body column and index in one transaction), usage scopes with the stores'
``update_scope``.

The owners in the mapping are checked before anything is written, users
included. One broken v0 item is reported as a failure and the run goes on.

v0 is only read: ``Scan``, ``GetObject`` / ``HeadObject`` and ``ListUsers``.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import polars as pl
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from src.models.authz import ResourceType, Role
from src.models.custom_persona_data import (
    CUSTOM_FILE_PREFIX,
    CustomPersonaData,
    reservation_id,
)
from src.models.errors import CodedError
from src.models.sharing import SHAREABLE_TYPES
from src.services.cognito_groups_service import GROUP_PREFIX
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
from .users import UserPlan, V0User, normalize_email, plan_users

logger = logging.getLogger(__name__)

#: Group every re-created user joins (admins are switched by hand afterwards).
MEMBER_GROUP = f"role:{Role.MEMBER.value}"
#: Largest discussion body DynamoDB accepts.
_MAX_ITEM_BYTES = 400 * 1024

#: ``update_scope(id, add=..., remove=...)`` of a usage-scoped store.
ScopeUpdate = Callable[..., Any]


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


@dataclass(frozen=True)
class V1User:
    """A user of the v1 pool (the ``Username`` is what the admin APIs take)."""

    username: str
    sub: str


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
        self._v0_items: dict[str, list[dict[str, Any]]] = {}
        self._v0_custom: list[tuple[str, str]] | None = None
        self._v0_persona_ids: set[str] = set()
        self._binding_keys: dict[str, set[str]] = {}
        self._images: dict[str, dict[str, Any]] = {}
        self._documents: dict[str, dict[str, Any]] = {}
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
                department is not usable. Raised before anything is written
                (users included); after the users step only when a mapped
                owner could not be created.
        """
        self._check_departments()
        v1_users = self._v1_users()
        user_plan: UserPlan | None = None
        if "users" in self.kinds:
            user_plan = self._plan_users(set(v1_users))
        planned = set(user_plan.create) if user_plan else set()
        # Checked before any write: a typo here must not create users first
        self.resolver = self._resolver(v1_users, planned)
        if user_plan is not None:
            self._users(user_plan, v1_users)
            if self.apply and user_plan.create:
                self.resolver = self._resolver(self._v1_users(), set())
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

    def _resolver(
        self, users: Mapping[str, V1User], planned: set[str]
    ) -> OwnerResolver:
        return OwnerResolver(
            {email: user.sub for email, user in users.items()},
            default_email=self.default_owner,
            mapping=self.mapping,
            planned_emails=planned,
        )

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
        missing = sorted(g for g in wanted if f"{GROUP_PREFIX}{g}" not in names)
        if missing:
            raise MappingError(
                "departments not in the v1 pool (create group:<slug> first): "
                + ", ".join(missing)
            )

    def _v1_users(self) -> dict[str, V1User]:
        """Normalized email -> user of every user in the v1 pool."""
        users: dict[str, V1User] = {}
        paginator = self.c.v1_cognito.get_paginator("list_users")
        for page in paginator.paginate(UserPoolId=self.v1.user_pool_id):
            for user in page.get("Users") or []:
                attrs = {a["Name"]: a["Value"] for a in user.get("Attributes") or []}
                email = normalize_email(attrs.get("email"))
                if email and attrs.get("sub"):
                    users[email] = V1User(
                        str(user.get("Username") or email), attrs["sub"]
                    )
        return users

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

    def _plan_users(self, v1_emails: set[str]) -> UserPlan:
        rep = self.report["users"]
        v0_users = self._v0_users()
        rep.source = len(v0_users)
        return plan_users(v0_users, v1_emails)

    def _users(self, plan: UserPlan, v1_users: Mapping[str, V1User]) -> None:
        """Create the missing users and give ``role:member`` to group-less ones.

        A v1 user with the same email keeps its groups; only one with no group
        at all (left by an earlier run that stopped between the two calls)
        gets ``role:member``.
        """
        rep = self.report["users"]
        rep.existing = len(plan.existing)
        untouched: list[str] = []
        for email in plan.existing:
            try:
                self._member_if_groupless(
                    rep, email, v1_users[email].username, untouched
                )
            except Exception as e:  # noqa: BLE001 - reported per user, the run goes on
                rep.fail(f"{email}: {_describe(e)}")
        if untouched:
            rep.messages.append(
                "既存のためスキップ（作成もグループの変更もしない）: "
                + ", ".join(untouched)
            )
        for username, reason in plan.invalid:
            rep.skip(reason, username)
        for email in plan.create:
            if not self.apply:
                rep.created += 1
                continue
            self._create_user(rep, email)

    def _member_if_groupless(
        self, rep: KindReport, email: str, username: str, untouched: list[str]
    ) -> None:
        groups = self.c.v1_cognito.admin_list_groups_for_user(
            UserPoolId=self.v1.user_pool_id, Username=username
        ).get("Groups")
        if groups:
            untouched.append(email)
            return
        if self.apply:
            self._add_member(username)
            rep.messages.append(
                f"グループがないため {MEMBER_GROUP} を付けました: {email}"
            )
        else:
            rep.messages.append(
                f"グループがないため {MEMBER_GROUP} を付ける予定: {email}"
            )

    def _create_user(self, rep: KindReport, email: str) -> None:
        delivery: dict[str, Any] = (
            {"DesiredDeliveryMediums": ["EMAIL"]}
            if self.invite
            else {"MessageAction": "SUPPRESS"}
        )
        try:
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
            return
        try:
            self._add_member(email)
        except ClientError as e:
            rep.fail(
                f"{email}: {MEMBER_GROUP} に追加できません（{_code(e)}）。"
                "再実行すると追加します"
            )
            return
        rep.created += 1

    def _add_member(self, username: str) -> None:
        self.c.v1_cognito.admin_add_user_to_group(
            UserPoolId=self.v1.user_pool_id, Username=username, GroupName=MEMBER_GROUP
        )

    # --- organization assets ------------------------------------------------------

    def _datasets(self) -> None:
        rep = self.report["datasets"]
        self._each(rep, self._scan_v0("datasets", rep), self._dataset)

    def _dataset(self, item: Mapping[str, Any]) -> None:
        rep = self.report["datasets"]
        item_id = str(item["id"])
        if self._mapped_skip("dataset", item_id, rep):
            return
        found = convert.dataset(
            item, src_bucket=self.v0.bucket, dst_bucket=self.v1.bucket
        )
        if found is None:
            rep.skip("not_in_s3", f"{item_id}（{item.get('s3_path')}）")
            return
        plan: convert.DatasetPlan = found

        def write() -> None:
            self._copy_all(plan.copies)
            self.datasets.put_new(plan.dataset)

        self._migrate(
            "datasets",
            item_id,
            rep,
            write,
            scope=(self.datasets.update_scope, "dataset"),
        )

    def _knowledge_bases(self) -> None:
        rep = self.report["knowledge_bases"]
        items = self._scan_v0("knowledge_bases", rep)
        if items and self.v0.region != self.v1.region:
            rep.warn(
                f"Bedrock のナレッジベースはリージョンごとの資源です。v1（{self.v1.region}）から"
                f"v0（{self.v0.region}）のナレッジベースは使えないため、v1 のリージョンに作り直して"
                "登録を更新してください"
            )
        self._each(rep, items, self._knowledge_base)

    def _knowledge_base(self, item: Mapping[str, Any]) -> None:
        rep = self.report["knowledge_bases"]
        item_id = str(item["id"])
        if self._mapped_skip("knowledge_base", item_id, rep):
            return
        kb = convert.knowledge_base(item)
        self._migrate(
            "knowledge_bases",
            item_id,
            rep,
            lambda: self.kbs.put_new(kb),
            scope=(self.kbs.update_scope, "knowledge_base"),
        )

    def _custom_persona_data(self) -> None:
        rep = self.report["custom_persona_data"]
        objects = self._v0_custom_objects()
        rep.source = len(objects)
        for key, modified in objects:
            try:
                self._custom_one(rep, key, modified)
            except MappingError:
                raise
            except Exception as e:  # noqa: BLE001 - reported per item, the run goes on
                rep.fail(f"{key}: {_describe(e)}")

    def _custom_one(self, rep: KindReport, key: str, modified: str) -> None:
        name = convert.custom_name_from_key(key)
        if name is None or self._mapped_skip("custom_persona_data", name, rep):
            return
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
            return
        groups = self.mapping.groups("custom_persona_data", name)
        if self._exists("custom_persona_data", record.id):
            rep.existing += 1
            self._custom_ids[name] = record.id
            self._scope(
                rep, "custom_persona_data", record.id, groups, self.custom.update_scope
            )
            return
        holder = self._name_holder(record.name)
        if holder is not None and holder != record.id:
            # Checked before the copy so nothing is left in the v1 bucket
            rep.skip("name_taken", f"{name}（v1 に同じ名前の別のデータがあります）")
            return
        if not self.apply:
            rep.created += 1
            if groups:
                rep.shared += 1
            self._custom_ids[name] = record.id
            return
        try:
            full = self._read_custom(key, name, modified, groups)
            self._copy(convert.Copy(key, full.s3_key))
            self.custom.put_new(full)
        except CustomPersonaDataConditionFailed:
            rep.existing += 1
        except CustomPersonaDataNameTaken:
            rep.skip("name_taken", f"{name}（v1 に同じ名前の別のデータがあります）")
            return
        else:
            rep.created += 1
            if groups:
                rep.shared += 1
        self._custom_ids[name] = record.id

    def _name_holder(self, name: str) -> str | None:
        """ID of the v1 record holding ``name``, or ``None``."""
        response = self.c.v1_dynamodb.get_item(
            TableName=self.v1.table("custom_persona_data"),
            Key={"id": {"S": reservation_id(name)}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return str(item.get("data_id", {}).get("S") or "")

    def _v0_custom_objects(self) -> list[tuple[str, str]]:
        """``(key, last modified as naive UTC ISO)`` of the v0 custom data files."""
        if self._v0_custom is not None:
            return self._v0_custom
        objects: list[tuple[str, str]] = []
        paginator = self.c.v0_s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.v0.bucket, Prefix=CUSTOM_FILE_PREFIX
        ):
            for obj in page.get("Contents") or []:
                key = str(obj["Key"])
                if convert.custom_name_from_key(key) is None:
                    continue
                modified = obj.get("LastModified")
                at = (
                    modified
                    if isinstance(modified, datetime)
                    else datetime.now(timezone.utc)
                )
                if at.tzinfo is not None:
                    at = at.astimezone(timezone.utc).replace(tzinfo=None)
                objects.append((key, at.isoformat()))
        self._v0_custom = objects
        return objects

    def _read_custom(
        self, key: str, name: str, modified: str, groups: frozenset[str]
    ) -> CustomPersonaData:
        """The full record: columns and rows read from the parquet file."""
        meta: Mapping[str, Any] | None = None
        meta_key = f"{CUSTOM_FILE_PREFIX}{name}.meta.json"
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
        self._each(rep, self._scan_v0("personas", rep), self._persona)

    def _persona(self, item: Mapping[str, Any]) -> None:
        rep = self.report["personas"]
        item_id = str(item["id"])
        if self._mapped_skip("persona", item_id, rep):
            return
        plan = convert.persona(
            item,
            owner_sub=self.owner_resolver.owner("persona", item_id),
            src_bucket=self.v0.bucket,
            dst_bucket=self.v1.bucket,
        )
        for warning in plan.warnings:
            rep.warn(f"{item_id}: {warning}")

        def write() -> None:
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
        # v0 never checked that a binding's persona exists, so a binding may
        # name a persona that only v1 has; only v0 personas get bindings
        self._v0_persona_ids = {str(p["id"]) for p in self._v0_scan("personas")}
        self._binding_keys = {}
        self._each(
            rep, self._scan_v0("persona_dataset_bindings", rep), self._dataset_binding
        )
        self._each(rep, self._scan_v0("persona_kb_bindings", rep), self._kb_binding)

    def _dataset_binding(self, item: Mapping[str, Any]) -> None:
        self._binding(convert.dataset_binding(item), "datasets", "dataset")

    def _kb_binding(self, item: Mapping[str, Any]) -> None:
        self._binding(convert.kb_binding(item), "knowledge_bases", "knowledge_base")

    def _binding(self, binding: Any, target_kind: str, target_type: str) -> None:
        rep = self.report["bindings"]
        label = f"{binding.persona_id} → {binding.target_key}"
        if binding.persona_id not in self._v0_persona_ids:
            rep.skip("persona_not_from_v0", label)
            return
        if self.mapping.skipped("persona", binding.persona_id) or self.mapping.skipped(
            target_type, binding.target_id
        ):
            rep.skip("mapping_skip", label)
            return
        if not self._present_or_exists("personas", binding.persona_id):
            rep.skip("persona_missing", label)
            return
        if not self._present_or_exists(target_kind, binding.target_id):
            rep.skip("target_missing", label)
            return
        keys = self._binding_keys.get(binding.persona_id)
        if keys is None:
            keys = {
                b.target_key for b in self.bindings.list_for_persona(binding.persona_id)
            }
            self._binding_keys[binding.persona_id] = keys
        if binding.target_key in keys:
            rep.existing += 1
            return
        if self.apply:
            self.bindings.put(binding)
        keys.add(binding.target_key)
        rep.created += 1

    # --- surveys -------------------------------------------------------------------

    def _survey_templates(self) -> None:
        rep = self.report["survey_templates"]
        self._images = {
            str(f["file_path"]): f
            for f in self._v0_scan("uploaded_files")
            if f.get("file_type") == "survey_image" and f.get("file_path")
        }
        self._each(rep, self._scan_v0("survey_templates", rep), self._survey_template)

    def _survey_template(self, item: Mapping[str, Any]) -> None:
        rep = self.report["survey_templates"]
        item_id = str(item["id"])
        if self._mapped_skip("survey_template", item_id, rep):
            return
        plan = convert.template(
            item,
            owner_sub=self.owner_resolver.owner("survey_template", item_id),
            files_by_path=self._images,
            src_bucket=self.v0.bucket,
            dst_bucket=self.v1.bucket,
            now_epoch=self.now_epoch,
        )
        for warning in plan.warnings:
            rep.warn(f"{item_id}: {warning}")

        def write() -> None:
            self._write_files(plan.files)
            self.templates.put_new(plan.template)

        self._migrate(
            "survey_templates",
            item_id,
            rep,
            write,
            share=(ResourceType.SURVEY_TEMPLATE, "survey_templates", "survey_template"),
        )

    def _surveys(self) -> None:
        rep = self.report["surveys"]
        if "custom_persona_data" not in self.kinds:
            self._custom_ids = self._existing_custom_ids()
        self._each(rep, self._scan_v0("surveys", rep), self._survey)

    def _survey(self, item: Mapping[str, Any]) -> None:
        rep = self.report["surveys"]
        item_id = str(item["id"])
        if self._mapped_skip("survey", item_id, rep):
            return
        plan = convert.survey(
            item,
            owner_sub=self.owner_resolver.owner("survey", item_id),
            custom_ids=self._custom_ids,
            src_bucket=self.v0.bucket,
            dst_bucket=self.v1.bucket,
        )
        for warning in plan.warnings:
            rep.warn(f"{item_id}: {warning}")

        def write() -> None:
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
        self._documents = {
            str(f["id"]): f
            for f in self._v0_scan("uploaded_files")
            if f.get("file_type") == "discussion_document"
        }
        self._each(rep, self._scan_v0("discussions", rep), self._discussion)

    def _discussion(self, item: Mapping[str, Any]) -> None:
        rep = self.report["discussions"]
        item_id = str(item["id"])
        if self._mapped_skip("discussion", item_id, rep):
            return
        plan = convert.discussion(
            item,
            owner_sub=self.owner_resolver.owner("discussion", item_id),
            files=self._documents,
            src_bucket=self.v0.bucket,
            dst_bucket=self.v1.bucket,
            now_epoch=self.now_epoch,
        )
        size = DiscussionStore.item_size(plan.discussion.to_dict())
        if size > _MAX_ITEM_BYTES:
            rep.fail(f"{item_id}: 本体が 400KB を超えます（{size} bytes）")
            return
        for warning in plan.warnings:
            rep.warn(f"{item_id}: {warning}")

        def write() -> None:
            self._write_files(plan.files)
            if plan.messages:
                self.discussions.write_messages(plan.messages)
            for report in plan.reports:
                self._put_plain(self.v1.table("discussion_reports"), report.to_dict())
            self.discussions.put_new(plan.discussion.to_dict())

        self._migrate(
            "discussions",
            item_id,
            rep,
            write,
            share=(ResourceType.DISCUSSION, "discussions", "discussion"),
        )

    # --- shared steps --------------------------------------------------------------

    def _each(
        self,
        rep: KindReport,
        items: Iterable[Mapping[str, Any]],
        handle: Callable[[Mapping[str, Any]], None],
    ) -> None:
        """Handle every item; a broken one is a failure and the run goes on."""
        for item in items:
            try:
                handle(item)
            except MappingError:
                raise
            except SourceMissing as e:
                rep.fail(
                    f"{item.get('id', '(id なし)')}: {e}"
                    "（対応表で skip にするか、v0 側を直してください）"
                )
            except Exception as e:  # noqa: BLE001 - reported per item, the run goes on
                rep.fail(f"{item.get('id', '(id なし)')}: {_describe(e)}")

    def _migrate(
        self,
        kind: str,
        item_id: str,
        rep: KindReport,
        write: Callable[[], Any],
        *,
        share: tuple[ResourceType, str, str] | None = None,
        scope: tuple[ScopeUpdate, str] | None = None,
    ) -> None:
        """Write one item unless v1 has it, then set its departments.

        The departments are set also when the item was already in v1 and has
        none yet, so a run stopped between the two steps is completed by the
        next one.

        Args:
            share: ``(resource type, table key, mapping type)`` of a shareable
                body (``ResourceShares``).
            scope: ``(update_scope, mapping type)`` of an organization asset
                (usage scope on the body only).
        """
        if self._exists(kind, item_id):
            rep.existing += 1
        elif not self.apply:
            rep.created += 1
        else:
            try:
                write()
            except SourceMissing:
                raise
            except Exception:
                if not self._exists(kind, item_id):
                    raise
                rep.existing += 1
            else:
                rep.created += 1
        self.present.add(kind, item_id)
        if share:
            groups = self.mapping.groups(share[2], item_id)
            if groups:
                self._share(rep, share[0], share[1], item_id, groups)
        if scope:
            groups = self.mapping.groups(scope[1], item_id)
            self._scope(rep, kind, item_id, groups, scope[0])

    def _current_groups(self, table_key: str, item_id: str) -> dict[str, Any] | None:
        return self.shares.get_item(
            self.v1.table(table_key),
            item_id,
            ["shared_group_ids", "owner_sub", "created_at"],
        )

    def _share(
        self,
        rep: KindReport,
        resource_type: ResourceType,
        table_key: str,
        item_id: str,
        groups: frozenset[str],
    ) -> None:
        assert resource_type in SHAREABLE_TYPES
        if not self.apply:
            rep.shared += 1
            return
        current = self._current_groups(table_key, item_id)
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
                self.v1.table(table_key),
                resource_type,
                item_id,
                expected=frozenset(),
                new=groups,
                created_at=str(current["created_at"]),
                owner_sub=str(current["owner_sub"]),
            )
        except Exception as e:  # noqa: BLE001
            rep.fail(
                f"{item_id}: 共有の設定に失敗（{_describe(e)}）。再実行すると設定します"
            )
            return
        rep.shared += 1

    def _scope(
        self,
        rep: KindReport,
        table_key: str,
        item_id: str,
        groups: frozenset[str],
        update: ScopeUpdate,
    ) -> None:
        """Set the usage scope of an organization asset that has none yet."""
        if not groups:
            return
        if not self.apply:
            rep.shared += 1
            return
        current = self._current_groups(table_key, item_id)
        if current is None:
            rep.fail(f"{item_id}: 利用範囲を設定する本体が v1 にありません")
            return
        now = frozenset(current.get("shared_group_ids") or ())
        if now == groups:
            return
        if now:
            rep.warn(
                f"{item_id}: v1 の利用範囲（{', '.join(sorted(now))}）が対応表と違うため変更しません"
            )
            return
        try:
            result = update(item_id, add=groups, remove=frozenset())
        except Exception as e:  # noqa: BLE001
            rep.fail(
                f"{item_id}: 利用範囲の設定に失敗（{_describe(e)}）。再実行すると設定します"
            )
            return
        if result is False:
            rep.fail(f"{item_id}: 利用範囲を設定できません（組織の資産ではありません）")
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

    def _scan_v0(self, key: str, rep: KindReport) -> list[dict[str, Any]]:
        """Every item of a v0 table, counted as the source of ``rep``."""
        items = self._v0_scan(key)
        rep.source += len(items)
        return items

    def _v0_scan(self, key: str) -> list[dict[str, Any]]:
        """Every item of a v0 table (each table is scanned once per run)."""
        if key not in self._v0_items:
            items: list[dict[str, Any]] = []
            paginator = self.c.v0_dynamodb.get_paginator("scan")
            for page in paginator.paginate(TableName=self.v0.table(key)):
                for item in page.get("Items") or []:
                    items.append(
                        {k: self._deserializer.deserialize(v) for k, v in item.items()}
                    )
            self._v0_items[key] = items
        return self._v0_items[key]


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _describe(error: Exception) -> str:
    if isinstance(error, ClientError):
        return _code(error)
    code = getattr(error, "code", None)
    if code is not None:
        return f"{type(error).__name__}({getattr(code, 'value', code)})"
    return f"{type(error).__name__}: {error}"
