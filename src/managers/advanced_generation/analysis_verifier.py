"""Checks of an analysis result against the tool outputs (BR8.1〜BR8.3, contract C8).

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package. It is a pure decision: no model, no Service.

- BR8.1: a segment's ``size`` must appear in the output of a tool call the
  segment rests on (its definition's ``tool_call_id`` and its evidence's
  locations), and its ``share`` must equal ``size / population`` at three
  decimals. Otherwise the segment is *unverified*: its values are not shown,
  the generation goes on.
- BR8.2: the sizes of the non-overlapping segments must not add up to more
  than the population; for ``cluster`` they must add up to it exactly.
- BR8.3: every persona must stand for one of the segments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from ...models.analysis import AnalysisMethod, Evidence, Segment
from ...models.errors import ErrorCode

_SHARE_DIGITS = 3


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of the verification.

    Attributes:
        passed: Whether the result may be shown.
        failure: ``GENERATION_VERIFICATION_FAILED`` when it did not pass.
        unverified_segment_ids: Segments whose size / share is not shown.
        reason: Fixed word for the log (``sum``, ``cluster_sum``,
            ``persona_without_segment``, ``population``).
    """

    passed: bool
    failure: Optional[ErrorCode] = None
    unverified_segment_ids: tuple[str, ...] = ()
    reason: Optional[str] = None


def _numbers(value: Any) -> Iterator[int]:
    """Every integer inside a tool output (bools left out)."""
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        yield value
    elif isinstance(value, float) and value.is_integer():
        yield int(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _numbers(item)


def _fail(reason: str, unverified: Iterable[str] = ()) -> VerificationResult:
    return VerificationResult(
        passed=False,
        failure=ErrorCode.GENERATION_VERIFICATION_FAILED,
        unverified_segment_ids=tuple(unverified),
        reason=reason,
    )


class AnalysisVerifier:
    """The contract checks of BR8.1〜BR8.3."""

    @staticmethod
    def verify(
        *,
        method: AnalysisMethod,
        segments: Sequence[Segment],
        evidence: Sequence[Evidence],
        persona_segment_ids: Sequence[Optional[str]],
        population: int,
        outputs: Mapping[str, Any],
    ) -> VerificationResult:
        """Check the result (BR8.1〜BR8.3).

        Args:
            method: The method that ran.
            segments: Segments from the analysis agent.
            evidence: Evidence from the analysis agent.
            persona_segment_ids: The segment each synthesized persona names.
            population: ``PrepareReport.rows_read`` of the table.
            outputs: Tool call ID -> structured tool output.
        """
        unverified = [
            s.id
            for s in segments
            if not AnalysisVerifier._is_backed(s, evidence, population, outputs)
        ]
        if population < 1:
            return _fail("population", unverified)
        if sum(s.size for s in segments if not s.overlaps) > population:
            return _fail("sum", unverified)
        if method is AnalysisMethod.CLUSTER and (
            sum(s.size for s in segments) != population
        ):
            return _fail("cluster_sum", unverified)
        known = {s.id for s in segments}
        if any(sid not in known for sid in persona_segment_ids):
            return _fail("persona_without_segment", unverified)
        return VerificationResult(passed=True, unverified_segment_ids=tuple(unverified))

    @staticmethod
    def _is_backed(
        segment: Segment,
        evidence: Sequence[Evidence],
        population: int,
        outputs: Mapping[str, Any],
    ) -> bool:
        calls = {str(segment.definition.get("spec", {}).get("tool_call_id", ""))}
        calls |= {e.location for e in evidence if e.id in set(segment.evidence_ids)}
        seen = {n for call in calls if call in outputs for n in _numbers(outputs[call])}
        if segment.size not in seen or population < 1:
            return False
        expected = round(segment.size / population, _SHARE_DIGITS)
        return round(segment.share, _SHARE_DIGITS) == expected
