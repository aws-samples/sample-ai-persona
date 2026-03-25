"""
Insight data model for the AI Persona System.
"""

from dataclasses import dataclass, asdict
from typing import List, Dict, Any
import json


@dataclass
class Insight:
    """
    Represents an insight extracted from persona discussions.
    """

    category: str
    description: str
    supporting_messages: List[str]
    confidence_score: float

    def __post_init__(self):
        """
        Validate confidence score range.
        """
        if not 0.0 <= self.confidence_score <= 1.0:
            raise ValueError("Confidence score must be between 0.0 and 1.0")

    @classmethod
    def create_new(
        cls,
        category: str,
        description: str,
        supporting_messages: List[str],
        confidence_score: float,
    ) -> "Insight":
        """
        Create a new Insight instance with validation.
        """
        return cls(
            category=category,
            description=description,
            supporting_messages=supporting_messages,
            confidence_score=confidence_score,
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert Insight to dictionary for serialization.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Insight":
        """
        Create Insight instance from dictionary.
        """
        return cls(**data)

    def to_json(self) -> str:
        """
        Convert Insight to JSON string.
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "Insight":
        """
        Create Insight instance from JSON string.
        """
        data = json.loads(json_str)
        return cls.from_dict(data)
