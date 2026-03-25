"""
Persona data model for the AI Persona System.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Any
import json
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
        )

    def update(
        self,
        name: str = None,
        age: int = None,
        occupation: str = None,
        background: str = None,
        values: List[str] = None,
        pain_points: List[str] = None,
        goals: List[str] = None,
    ) -> "Persona":
        """
        Update persona fields and return a new instance with updated timestamp.
        """
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
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert Persona to dictionary for serialization.
        """
        data = asdict(self)
        # Convert datetime objects to ISO format strings
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Persona":
        """
        Create Persona instance from dictionary.
        """
        # Convert ISO format strings back to datetime objects
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        return cls(**data)

    def to_json(self) -> str:
        """
        Convert Persona to JSON string.
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "Persona":
        """
        Create Persona instance from JSON string.
        """
        data = json.loads(json_str)
        return cls.from_dict(data)
