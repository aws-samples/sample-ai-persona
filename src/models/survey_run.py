"""Value types of one survey execution on Step Functions (entities.md).

- ``SurveyRunItem``: one line of the Distributed Map input (one persona's
  request). It carries either the encoded Anthropic Messages body or, for a
  survey with images or a large body, the ``s3://`` URI of a body stored
  apart (BR6.3).
- ``SurveyRunOutcome``: what the results of a finished execution add up to.
  It is built once when the execution ends and never stored.
"""

import json
from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass(frozen=True)
class SurveyRunItem:
    """One persona's request in the execution input (JSONL line).

    Attributes:
        record_id: The persona's ``uuid``; ties the result to the CSV row.
        model_id: Inference profile the Task invokes.
        body_json: The request body, already encoded as one-line JSON (no
            images, small enough for the item). Encoded once and reused for
            the size check and the line.
        body_s3_uri: Where the body is stored (images, or too large for the item).

    Unlike the stored models it has no ``to_dict``: its only form outside is
    the JSONL line, and building a dict would encode the body a second time.

    Raises:
        ValueError: Not exactly one of ``body_json`` and ``body_s3_uri`` is
            given, or ``body_json`` spans lines (it would split the JSONL line).
    """

    record_id: str
    model_id: str
    body_json: Optional[str] = None
    body_s3_uri: Optional[str] = None

    def __post_init__(self) -> None:
        if (self.body_json is None) == (self.body_s3_uri is None):
            raise ValueError("exactly one of body_json and body_s3_uri is required")
        if self.body_json is not None and (
            "\n" in self.body_json or "\r" in self.body_json
        ):
            raise ValueError("body_json must be one line")

    def to_json_line(self) -> str:
        """The JSONL line; the encoded body is embedded as is (no second encoding)."""
        head = (
            f'{{"record_id": {json.dumps(self.record_id, ensure_ascii=False)}, '
            f'"model_id": {json.dumps(self.model_id, ensure_ascii=False)}, '
        )
        if self.body_json is not None:
            return f'{head}"body": {self.body_json}}}'
        uri = json.dumps(self.body_s3_uri, ensure_ascii=False)
        return f'{head}"body_s3_uri": {uri}}}'


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
