"""
Persona data model for the AI Persona System.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import List, Dict, Any
import uuid


@dataclass
class Persona:
    """
    Represents an AI persona generated from N1 interview data.
    """

    id: str
    name: str
    age: int
    occupation: str
    background: str
    values: List[str]
    pain_points: List[str]
    goals: List[str]
    created_at: datetime
    updated_at: datetime
    gender: str | None = field(default=None)
    country: str | None = field(default=None)
    city: str | None = field(default=None)
    tags: List[str] = field(default_factory=list)
    generation_log: list[dict[str, str]] | None = field(default=None)
    generation_context: dict[str, Any] | None = field(default=None)

    @classmethod
    def create_new(
        cls,
        name: str,
        age: int,
        occupation: str,
        background: str,
        values: List[str],
        pain_points: List[str],
        goals: List[str],
        gender: str | None = None,
        country: str | None = None,
        city: str | None = None,
        tags: List[str] | None = None,
    ) -> "Persona":
        """
        Create a new Persona instance with auto-generated ID and timestamps.
        """
        now = datetime.now()
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            age=age,
            occupation=occupation,
            background=background,
            values=values,
            pain_points=pain_points,
            goals=goals,
            created_at=now,
            updated_at=now,
            gender=gender,
            country=country,
            city=city,
            tags=tags if tags is not None else [],
        )

    def update(
        self,
        name: str | None = None,
        age: int | None = None,
        occupation: str | None = None,
        background: str | None = None,
        values: List[str] | None = None,
        pain_points: List[str] | None = None,
        goals: List[str] | None = None,
        gender: str | None = None,
        country: str | None = None,
        city: str | None = None,
        tags: List[str] | None = None,
    ) -> "Persona":
        """
        Update persona fields and return a new instance with updated timestamp.

        gender/country/city は3値のセマンティクスを持つ:
        - None: 変更なし（既存値を保持）
        - 空文字列 "": 値をクリアして None にする
        - それ以外の文字列: その値に更新
        これにより編集画面から属性を未設定に戻せる。
        """

        def _resolve_clearable(
            new_value: str | None, current: str | None
        ) -> str | None:
            """None=変更なし、空文字=クリア、それ以外=更新。"""
            if new_value is None:
                return current
            stripped = new_value.strip()
            return stripped if stripped else None

        return Persona(
            id=self.id,
            name=name if name is not None else self.name,
            age=age if age is not None else self.age,
            occupation=occupation if occupation is not None else self.occupation,
            background=background if background is not None else self.background,
            values=values if values is not None else self.values,
            pain_points=pain_points if pain_points is not None else self.pain_points,
            goals=goals if goals is not None else self.goals,
            created_at=self.created_at,
            updated_at=datetime.now(),
            gender=_resolve_clearable(gender, self.gender),
            country=_resolve_clearable(country, self.country),
            city=_resolve_clearable(city, self.city),
            tags=tags if tags is not None else self.tags,
            generation_log=self.generation_log,
            generation_context=self.generation_context,
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert Persona to dictionary for serialization.
        """
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        # Omit None optional fields to save DynamoDB capacity
        for key in (
            "gender",
            "country",
            "city",
            "generation_log",
            "generation_context",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        # Omit empty tags list as well
        if not data.get("tags"):
            data.pop("tags", None)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Persona":
        """
        Create Persona instance from dictionary.
        """
        parsed = {**data}
        parsed["created_at"] = datetime.fromisoformat(parsed["created_at"])
        parsed["updated_at"] = datetime.fromisoformat(parsed["updated_at"])
        return cls(**parsed)
