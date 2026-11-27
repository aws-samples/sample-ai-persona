"""
Insight data model for the AI Persona System.
"""

import re
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Tuple

# The extraction prompt asks for "観察: …／仮説: …", or "観察: …" alone for a finding
# stated outright; models also use ASCII "/", a line break or a full-width colon.
_OBSERVATION_HYPOTHESIS = re.compile(
    r"^\s*観察\s*[:：]\s*(?P<observation>[^／/\s].*?)\s*[／/]?\s*仮説\s*[:：]\s*(?P<hypothesis>.+?)\s*$",
    re.S,
)
_OBSERVATION_ONLY = re.compile(
    r"^\s*観察\s*[:：]\s*(?P<observation>[^／/\s].*?)\s*$", re.S
)


def split_observation_hypothesis(
    description: str,
) -> Optional[Tuple[str, Optional[str]]]:
    """Split a "観察: …／仮説: …" description into (observation, hypothesis).

    A "観察: …" description gives (observation, None). Returns None for any
    other text (including insights saved before the format existed), so callers
    show the description as it is.
    """
    text = description or ""
    match = _OBSERVATION_HYPOTHESIS.match(text)
    if match:
        return match.group("observation"), match.group("hypothesis")
    match = _OBSERVATION_ONLY.match(text)
    if match:
        return match.group("observation"), None
    return None


@dataclass
class Insight:
    """
    Represents an insight extracted from persona discussions.
    """

    category: str
    description: str
    supporting_messages: List[str]
    confidence_score: float

    def __post_init__(self) -> None:
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
