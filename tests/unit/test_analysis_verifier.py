"""AnalysisVerifier: the result is checked against the tool outputs (BR8.1〜BR8.3).

Values not seen in a tool output are marked unverified (the generation goes
on); non-overlapping sizes over the population, a cluster that does not add
up, or a persona without a segment fail the verification.
"""

from __future__ import annotations

import pytest

from src.managers.advanced_generation.analysis_verifier import AnalysisVerifier
from src.models.analysis import AnalysisMethod, Evidence, EvidenceKind, Segment
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
        output = {"rows": [{"count": 33}]}
        result = _verify(
            [_segment("a", 33, 0.3304)],
            ["a"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

        assert result.passed and result.unverified_segment_ids == ()


class TestUnverified:
    def test_a_size_no_tool_printed_is_unverified(self) -> None:
        output = {"rows": [{"count": 55}]}
        result = _verify(
            [_segment("a", 50, 0.5)],
            ["a"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

        assert result.passed
        assert result.unverified_segment_ids == ("a",)

    def test_a_wrong_share_is_unverified(self) -> None:
        output = {"rows": [{"count": 50}]}
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
        output = {"rows": [{"count": 70}, {"count": 50}]}
        result = _verify(
            [_segment("a", 70, 0.7), _segment("b", 50, 0.5)],
            ["a", "b"],
            outputs={"tc-1": output},
            method=AnalysisMethod.FILTER,
        )

        assert not result.passed
        assert result.failure is ErrorCode.GENERATION_VERIFICATION_FAILED

    def test_overlapping_segments_are_not_summed(self) -> None:
        output = {"rows": [{"count": 70}, {"count": 50}]}
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
