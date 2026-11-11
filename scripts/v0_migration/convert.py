"""Pure conversions from v0 (v0.19.x) items to v1.x models.

Every function takes a plain v0 item (as read with ``TypeDeserializer``) and
returns v1 models plus the S3 copies they need. The v1 models' own readers
(``from_dict``, ``legacy_messages``, ``legacy_reports``) do the reading so the
shapes are never written by hand here. Nothing here does I/O.
"""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Mapping

from src.models.custom_persona_data import (
    CUSTOM_FILE_PREFIX,
    CUSTOM_PREFIX,
    CustomPersonaData,
    ExtraColumn,
    custom_file_key,
    normalize_name,
)
from src.models.dataset import Dataset
from src.models.discussion import (
    Discussion,
    legacy_messages,
    legacy_reports,
)
from src.models.discussion_report import DiscussionReport
from src.models.errors import ErrorCode
from src.models.knowledge_base import KnowledgeBase
from src.models.message import DiscussionMessage
from src.models.persona import Persona
from src.models.persona_binding import PersonaBinding
from src.models.survey import Survey, SurveyStatus
from src.models.survey_template import SurveyTemplate, TemplateImage
from src.models.uploaded_file import UploadedFile, is_uuid, upload_key

#: Namespace of the IDs made by the migration (stable across re-runs).
MIGRATION_NAMESPACE = uuid.UUID("6c1f3f0e-5d0b-4b5e-9a49-1f0a3f6c2d71")

_PERSONA_FIELDS = frozenset(f.name for f in fields(Persona))
_UNFINISHED = frozenset({"pending", "running", "queued"})


@dataclass(frozen=True)
class Copy:
    """One S3 object to copy from the v0 bucket to the v1 bucket."""

    src_key: str
    dst_key: str


@dataclass(frozen=True)
class Moved:
    """An ``s3://`` path of the v0 bucket rewritten to the v1 bucket."""

    path: str
    src_key: str
    dst_key: str


@dataclass(frozen=True)
class FilePlan:
    """An uploaded file to register in v1 and the object to copy for it."""

    uploaded: UploadedFile
    copy: Copy


@dataclass
class PersonaPlan:
    persona: Persona
    copies: list[Copy] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DiscussionPlan:
    discussion: Discussion
    messages: list[DiscussionMessage]
    reports: list[DiscussionReport]
    files: list[FilePlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TemplatePlan:
    template: SurveyTemplate
    files: list[FilePlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SurveyPlan:
    survey: Survey
    copies: list[Copy] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DatasetPlan:
    dataset: Dataset
    copies: list[Copy] = field(default_factory=list)


def plain(value: Any) -> Any:
    """``Decimal`` to ``int`` / ``float`` and sets to sorted lists, recursively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    return value


def rebucket(path: str | None, src_bucket: str, dst_bucket: str) -> Moved | None:
    """Rewrite an ``s3://{src_bucket}/key`` path to the v1 bucket.

    Returns:
        The new path and keys, or ``None`` for any other path (local paths,
        ``local://``, another bucket).
    """
    prefix = f"s3://{src_bucket}/"
    if not path or not path.startswith(prefix) or len(path) == len(prefix):
        return None
    key = path[len(prefix) :]
    return Moved(path=f"s3://{dst_bucket}/{key}", src_key=key, dst_key=key)


def stable_id(*parts: str) -> str:
    """A UUID derived from ``parts`` (the same on every run)."""
    return str(uuid.uuid5(MIGRATION_NAMESPACE, "\x1f".join(parts)))


# --- personas -------------------------------------------------------------------


def persona(
    item: Mapping[str, Any], *, owner_sub: str, src_bucket: str, dst_bucket: str
) -> PersonaPlan:
    """A v0 persona owned by ``owner_sub``, its avatar moved to the v1 bucket."""
    data = {k: v for k, v in plain(item).items() if k in _PERSONA_FIELDS}
    data["owner_sub"] = owner_sub
    data.pop("shared_group_ids", None)
    data["age"] = int(data.get("age") or 0)
    plan = PersonaPlan(persona=Persona.from_dict(data))
    avatar = plan.persona.avatar_path
    if avatar:
        moved = rebucket(avatar, src_bucket, dst_bucket)
        if moved is None:
            plan.warnings.append(f"avatar {avatar!r} is not in the v0 bucket; dropped")
            plan.persona = replace(plan.persona, avatar_path=None)
        else:
            plan.persona = replace(plan.persona, avatar_path=moved.path)
            plan.copies.append(Copy(moved.src_key, moved.dst_key))
    return plan


# --- uploaded files -------------------------------------------------------------


def _uploaded(
    *,
    file_id: str,
    file_type: str,
    owner_sub: str,
    original_filename: str,
    record: Mapping[str, Any] | None,
    mime_type: str | None,
    parent_type: str,
    parent_id: str,
    src_key: str,
    dst_bucket: str,
    now_epoch: int,
) -> FilePlan:
    record = record or {}
    key = upload_key(owner_sub, file_type, file_id, original_filename)
    uploaded_at = record.get("uploaded_at")
    uploaded = UploadedFile(
        id=file_id,
        owner_sub=owner_sub,
        file_type=file_type,
        s3_key=key,
        original_filename=original_filename,
        file_size=int(record.get("file_size") or 0),
        file_hash=str(record.get("file_hash") or ""),
        mime_type=str(
            mime_type
            or record.get("mime_type")
            or mimetypes.guess_type(original_filename)[0]
            or "application/octet-stream"
        ),
        uploaded_at=(
            datetime.fromisoformat(str(uploaded_at)) if uploaded_at else datetime.now()
        ),
        file_path=f"s3://{dst_bucket}/{key}",
        parent_type=parent_type,
        parent_id=parent_id,
        attached_at=now_epoch,
    )
    return FilePlan(uploaded=uploaded, copy=Copy(src_key, key))


def _original_name(record: Mapping[str, Any] | None, src_key: str) -> str:
    if record and record.get("original_filename"):
        return PurePosixPath(str(record["original_filename"])).name
    name = PurePosixPath(src_key).name
    prefix, sep, rest = name.partition("_")
    return rest if sep and rest and is_uuid(prefix) else name


# --- discussions ----------------------------------------------------------------


def discussion(
    item: Mapping[str, Any],
    *,
    owner_sub: str,
    files: Mapping[str, Mapping[str, Any]],
    src_bucket: str,
    dst_bucket: str,
    now_epoch: int,
) -> DiscussionPlan:
    """A v0 discussion split into the v1 body, messages and reports.

    Args:
        files: v0 ``UploadedFiles`` items by ID; an attached document whose
            file is found here (in the v0 bucket) is moved under the owner.
    """
    data = plain(item)
    body = Discussion.from_dict(data)
    messages = [
        DiscussionMessage.from_message(body.id, seq, message)
        for seq, message in enumerate(legacy_messages(data), start=1)
    ]
    reports = legacy_reports(data, body.id)
    body = replace(
        body,
        owner_sub=owner_sub,
        shared_group_ids=frozenset(),
        message_count=len(messages),
        report_count=len(reports),
    )
    plan = DiscussionPlan(discussion=body, messages=messages, reports=reports)
    for doc in body.documents or []:
        record = files.get(doc.id) if doc.id else None
        moved = (
            rebucket(record.get("file_path"), src_bucket, dst_bucket)
            if record
            else None
        )
        if record is None or moved is None or not is_uuid(doc.id):
            if doc.id:
                plan.warnings.append(f"document {doc.id} has no file in the v0 bucket")
            continue
        plan.files.append(
            _uploaded(
                file_id=doc.id,
                file_type="discussion_document",
                owner_sub=owner_sub,
                original_filename=_original_name(record, moved.src_key),
                record=record,
                mime_type=doc.mime_type,
                parent_type="discussion",
                parent_id=body.id,
                src_key=moved.src_key,
                dst_bucket=dst_bucket,
                now_epoch=now_epoch,
            )
        )
    return plan


# --- survey templates and surveys ----------------------------------------------


def template(
    item: Mapping[str, Any],
    *,
    owner_sub: str,
    files_by_path: Mapping[str, Mapping[str, Any]],
    src_bucket: str,
    dst_bucket: str,
    now_epoch: int,
) -> TemplatePlan:
    """A v0 template owned by ``owner_sub``, its images moved under the owner."""
    data = plain(item)
    images = data.pop("images", None) or []
    data.pop("shared_group_ids", None)
    data["owner_sub"] = owner_sub
    base = SurveyTemplate.from_dict({**data, "images": []})
    plan = TemplatePlan(template=base)
    kept: list[TemplateImage] = []
    for raw in images:
        image = TemplateImage.from_dict(raw)
        moved = rebucket(image.file_path, src_bucket, dst_bucket)
        if moved is None:
            plan.warnings.append(
                f"image {image.file_path!r} is not in the v0 bucket; dropped"
            )
            continue
        record = files_by_path.get(image.file_path)
        record_id = str(record.get("id") or "") if record else ""
        file_id = (
            record_id if is_uuid(record_id) else stable_id("image", image.file_path)
        )
        name = _original_name(record, moved.src_key)
        planned = _uploaded(
            file_id=file_id,
            file_type="survey_image",
            owner_sub=owner_sub,
            original_filename=name,
            record=record,
            mime_type=image.mime_type,
            parent_type="survey_template",
            parent_id=base.id,
            src_key=moved.src_key,
            dst_bucket=dst_bucket,
            now_epoch=now_epoch,
        )
        plan.files.append(planned)
        kept.append(replace(image, file_path=planned.uploaded.file_path))
    plan.template = replace(base, images=kept)
    return plan


def survey(
    item: Mapping[str, Any],
    *,
    owner_sub: str,
    custom_ids: Mapping[str, str],
    src_bucket: str,
    dst_bucket: str,
) -> SurveyPlan:
    """A v0 survey owned by ``owner_sub``.

    Args:
        custom_ids: v0 custom data name -> v1 ``CustomPersonaData.id`` of the
            records present in v1.
    """
    data = plain(item)
    data.pop("shared_group_ids", None)
    data["owner_sub"] = owner_sub
    for key in ("run_queue", "queued_at", "execution_arn"):
        data.pop(key, None)
    warnings: list[str] = []
    if data.get("status") in _UNFINISHED:
        warnings.append(f"status {data['status']} cannot resume in v1; marked error")
        data["status"] = SurveyStatus.ERROR.value
        data["error_code"] = ErrorCode.SURVEY_EXECUTION_INTERRUPTED.value
    source = str(data.get("datasource") or "")
    if source.startswith(CUSTOM_PREFIX):
        name = source[len(CUSTOM_PREFIX) :]
        data["datasource_name"] = name
        if name in custom_ids:
            data["datasource"] = f"{CUSTOM_PREFIX}{custom_ids[name]}"
        else:
            warnings.append(f"custom persona data {name!r} is not in v1; name kept")
    plan = SurveyPlan(survey=Survey.from_dict(data), warnings=warnings)
    result = plan.survey.s3_result_path
    if result:
        moved = rebucket(result, src_bucket, dst_bucket)
        if moved is None:
            plan.warnings.append(f"result {result!r} is not in the v0 bucket; dropped")
            plan.survey = replace(plan.survey, s3_result_path=None)
        else:
            plan.survey = replace(plan.survey, s3_result_path=moved.path)
            plan.copies.append(Copy(moved.src_key, moved.dst_key))
    return plan


# --- organization assets --------------------------------------------------------


def dataset(
    item: Mapping[str, Any], *, src_bucket: str, dst_bucket: str
) -> DatasetPlan | None:
    """A v0 dataset in the v1 bucket, or ``None`` when its file is not in S3."""
    data = plain(item)
    data.pop("shared_group_ids", None)
    moved = rebucket(data.get("s3_path"), src_bucket, dst_bucket)
    if moved is None:
        return None
    data["s3_path"] = moved.path
    data.setdefault("description", "")
    data["row_count"] = int(data.get("row_count") or 0)
    return DatasetPlan(Dataset.from_dict(data), [Copy(moved.src_key, moved.dst_key)])


def knowledge_base(item: Mapping[str, Any]) -> KnowledgeBase:
    """A v0 knowledge base registration (the Bedrock KB ID is kept)."""
    data = plain(item)
    data.pop("shared_group_ids", None)
    return KnowledgeBase.from_dict(data)


def _created(item: Mapping[str, Any]) -> datetime | None:
    value = item.get("created_at")
    return datetime.fromisoformat(str(value)) if value else None


def dataset_binding(item: Mapping[str, Any]) -> PersonaBinding:
    """A v0 ``PersonaDatasetBindings`` item as a ``PersonaBindings`` item."""
    data = plain(item)
    return PersonaBinding.for_dataset(
        str(data["persona_id"]),
        str(data["dataset_id"]),
        binding_keys={
            str(k): str(v) for k, v in (data.get("binding_keys") or {}).items()
        },
        now=_created(data),
    )


def kb_binding(item: Mapping[str, Any]) -> PersonaBinding:
    """A v0 ``PersonaKBBindings`` item as a ``PersonaBindings`` item."""
    data = plain(item)
    return PersonaBinding.for_kb(
        str(data["persona_id"]),
        str(data["kb_id"]),
        metadata_filters={
            str(k): str(v) for k, v in (data.get("metadata_filters") or {}).items()
        },
        now=_created(data),
    )


def custom_name_from_key(key: str) -> str | None:
    """The v0 custom data name of ``persona-dataset/custom/{name}.parquet``."""
    if not key.startswith(CUSTOM_FILE_PREFIX) or not key.endswith(".parquet"):
        return None
    name = key[len(CUSTOM_FILE_PREFIX) : -len(".parquet")]
    return name if name and "/" not in name else None


def custom_persona_data(
    name: str,
    *,
    meta: Mapping[str, Any] | None,
    columns: list[str],
    row_count: int,
    registered_by: str,
    registered_at: str,
) -> CustomPersonaData:
    """The v1 record of a v0 custom data file (ID derived from the name).

    Raises:
        CodedError: ``CUSTOM_PERSONA_DATA_NAME_INVALID`` (blank or too long).
    """
    clean = normalize_name(name)
    data_id = stable_id("custom_persona_data", clean)
    meta = meta or {}
    at = datetime.fromisoformat(registered_at)
    return CustomPersonaData(
        id=data_id,
        name=clean,
        s3_key=custom_file_key(data_id),
        columns=tuple(str(c) for c in columns),
        row_count=int(row_count),
        registered_by=registered_by,
        registered_at=at,
        standard_mapping={
            str(k): str(v) for k, v in (meta.get("standard_mapping") or {}).items()
        },
        extra_columns=tuple(
            ExtraColumn.from_dict(c) for c in meta.get("extra_columns") or []
        ),
        created_at=at,
    )
