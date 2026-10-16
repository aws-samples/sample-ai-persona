"""
Survey, InsightReport, and VisualAnalysisData data models for the Mass Survey feature.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from .errors import ErrorCode
from .survey_template import Question, SurveyTemplate, TemplateImage


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
    """

    id: str
    name: str
    description: str
    template_id: str
    persona_count: int
    filters: Optional[Dict[str, Any]]
    status: str  # "pending", "running", "completed", "error"
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
        """Create a new, unshared Survey with auto-generated ID and timestamps."""
        now = datetime.now()
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            template_id=template_id,
            persona_count=persona_count,
            filters=filters,
            status="pending",
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
        return result

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
