"""Checks of an analysis result against the tool outputs.

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package. It is a pure decision: no model, no Service.

- A segment's ``size`` must appear in the output of a tool call the
  segment rests on (its definition's ``tool_call_id`` and its evidence's
  locations), and its ``share`` must equal ``size / population`` at three
  decimals. Otherwise the segment is *unverified*: its values are not shown,
  the generation goes on.
- The sizes of the non-overlapping segments must not add up to more
  than the population; for ``cluster`` they must add up to it exactly.
- Every persona must stand for one of the segments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from ...models.analysis import (
    AnalysisMethod,
    EmittedResult,
    Evidence,
    EvidenceKind,
    Segment,
)
from ...models.errors import ErrorCode

_SHARE_DIGITS = 3
# The key items a document-only persona must rest on.
_KEY_KINDS = frozenset({EvidenceKind.VALUE, EvidenceKind.PAIN, EvidenceKind.GOAL})


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


#: Keys that hold a number of people, wherever they are in a tool output.
_COUNT_KEYS = frozenset({"size", "count", "population", "rows", "rows_read"})


def _is_count_label(label: object) -> bool:
    """``count`` and ``count(column)`` count rows; ``count(distinct ..)`` does not."""
    text = str(label)
    return text == "count" or (
        text.startswith("count(") and not text.startswith("count(distinct")
    )


def _as_int(value: Any) -> Iterator[int]:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        yield value
    elif isinstance(value, float) and value.is_integer():
        yield int(value)


def _numbers(value: Any) -> Iterator[int]:
    """The integers of a tool output that count people.

    A size is backed only by a value that is a count: a cell of a ``count`` /
    ``count(column)`` column, or a value under a key such as ``size`` or
    ``population``. An average, a sum or any other number that merely equals
    the size does not back it.
    """
    if isinstance(value, Mapping):
        columns, rows = value.get("columns"), value.get("rows")
        tabular = isinstance(rows, list)
        if isinstance(rows, list):
            names = columns if isinstance(columns, list) else []
            at = [i for i, c in enumerate(names) if _is_count_label(c)]
            for row in rows:
                if isinstance(row, (list, tuple)):
                    for i in at:
                        if i < len(row):
                            yield from _as_int(row[i])
                elif isinstance(row, Mapping):
                    for key, cell in row.items():
                        if _is_count_label(key):
                            yield from _as_int(cell)
        for key, item in value.items():
            if tabular and key in ("columns", "rows"):
                continue
            if key in _COUNT_KEYS:
                yield from _as_int(item)
            elif isinstance(item, (Mapping, list, tuple)):
                yield from _numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _numbers(item)


def _is_cluster_output(value: Any) -> bool:
    """A cluster tool output: its sizes belong to numbered clusters."""
    return isinstance(value, Mapping) and isinstance(value.get("clusters"), list)


def _fail(reason: str, unverified: Iterable[str] = ()) -> VerificationResult:
    return VerificationResult(
        passed=False,
        failure=ErrorCode.GENERATION_VERIFICATION_FAILED,
        unverified_segment_ids=tuple(unverified),
        reason=reason,
    )


class AnalysisVerifier:
    """The contract checks of segment sizes, sums and persona references."""

    @staticmethod
    def verify(
        *,
        method: AnalysisMethod,
        segments: Sequence[Segment],
        evidence: Sequence[Evidence],
        persona_segment_ids: Sequence[Optional[str]],
        outputs: Mapping[str, Any],
        population: Optional[int] = None,
        populations: Optional[Mapping[str, int]] = None,
        emitted: Optional[Mapping[str, EmittedResult]] = None,
    ) -> VerificationResult:
        """Check the result.

        Args:
            method: The method that ran.
            segments: Segments from the analysis agent.
            evidence: Evidence from the analysis agent.
            persona_segment_ids: The segment each synthesized persona names.
            outputs: Tool call ID -> structured tool output.
            population: ``PrepareReport.rows_read`` of the one table.
            populations: Table alias -> people (``rows_read``) when several
                tables are used; a segment without ``source`` rests on the
                first table.
            emitted: ``run_code`` call ID -> the recounted result. Its
                segments must match the same ``segment_key`` exactly and use
                its population; the sum checks run per population.
        """
        tables = dict(populations or {"": int(population or 0)})
        runs = dict(emitted or {})
        default_key = next(iter(tables))
        groups: dict[str, list[Segment]] = {}
        sizes: dict[str, int] = {}
        unverified: list[str] = []
        for segment in segments:
            key, people, backed = AnalysisVerifier._place(
                segment, evidence, outputs, tables, runs, default_key
            )
            groups.setdefault(key, []).append(segment)
            sizes[key] = people
            if not backed:
                unverified.append(segment.id)
        if any(people < 1 for people in tables.values()) or any(
            people < 1 for people in sizes.values()
        ):
            return _fail("population", unverified)
        for key, members in groups.items():
            if sum(s.size for s in members if not s.overlaps) > sizes[key]:
                return _fail("sum", unverified)
        if method is AnalysisMethod.CLUSTER:
            # The segments of a run_code result are left out: its check already
            # put every person in exactly one of them, and each size used must
            # equal the recounted one. With fewer personas than its segments,
            # the ones used need not add up to the population.
            clustered = [
                (key, members)
                for key, members in groups.items()
                if not any(s.tool_call_id in runs for s in members)
                and (
                    len(groups) == 1
                    or any(str(s.definition.get("kind")) == "cluster" for s in members)
                )
            ]
            for key, members in clustered:
                if sum(s.size for s in members) != sizes[key]:
                    return _fail("cluster_sum", unverified)
        known = {s.id for s in segments}
        if any(sid not in known for sid in persona_segment_ids):
            return _fail("persona_without_segment", unverified)
        return VerificationResult(passed=True, unverified_segment_ids=tuple(unverified))

    @staticmethod
    def _place(
        segment: Segment,
        evidence: Sequence[Evidence],
        outputs: Mapping[str, Any],
        tables: Mapping[str, int],
        runs: Mapping[str, EmittedResult],
        default_key: str,
    ) -> tuple[str, int, bool]:
        """The population a segment rests on, its people and whether it is backed."""
        call = segment.tool_call_id
        if call is not None and call in runs:
            result = runs[call]
            key = segment.segment_key
            size = result.size_of(key) if key is not None else None
            people = int(result.population)
            backed = (
                size is not None
                and size == segment.size
                and people >= 1
                and round(segment.share, _SHARE_DIGITS)
                == round(size / people, _SHARE_DIGITS)
            )
            return f"run:{result.population_source}", people, backed
        source = segment.source
        key = source if source is not None and source in tables else default_key
        people = int(tables[key])
        return (
            key,
            people,
            AnalysisVerifier._is_backed(segment, evidence, people, outputs),
        )

    @staticmethod
    def verify_documents(
        *,
        persona_evidence_ids: Sequence[Sequence[str]],
        evidence: Sequence[Evidence],
    ) -> VerificationResult:
        """Documents only: every persona rests on checked document evidence.

        Each persona's ``evidence_ids`` must point at checked document evidence
        of the kinds ``value``, ``pain`` and ``goal`` (one or more of each).
        Missing IDs and dropped evidence do not count.

        Args:
            persona_evidence_ids: The ``evidence_ids`` of each persona.
            evidence: The document evidence that passed the quote check.
        """
        by_id = {e.id: e for e in evidence if e.is_document}
        for ids in persona_evidence_ids:
            kinds = {by_id[i].kind for i in ids if i in by_id}
            if not _KEY_KINDS <= kinds:
                return _fail("document_evidence")
        return VerificationResult(passed=True)

    @staticmethod
    def _is_backed(
        segment: Segment,
        evidence: Sequence[Evidence],
        population: int,
        outputs: Mapping[str, Any],
    ) -> bool:
        spec = segment.definition.get("spec", {})
        own = outputs.get(str(spec.get("tool_call_id", "")))
        clusters = own.get("clusters") if isinstance(own, Mapping) else None
        if isinstance(clusters, list):
            # A cluster output backs a size only through its own cluster: without
            # the index (or with one the output lacks) nothing says whose 60 or 40
            # it is, so the size is unverified.
            index = spec.get("cluster_index")
            sizes = {
                n
                for c in clusters
                if index is not None and isinstance(c, Mapping)
                if c.get("index") == index
                for n in _as_int(c.get("size"))
            }
            return (
                segment.size in sizes
                and population >= 1
                and round(segment.share, _SHARE_DIGITS)
                == round(segment.size / population, _SHARE_DIGITS)
            )
        calls = {str(spec.get("tool_call_id", ""))}
        calls |= {e.location for e in evidence if e.id in set(segment.evidence_ids)}
        seen = {
            n
            for call in calls
            if call in outputs and not _is_cluster_output(outputs[call])
            for n in _numbers(outputs[call])
        }
        if segment.size not in seen or population < 1:
            return False
        expected = round(segment.size / population, _SHARE_DIGITS)
        return round(segment.share, _SHARE_DIGITS) == expected
