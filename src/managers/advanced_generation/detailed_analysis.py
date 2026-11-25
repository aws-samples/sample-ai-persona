"""The AI detailed analysis of one generation: its budget and its evidence (U6).

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package (logical-components.md).

- :class:`RunCodeBudget` implements the Models' ``RunCodeGate``: the Service
  tells it each ``run_code`` outcome and asks it before the next one. Five
  failures (``ADVANCED_RUN_CODE_MAX_FAILURES``) or a cancel that failed take
  ``run_code`` off and the generation goes on with the fixed functions
  (BR36.2, NFR1.27); a changed library or Parquet halts every tool, and the
  Manager then fails the generation (BR36.6, NFR5.55). A check that itself
  failed halts the same way (fail closed) but is not told as a tamper
  (review-01 R-03). A run that ended without ``emit_result`` (a look around,
  U6 addendum) is a call but no failure.
- :func:`filter_evidence` keeps, of what rests on ``run_code``, only what a
  successful call recounted: its ``tool_call_id``, a known ``segment_key`` and
  numbers the library recounted (population, sizes and their shares). The
  statistics and features the code passed never back an evidence, and a
  failed call's text never reaches the synthesis through a claim (BR36.4,
  NFR3.39, review-01 R-02).
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from ...models.analysis import (
    EmittedResult,
    Evidence,
    EvidenceKind,
    RunCodeOutcome,
    Segment,
)

#: Reasons ``run_code`` is taken off (``advanced_generation.run_code_disabled``).
REASON_MAX_FAILURES = "max_failures"
REASON_CANCEL_FAILED = "cancel_failed"
#: The isolated check itself failed; the generation fails, but no tamper.
REASON_VERIFY_FAILED = "verify_failed"
#: A hash to compare with is missing; run_code is never offered.
REASON_MISSING_HASH = "missing_hash"
_DIGITS = 3


class RunCodeBudget:
    """Counts the ``run_code`` outcomes of one generation (a ``RunCodeGate``)."""

    def __init__(
        self,
        *,
        max_failures: int,
        on_disabled: Optional[Callable[[str, int], None]] = None,
        on_tampered: Optional[Callable[[tuple[str, ...]], None]] = None,
        on_verify_failed: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Args:
            max_failures: Failures that take ``run_code`` off (5).
            on_disabled: Told once, with the reason and the failures.
            on_tampered: Told once, with what changed.
            on_verify_failed: Told once, when the check itself failed.
        """
        self._max_failures = int(max_failures)
        self._on_disabled = on_disabled
        self._on_tampered = on_tampered
        self._on_verify_failed = on_verify_failed
        self.failures = 0
        self.successes = 0
        #: U6 addendum: runs that ended without emit_result (no failure)
        self.no_results = 0
        self.disabled_reason: Optional[str] = None
        self.changed: tuple[str, ...] = ()
        self.verify_failed = False
        self._halted = False

    def allows_run_code(self) -> bool:
        return self.disabled_reason is None and not self._halted

    def halted(self) -> bool:
        return self._halted

    @property
    def calls(self) -> int:
        """Outcomes recorded (successes, failures and runs without a result)."""
        return self.successes + self.failures + self.no_results

    def record(self, outcome: RunCodeOutcome) -> None:
        if outcome.verify_failed:
            if not self._halted:
                self._halted = True
                self.verify_failed = True
                if self._on_verify_failed is not None:
                    self._on_verify_failed()
            return
        if outcome.tampered:
            if not self._halted:
                self._halted = True
                self.changed = tuple(outcome.changed)
                if self._on_tampered is not None:
                    self._on_tampered(self.changed)
            return
        if outcome.ok:
            self.successes += 1
            return
        if outcome.no_result:
            # U6 addendum: a look around is a call, never a failure
            self.no_results += 1
            return
        self.failures += 1
        if self.disabled_reason is not None:
            return
        if outcome.cancel_failed:
            self._disable(REASON_CANCEL_FAILED)
        elif self.failures >= self._max_failures:
            self._disable(REASON_MAX_FAILURES)

    def _disable(self, reason: str) -> None:
        self.disabled_reason = reason
        if self._on_disabled is not None:
            self._on_disabled(reason, self.failures)


def _numbers(value: Any) -> Iterator[float]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _numbers(item)


def _known_numbers(result: EmittedResult) -> set[float]:
    # Only what the library recounted: never the code's statistics / features
    known = {round(n, _DIGITS) for n in _numbers(result.recounted())}
    if result.population:
        known |= {
            round(int(s["size"]) / result.population, _DIGITS) for s in result.segments
        }
    return known


def _evidence_holds(evidence: Evidence, result: Optional[EmittedResult]) -> bool:
    if result is None or evidence.quote is not None or not evidence.statistic:
        return False
    numbers = list(_numbers(evidence.statistic))
    known = _known_numbers(result)
    return bool(numbers) and all(round(n, _DIGITS) in known for n in numbers)


def _counted_size(
    segment: Segment,
    result: EmittedResult,
    evidence: Sequence[Evidence],
    taken: set[str],
) -> Optional[Evidence]:
    """Evidence made of the layer's recounted size and the population."""
    key = segment.segment_key
    size = result.size_of(key) if key is not None else None
    if size is None or not result.population:
        return None
    ident = f"count-{segment.id}"
    while ident in taken:
        ident += "_"
    return Evidence(
        id=ident,
        source_id=evidence[0].source_id,
        location=str(segment.tool_call_id),
        kind=EvidenceKind.ATTRIBUTE,
        claim=f"この層の人数は {size}（母数 {result.population}）",
        statistic={"value": size, "population": result.population},
    )


def filter_evidence(
    segments: Sequence[Segment],
    evidence: Sequence[Evidence],
    runs: Mapping[str, Optional[EmittedResult]],
) -> tuple[list[Segment], list[Evidence]]:
    """Leave only what a successful ``run_code`` recounted (BR36.4, NFR3.39).

    Args:
        segments: Segments from the analysis agent.
        evidence: Evidence from the analysis agent.
        runs: Every ``run_code`` call: its ``EmittedResult``, ``None`` failed.

    Returns:
        The segments and the evidence to keep. A segment left without
        evidence is dropped; segments and evidence of the fixed tools are
        kept as they are.
    """
    kept_evidence = [
        e
        for e in evidence
        if e.location not in runs or _evidence_holds(e, runs[e.location])
    ]
    kept_ids = {e.id for e in kept_evidence}
    known_ids = {e.id for e in evidence}
    kept_segments: list[Segment] = []
    for segment in segments:
        call = segment.tool_call_id
        counted: Optional[EmittedResult] = None
        if call in runs:
            result = runs[call] if call is not None else None
            key = segment.segment_key
            if result is None or key is None or result.size_of(key) is None:
                continue
            counted = result
        ids = tuple(
            i for i in segment.evidence_ids if i in kept_ids or i not in known_ids
        )
        if not ids and counted is not None and evidence:
            # The code's own numbers were not accepted as evidence, but the
            # layer's size is the library's count: that is evidence enough.
            fallback = _counted_size(segment, counted, evidence, known_ids | kept_ids)
            if fallback is not None:
                kept_evidence.append(fallback)
                kept_ids.add(fallback.id)
                ids = (fallback.id,)
        if not ids:
            continue
        kept_segments.append(
            segment if ids == segment.evidence_ids else segment.update(evidence_ids=ids)
        )
    return kept_segments, kept_evidence
