"""
Survey, InsightReport, and VisualAnalysisData data models for the Mass Survey feature.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Dict, List, Optional
import uuid

from .errors import ErrorCode
from .survey_template import Question, SurveyTemplate, TemplateImage


class SurveyStatus(StrEnum):
    """Where a survey is in its life (functional-spec.md ch.5).

    ``pending`` is only held by records written before the move to Step
    Functions; new surveys start ``queued`` so the two are told apart (BR8.1).
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    PENDING = "pending"


#: ``run_queue`` of queued and running surveys; the partition key of the
#: sparse ``RunQueueIndex`` (only these surveys appear in it, NFR3.10).
RUN_QUEUE_ACTIVE = "active"

#: Fewest personas a survey runs with, and so the fewest rows a dataset may be
#: registered or extracted with (FR4.1; was the batch inference minimum 100).
MIN_SURVEY_PERSONAS = 10


def _read_time(value: Any) -> Optional[datetime]:
    """An ISO 8601 timestamp read back from a record, or ``None``."""
    if not value:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _read_int(value: Any) -> Optional[int]:
    """A number read back from DynamoDB (``Decimal``) as an int, or ``None``."""
    return None if value is None else int(value)


@dataclass
class InsightReport:
    """インサイトレポート"""

    id: str
    survey_id: str
    content: str
    created_at: datetime

    @classmethod
    def create_new(cls, survey_id: str, content: str) -> "InsightReport":
        """Create a new InsightReport with auto-generated ID and timestamp."""
        return cls(
            id=str(uuid.uuid4()),
            survey_id=survey_id,
            content=content,
            created_at=datetime.now(),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert InsightReport to dictionary for serialization."""
        return {
            "id": self.id,
            "survey_id": self.survey_id,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InsightReport":
        """Create InsightReport instance from dictionary."""
        return cls(
            id=data["id"],
            survey_id=data["survey_id"],
            content=data["content"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )


@dataclass
class VisualAnalysisData:
    """ビジュアル分析用データ"""

    multiple_choice_charts: List[Dict[str, Any]] = field(default_factory=list)
    scale_rating_charts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class PersonaStatistics:
    """調査対象ペルソナの統計データ"""

    total_count: int
    sex_distribution: Dict[str, int]
    age_distribution: Dict[str, int]
    occupation_distribution: Dict[str, int]
    region_distribution: Dict[str, int]
    prefecture_distribution: Dict[str, int]
    marital_status_distribution: Dict[str, int]
    age_stats: Dict[str, Any]  # min, max, average


@dataclass(frozen=True)
class TemplateSnapshot:
    """The template as it was when a survey started (U7 BR2.7).

    Analysis, display and the insight prompt use this copy, so editing or
    deleting the template later does not change past results. Images are
    references only; the files stay attached to the template (BR2.8).
    """

    name: str
    questions: tuple[Question, ...]
    images: tuple[TemplateImage, ...] = ()

    @classmethod
    def from_template(cls, template: SurveyTemplate) -> "TemplateSnapshot":
        """Copy the name, questions and image references of ``template``."""
        return cls(
            name=template.name,
            questions=tuple(template.questions),
            images=tuple(template.images or ()),
        )

    def image(self, image_id: str) -> Optional[TemplateImage]:
        """The image reference with ``image_id``, or ``None`` (BR2.8)."""
        return next((img for img in self.images if img.id == image_id), None)

    def to_dict(self) -> Dict[str, Any]:
        """Convert the snapshot to a dictionary (no empty image list)."""
        data: Dict[str, Any] = {
            "name": self.name,
            "questions": [q.to_dict() for q in self.questions],
        }
        if self.images:
            data["images"] = [img.to_dict() for img in self.images]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemplateSnapshot":
        """Create a snapshot from :meth:`to_dict` output (DynamoDB values too)."""
        return cls(
            name=str(data["name"]),
            questions=tuple(Question.from_dict(q) for q in data.get("questions", [])),
            images=tuple(TemplateImage.from_dict(i) for i in data.get("images", [])),
        )


@dataclass
class Survey:
    """アンケート実行インスタンス

    U7（約束 C8、BR1.1、BR2.7）: ``owner_sub`` は実行した人（古い結果にはない）、
    ``shared_group_ids`` は ``ResourceSharing`` だけが書く共有先、``template_snapshot``
    は開始時のテンプレートの写し、``datasource_name`` は開始時のカスタムペルソナ
    データの名前（表示用）。

    Step Functions への移行（entities.md）: ``run_queue`` と ``queued_at`` は
    queued・running の間だけ持つ（疎な索引のキー）。``execution_arn``・
    ``started_at``・``model_id`` は実行を始めたときに書く。``start_attempts`` と
    ``finish_attempts`` は開始と完了のまとめの一時的な失敗の回数、
    ``start_blocked_since`` は権限・設定の失敗で開始できなくなった時刻、
    ``failed_count`` は取れなかった回答の件数。
    """

    id: str
    name: str
    description: str
    template_id: str
    persona_count: int
    filters: Optional[Dict[str, Any]]
    status: str  # a SurveyStatus value
    s3_result_path: Optional[str]
    insight_report: Optional[InsightReport]
    created_at: datetime
    updated_at: datetime
    #: Failure reason as an ``ErrorCode`` value (not a message). Batch execution
    #: fails in a background thread, so the reason has to survive until a later
    #: GET renders it; storing ``str(e)`` here used to leak S3 paths, role ARNs
    #: and botocore text to the screen. The wording is resolved at display time
    #: by ``web/error_messages.py``.
    error_code: Optional[str] = None
    #: Values for interpolating the wording template. Only carries what is safe
    #: to show a user (counts, limits) -- never IDs, paths or SDK text.
    error_context: Optional[Dict[str, Any]] = None
    datasource: Optional[str] = None
    owner_sub: Optional[str] = None
    shared_group_ids: frozenset[str] = field(default_factory=frozenset)
    template_snapshot: Optional[TemplateSnapshot] = None
    datasource_name: Optional[str] = None
    run_queue: Optional[str] = None
    queued_at: Optional[datetime] = None
    execution_arn: Optional[str] = None
    started_at: Optional[datetime] = None
    start_attempts: int = 0
    finish_attempts: int = 0
    model_id: Optional[str] = None
    failed_count: Optional[int] = None
    start_blocked_since: Optional[datetime] = None

    @classmethod
    def create_new(
        cls,
        name: str,
        description: str,
        template_id: str,
        persona_count: int,
        filters: Optional[Dict[str, Any]] = None,
        datasource: Optional[str] = None,
        owner_sub: Optional[str] = None,
        template_snapshot: Optional[TemplateSnapshot] = None,
        datasource_name: Optional[str] = None,
    ) -> "Survey":
        """Create a new, unshared, queued Survey with ID and timestamps (FR3.1)."""
        now = datetime.now()
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            template_id=template_id,
            persona_count=persona_count,
            filters=filters,
            status=SurveyStatus.QUEUED.value,
            run_queue=RUN_QUEUE_ACTIVE,
            queued_at=datetime.now(timezone.utc),
            s3_result_path=None,
            insight_report=None,
            created_at=now,
            updated_at=now,
            datasource=datasource,
            owner_sub=owner_sub,
            template_snapshot=template_snapshot,
            datasource_name=datasource_name,
        )

    @property
    def template_name(self) -> Optional[str]:
        """The template name of the snapshot; ``None`` for surveys before v1.0.0."""
        return self.template_snapshot.name if self.template_snapshot else None

    def to_dict(self) -> Dict[str, Any]:
        """Convert Survey to dictionary for serialization."""
        result: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "template_id": self.template_id,
            "persona_count": self.persona_count,
            "filters": self.filters,
            "status": self.status,
            "s3_result_path": self.s3_result_path,
            "insight_report": self.insight_report.to_dict()
            if self.insight_report
            else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if self.error_code is not None:
            result["error_code"] = self.error_code
        if self.error_context is not None:
            result["error_context"] = self.error_context
        if self.datasource is not None:
            result["datasource"] = self.datasource
        if self.owner_sub:
            result["owner_sub"] = self.owner_sub
        if self.shared_group_ids:
            result["shared_group_ids"] = sorted(self.shared_group_ids)
        if self.template_snapshot is not None:
            result["template_snapshot"] = self.template_snapshot.to_dict()
        if self.datasource_name:
            result["datasource_name"] = self.datasource_name
        result.update(self._execution_dict())
        return result

    def _execution_dict(self) -> Dict[str, Any]:
        """The queue and execution fields that are set (zero counts left out)."""
        data: Dict[str, Any] = {}
        for name in ("run_queue", "execution_arn", "model_id"):
            value = getattr(self, name)
            if value:
                data[name] = value
        for name in ("queued_at", "started_at", "start_blocked_since"):
            moment = getattr(self, name)
            if moment is not None:
                data[name] = moment.isoformat()
        for name in ("start_attempts", "finish_attempts"):
            if getattr(self, name):
                data[name] = getattr(self, name)
        if self.failed_count is not None:
            data["failed_count"] = self.failed_count
        return data

    @property
    def is_legacy_in_flight(self) -> bool:
        """Running or pending without an execution or a queue entry (BR8.1).

        Only records started by batch inference look like this; they can never
        finish now.
        """
        return (
            self.status in (SurveyStatus.RUNNING, SurveyStatus.PENDING)
            and not self.execution_arn
            and not self.run_queue
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Survey":
        """Create Survey instance from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            template_id=data["template_id"],
            persona_count=data["persona_count"],
            filters=data.get("filters"),
            status=data["status"],
            s3_result_path=data.get("s3_result_path"),
            insight_report=InsightReport.from_dict(data["insight_report"])
            if data.get("insight_report")
            else None,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            error_code=cls._read_error_code(data),
            error_context=data.get("error_context"),
            datasource=data.get("datasource"),
            owner_sub=str(data["owner_sub"]) if data.get("owner_sub") else None,
            shared_group_ids=frozenset(data.get("shared_group_ids") or ()),
            template_snapshot=TemplateSnapshot.from_dict(data["template_snapshot"])
            if data.get("template_snapshot")
            else None,
            datasource_name=data.get("datasource_name") or None,
            run_queue=data.get("run_queue") or None,
            queued_at=_read_time(data.get("queued_at")),
            execution_arn=data.get("execution_arn") or None,
            started_at=_read_time(data.get("started_at")),
            start_attempts=_read_int(data.get("start_attempts")) or 0,
            finish_attempts=_read_int(data.get("finish_attempts")) or 0,
            model_id=data.get("model_id") or None,
            failed_count=_read_int(data.get("failed_count")),
            start_blocked_since=_read_time(data.get("start_blocked_since")),
        )

    @property
    def datasource_label(self) -> str:
        """The persona data name to show (BR2.10): Nemotron or the stored name."""
        if not self.datasource or self.datasource == "nemotron":
            return "Nemotron"
        if self.datasource_name:
            return self.datasource_name
        return self.datasource.split(":", 1)[-1]

    @staticmethod
    def _read_error_code(data: Dict[str, Any]) -> Optional[str]:
        """Read the failure reason, tolerating records written before #118.

        Older records hold the raw exception text in ``error_message``. That text
        must never reach the screen, so it is discarded and only the fact of the
        failure is kept as a generic code.
        """
        code = data.get("error_code")
        if isinstance(code, str) and code:
            return code
        if data.get("error_message"):
            return ErrorCode.SURVEY_EXECUTION_FAILED.value
        return None


@dataclass(frozen=True)
class SurveyDetail:
    """What the result detail screen shows for one reader (U7 BR2.7〜BR2.11).

    Attributes:
        survey: The survey.
        manageable: The reader may delete, share and generate the report.
        template_name: From the snapshot; for an old survey without one, the
            template's name when the reader may read it, else ``None``.
        analysis_available: The questions are known (snapshot, or a readable
            template for an old survey); otherwise only the CSV is offered.
    """

    survey: Survey
    manageable: bool
    template_name: Optional[str]
    analysis_available: bool
