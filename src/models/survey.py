"""
Survey, InsightReport, and VisualAnalysisData data models for the Mass Survey feature.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid


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


@dataclass
class Survey:
    """アンケート実行インスタンス"""

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
    error_message: Optional[str] = None

    @classmethod
    def create_new(
        cls,
        name: str,
        description: str,
        template_id: str,
        persona_count: int,
        filters: Optional[Dict[str, Any]] = None,
    ) -> "Survey":
        """Create a new Survey with auto-generated ID and timestamps."""
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
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert Survey to dictionary for serialization."""
        return {
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
            "error_message": self.error_message,
        }

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
            error_message=data.get("error_message"),
        )
