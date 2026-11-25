"""AnalysisVerifier: the result is checked against the tool outputs (BR8.1〜BR8.3).

Values not seen in a tool output are marked unverified (the generation goes
on); non-overlapping sizes over the population, a cluster that does not add
up, or a persona without a segment fail the verification.
"""

from __future__ import annotations

import pytest

from src.managers.advanced_generation.analysis_verifier import AnalysisVerifier
from src.models.analysis import (
    AnalysisMethod,
    EmittedResult,
    Evidence,
    EvidenceKind,
    Segment,
)
from src.models.errors import ErrorCode

pytestmark = pytest.mark.unit

POPULATION = 100
CLUSTER_OUTPUT = {
    "population": 100,
    "clusters": [
        {"index": 0, "size": 60, "share": 0.6, "features": []},
        {"index": 1, "size": 40, "share": 0.4, "features": []},
    ],
}


def _segment(sid: str, size: int, share: float, *, overlaps=False, call="tc-1"):
    return Segment(
        id=sid,
        label=sid,
        definition={"kind": "cluster", "spec": {"tool_call_id": call}},
        size=size,
        share=share,
        features=(),
        evidence_ids=(f"e-{sid}",),
        overlaps=overlaps,
    )


def _evidence(sid: str, call: str = "tc-1") -> Evidence:
    return Evidence(
        id=f"e-{sid}",
        source_id="src-1",
        location=call,
        kind=EvidenceKind.ATTRIBUTE,
        claim="claim",
    )


def _verify(segments, persona_ids, *, outputs=None, method=AnalysisMethod.CLUSTER):
    return AnalysisVerifier.verify(
        method=method,
        segments=segments,
        evidence=[_evidence(s.id) for s in segments],
        persona_segment_ids=persona_ids,
        population=POPULATION,
        outputs=outputs if outputs is not None else {"tc-1": CLUSTER_OUTPUT},
    )


class TestPasses:
    def test_values_seen_in_the_output_pass(self) -> None:
        result = _verify([_segment("a", 60, 0.6), _segment("b", 40, 0.4)], ["a", "b"])

        assert result.passed and result.failure is None
        assert result.unverified_segment_ids == ()

    def test_share_is_compared_at_three_decimals(self) -> None:
        output = {"columns": ["plan", "count"], "rows": [["gold", 33]]}
        result = _verify(
            [_segment("a", 33, 0.3304)],
            ["a"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

        assert result.passed and result.unverified_segment_ids == ()


class TestOnlyCountsBackASize:
    def _check(self, output: dict, size: int, share: float):
        return _verify(
            [_segment("a", size, share)],
            ["a"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

    def test_an_average_equal_to_the_size_does_not_back_it(self) -> None:
        # 20 people, average age 42: "42 people" is not in a count column
        output = {"columns": ["plan", "count", "avg(age)"], "rows": [["gold", 20, 42]]}

        result = self._check(output, 42, 0.42)

        assert result.unverified_segment_ids == ("a",)

    def test_a_count_of_a_column_backs_it(self) -> None:
        output = {"columns": ["count(id)"], "rows": [[42]]}

        assert self._check(output, 42, 0.42).unverified_segment_ids == ()

    def test_a_distinct_count_is_not_the_people_of_a_segment(self) -> None:
        output = {"columns": ["count(distinct plan)"], "rows": [[42]]}

        assert self._check(output, 42, 0.42).unverified_segment_ids == ("a",)

    def test_the_size_of_a_cluster_backs_it(self) -> None:
        assert (
            _verify(
                [_segment("a", 60, 0.6), _segment("b", 40, 0.4)], ["a", "b"]
            ).unverified_segment_ids
            == ()
        )


class TestUnverified:
    def test_a_size_no_tool_printed_is_unverified(self) -> None:
        output = {"columns": ["plan", "count"], "rows": [["gold", 55]]}
        result = _verify(
            [_segment("a", 50, 0.5)],
            ["a"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

        assert result.passed
        assert result.unverified_segment_ids == ("a",)

    def test_a_wrong_share_is_unverified(self) -> None:
        output = {"columns": ["plan", "count"], "rows": [["gold", 50]]}
        result = _verify(
            [_segment("a", 50, 0.7)],
            ["a"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

        assert result.unverified_segment_ids == ("a",)

    def test_an_unknown_tool_call_is_unverified(self) -> None:
        result = _verify(
            [_segment("a", 50, 0.5)],
            ["a"],
            outputs={},
            method=AnalysisMethod.FILTER,
        )

        assert result.unverified_segment_ids == ("a",)


class TestFails:
    def test_non_overlapping_sizes_over_the_population_fail(self) -> None:
        output = {"columns": ["plan", "count"], "rows": [["a", 70], ["b", 50]]}
        result = _verify(
            [_segment("a", 70, 0.7), _segment("b", 50, 0.5)],
            ["a", "b"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

        assert not result.passed
        assert result.failure is ErrorCode.GENERATION_VERIFICATION_FAILED

    def test_overlapping_segments_are_not_summed(self) -> None:
        output = {"columns": ["plan", "count"], "rows": [["a", 70], ["b", 50]]}
        result = _verify(
            [_segment("a", 70, 0.7), _segment("b", 50, 0.5, overlaps=True)],
            ["a", "b"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

        assert result.passed

    def test_cluster_sizes_must_add_up(self) -> None:
        result = _verify([_segment("a", 60, 0.6)], ["a"])

        assert result.failure is ErrorCode.GENERATION_VERIFICATION_FAILED

    def test_a_persona_without_segment_fails(self) -> None:
        result = _verify(
            [_segment("a", 60, 0.6), _segment("b", 40, 0.4)], ["a", "missing"]
        )

        assert result.failure is ErrorCode.GENERATION_VERIFICATION_FAILED


def _quote(eid: str, kind: EvidenceKind) -> Evidence:
    return Evidence.for_document(
        id=eid,
        source_id="doc-1",
        location="p.1",
        kind=kind,
        claim="c",
        quote="文書からそのまま写した引用です",
    )


DOCUMENT_EVIDENCE = [
    _quote("v", EvidenceKind.VALUE),
    _quote("p", EvidenceKind.PAIN),
    _quote("g", EvidenceKind.GOAL),
    _quote("b", EvidenceKind.BEHAVIOR),
]


class TestVerifyDocuments:
    """U4 BR27.2, AC6.1.3: value, pain and goal for every document-only persona."""

    def test_every_key_item_backed_passes(self) -> None:
        result = AnalysisVerifier.verify_documents(
            persona_evidence_ids=[["v", "p", "g"], ["v", "p", "g", "b"]],
            evidence=DOCUMENT_EVIDENCE,
        )

        assert result.passed

    @pytest.mark.parametrize("missing", ["v", "p", "g"])
    def test_a_missing_key_item_fails(self, missing: str) -> None:
        ids = [i for i in ("v", "p", "g") if i != missing]

        result = AnalysisVerifier.verify_documents(
            persona_evidence_ids=[["v", "p", "g"], ids], evidence=DOCUMENT_EVIDENCE
        )

        assert result.failure is ErrorCode.GENERATION_VERIFICATION_FAILED
        assert result.reason == "document_evidence"

    def test_ids_not_in_the_checked_evidence_do_not_count(self) -> None:
        # "g" was dropped by the quote check: only v and p remain
        result = AnalysisVerifier.verify_documents(
            persona_evidence_ids=[["v", "p", "g", "made-up"]],
            evidence=DOCUMENT_EVIDENCE[:2],
        )

        assert not result.passed

    def test_table_evidence_does_not_stand_for_a_quote(self) -> None:
        table_goal = Evidence(
            id="g", source_id="t", location="tc-1", kind=EvidenceKind.GOAL, claim="c"
        )

        result = AnalysisVerifier.verify_documents(
            persona_evidence_ids=[["v", "p", "g"]],
            evidence=[*DOCUMENT_EVIDENCE[:2], table_goal],
        )

        assert not result.passed


# --- U6: populations per table and run_code segments (BR36.3, BR37.5) -------

_EMITTED = EmittedResult(
    population=50,
    population_source="join:source_1+source_2",
    segments=(
        {"segment_key": "buyer", "size": 20, "features": []},
        {"segment_key": "none", "size": 30, "features": []},
    ),
)


def _on(source: str, sid: str, size: int, share: float, *, call: str = "tc-1"):
    return _segment(sid, size, share, call=call).update(
        definition={"kind": "filter", "source": source, "spec": {"tool_call_id": call}}
    )


def _coded(sid: str, key: str, size: int, share: float, *, call: str = "tc-5"):
    return _segment(sid, size, share, call=call).update(
        definition={
            "kind": "derived",
            "source": "join:source_1+source_2",
            "spec": {"tool_call_id": call, "segment_key": key},
        }
    )


def _verify_u6(segments, *, outputs, populations, emitted=None, method=None):
    return AnalysisVerifier.verify(
        method=method or AnalysisMethod.FILTER,
        segments=segments,
        evidence=[_evidence(s.id, s.tool_call_id or "tc-1") for s in segments],
        persona_segment_ids=[s.id for s in segments],
        populations=populations,
        outputs=outputs,
        emitted=emitted or {},
    )


class TestPopulationsPerTable:
    def test_each_segment_uses_its_tables_people(self) -> None:
        outputs = {
            "tc-1": {"columns": ["count"], "rows": [[60]]},
            "tc-2": {"columns": ["count"], "rows": [[10]]},
        }
        segments = [
            _on("source_1", "a", 60, 0.6, call="tc-1"),
            _on("source_2", "b", 10, 0.5, call="tc-2"),
        ]

        result = _verify_u6(
            segments, outputs=outputs, populations={"source_1": 100, "source_2": 20}
        )

        assert result.passed and result.unverified_segment_ids == ()

    def test_the_sum_is_checked_per_population(self) -> None:
        outputs = {"tc-1": {"columns": ["count"], "rows": [[60], [50]]}}
        segments = [
            _on("source_1", "a", 60, 0.6),
            _on("source_1", "b", 50, 0.5),
            _on("source_2", "c", 1, 0.05),
        ]

        result = _verify_u6(
            segments, outputs=outputs, populations={"source_1": 100, "source_2": 20}
        )

        assert not result.passed and result.reason == "sum"

    def test_two_tables_may_add_up_to_more_than_one_of_them(self) -> None:
        outputs = {
            "tc-1": {"columns": ["count"], "rows": [[90]]},
            "tc-2": {"columns": ["count"], "rows": [[15]]},
        }
        segments = [
            _on("source_1", "a", 90, 0.9, call="tc-1"),
            _on("source_2", "b", 15, 0.75, call="tc-2"),
        ]

        result = _verify_u6(
            segments, outputs=outputs, populations={"source_1": 100, "source_2": 20}
        )

        assert result.passed

    def test_a_segment_without_a_source_uses_the_first_table(self) -> None:
        segments = [_segment("a", 60, 0.6)]

        result = _verify_u6(
            segments,
            outputs={"tc-1": CLUSTER_OUTPUT},
            populations={"source_1": 100, "source_2": 7},
        )

        assert result.unverified_segment_ids == ()


class TestRunCodeSegments:
    def test_the_same_key_and_size_pass_with_the_recounted_population(self) -> None:
        segments = [_coded("a", "buyer", 20, 0.4), _coded("b", "none", 30, 0.6)]

        result = _verify_u6(
            segments,
            outputs={"tc-5": _EMITTED.to_dict()},
            populations={"source_1": 50},
            emitted={"tc-5": _EMITTED},
        )

        assert result.passed and result.unverified_segment_ids == ()

    def test_another_integer_of_the_output_does_not_pass(self) -> None:
        # 30 is in the output (the other key's size), not the size of "buyer"
        segments = [_coded("a", "buyer", 30, 0.6)]

        result = _verify_u6(
            segments,
            outputs={"tc-5": _EMITTED.to_dict()},
            populations={"source_1": 50},
            emitted={"tc-5": _EMITTED},
        )

        assert result.unverified_segment_ids == ("a",)

    def test_an_unknown_key_is_unverified(self) -> None:
        segments = [_coded("a", "vip", 20, 0.4)]

        result = _verify_u6(
            segments,
            outputs={"tc-5": _EMITTED.to_dict()},
            populations={"source_1": 50},
            emitted={"tc-5": _EMITTED},
        )

        assert result.unverified_segment_ids == ("a",)

    def test_a_joined_cluster_must_add_up_to_its_own_population(self) -> None:
        segments = [_coded("a", "buyer", 20, 0.4)]

        result = _verify_u6(
            segments,
            outputs={"tc-5": _EMITTED.to_dict()},
            populations={"source_1": 50},
            emitted={"tc-5": _EMITTED},
            method=AnalysisMethod.CLUSTER,
        )

        assert not result.passed and result.reason == "cluster_sum"
