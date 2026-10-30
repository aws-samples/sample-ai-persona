"""Value types of one survey execution on Step Functions (entities.md).

- ``SurveyRunItem``: one line of the Distributed Map input (one persona's
  request). It carries either the Anthropic Messages body itself or, for a
  survey with images, the ``s3://`` URI of a body stored apart (BR6.3).
- ``SurveyRunOutcome``: what the results of a finished execution add up to.
  It is built once when the execution ends and never stored.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass(frozen=True)
class SurveyRunItem:
    """One persona's request in the execution input (JSONL line).

    Attributes:
        record_id: The persona's ``uuid``; ties the result to the CSV row.
        model_id: Inference profile the Task invokes.
        body: The request body (surveys without images).
        body_s3_uri: Where the body is stored (surveys with images).

    Raises:
        ValueError: Not exactly one of ``body`` and ``body_s3_uri`` is given.
    """

    record_id: str
    model_id: str
    body: Optional[Dict[str, Any]] = None
    body_s3_uri: Optional[str] = None

    def __post_init__(self) -> None:
        if (self.body is None) == (self.body_s3_uri is None):
            raise ValueError("exactly one of body and body_s3_uri is required")

    def to_dict(self) -> Dict[str, Any]:
        """The JSONL line (the absent one of body / body_s3_uri is left out)."""
        data: Dict[str, Any] = {"record_id": self.record_id, "model_id": self.model_id}
        if self.body is not None:
            data["body"] = self.body
        else:
            data["body_s3_uri"] = self.body_s3_uri
        return data


@dataclass(frozen=True)
class SurveyRunOutcome:
    """The summary of a finished execution's results (BR4.2).

    Attributes:
        execution_status: ``SUCCEEDED``, ``FAILED``, ``TIMED_OUT`` or ``ABORTED``.
        answers: record_id -> question ID -> answer as written to the CSV.
        failed_record_ids: Personas without a usable answer: failed items,
            responses that do not fit the schema and personas missing from the
            results, each counted once.
        total: Personas the execution was started for.
        failed_by_error: Failed items per Step Functions error name (logs).
        invalid_count: Successful items whose response did not fit (logs).
        missing_count: Personas found in no result file (logs).

    Raises:
        ValueError: ``total`` is not positive.
    """

    execution_status: str
    answers: Mapping[str, Mapping[str, str]]
    failed_record_ids: frozenset[str]
    total: int
    failed_by_error: Mapping[str, int] = field(default_factory=dict)
    invalid_count: int = 0
    missing_count: int = 0

    def __post_init__(self) -> None:
        if self.total < 1:
            raise ValueError("total must be at least 1")

    @property
    def failed_count(self) -> int:
        """How many personas have no usable answer."""
        return len(self.failed_record_ids)
