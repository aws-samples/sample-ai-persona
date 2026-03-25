"""
SurveyTemplate and Question data models for the Mass Survey feature.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid


@dataclass
class TemplateImage:
    """テンプレートに添付する画像"""

    id: str
    name: str  # ユーザーが付けるラベル名
    file_path: str  # S3パスまたはローカルパス
    mime_type: str
    original_filename: str

    @classmethod
    def create_new(
        cls, name: str, file_path: str, mime_type: str, original_filename: str
    ) -> "TemplateImage":
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            file_path=file_path,
            mime_type=mime_type,
            original_filename=original_filename,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "file_path": self.file_path,
            "mime_type": self.mime_type,
            "original_filename": self.original_filename,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemplateImage":
        return cls(
            id=data["id"],
            name=data["name"],
            file_path=data["file_path"],
            mime_type=data["mime_type"],
            original_filename=data["original_filename"],
        )


@dataclass
class Question:
    """アンケートの個別質問"""

    id: str
    text: str
    question_type: str  # "multiple_choice", "free_text", "scale_rating"
    options: List[str] = field(default_factory=list)
    scale_min: int = 1
    scale_max: int = 5
    allow_multiple: bool = False  # 複数回答を許可するか（選択式のみ）
    max_selections: int = 0  # 最大選択数（0=無制限、選択式+複数回答時のみ有効）

    @classmethod
    def create_multiple_choice(
        cls,
        text: str,
        options: List[str],
        allow_multiple: bool = False,
        max_selections: int = 0,
    ) -> "Question":
        """選択式質問を作成する"""
        return cls(
            id=str(uuid.uuid4()),
            text=text,
            question_type="multiple_choice",
            options=options,
            allow_multiple=allow_multiple,
            max_selections=max_selections,
        )

    @classmethod
    def create_free_text(cls, text: str) -> "Question":
        """自由記述質問を作成する"""
        return cls(
            id=str(uuid.uuid4()),
            text=text,
            question_type="free_text",
        )

    @classmethod
    def create_scale_rating(cls, text: str) -> "Question":
        """スケール評価質問を作成する"""
        return cls(
            id=str(uuid.uuid4()),
            text=text,
            question_type="scale_rating",
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert Question to dictionary for serialization."""
        d = {
            "id": self.id,
            "text": self.text,
            "question_type": self.question_type,
            "options": self.options,
            "scale_min": int(self.scale_min),
            "scale_max": int(self.scale_max),
        }
        if self.allow_multiple:
            d["allow_multiple"] = True
            if self.max_selections > 0:
                d["max_selections"] = self.max_selections
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Question":
        """Create Question instance from dictionary."""
        return cls(
            id=data["id"],
            text=data["text"],
            question_type=data["question_type"],
            options=data.get("options", []),
            scale_min=int(data.get("scale_min", 1)),
            scale_max=int(data.get("scale_max", 5)),
            allow_multiple=bool(data.get("allow_multiple", False)),
            max_selections=int(data.get("max_selections", 0)),
        )


@dataclass
class SurveyTemplate:
    """アンケートテンプレート"""

    id: str
    name: str
    questions: List[Question]
    created_at: datetime
    updated_at: datetime
    images: List[TemplateImage] = field(default_factory=list)

    @classmethod
    def create_new(
        cls,
        name: str,
        questions: List[Question],
        images: Optional[List[TemplateImage]] = None,
    ) -> "SurveyTemplate":
        """Create a new SurveyTemplate with auto-generated ID and timestamps."""
        now = datetime.now()
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            questions=questions,
            created_at=now,
            updated_at=now,
            images=images or [],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert SurveyTemplate to dictionary for serialization."""
        d: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "questions": [q.to_dict() for q in self.questions],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if self.images:
            d["images"] = [img.to_dict() for img in self.images]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SurveyTemplate":
        """Create SurveyTemplate instance from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            questions=[Question.from_dict(q) for q in data["questions"]],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            images=[TemplateImage.from_dict(img) for img in data.get("images", [])],
        )
