"""detailed_analysis: the run_code budget and the evidence it may leave (U6).

BR36.2 (five failures take run_code off), NFR5.55 (the gate the Service only
tells), BR36.6 (a change halts), BR36.4 / NFR3.39 (only what a successful
run recounted reaches the synthesis).
"""

from __future__ import annotations

import pytest

from src.managers.advanced_generation.detailed_analysis import (
    RunCodeBudget,
    filter_evidence,
)
from src.models.analysis import (
    EmittedResult,
    Evidence,
    EvidenceKind,
    RunCodeGate,
    RunCodeOutcome,
    Segment,
)

pytestmark = pytest.mark.unit

FAILED = RunCodeOutcome(ok=False, error_code="RUN_CODE_FAILED")


class TestRunCodeBudget:
    def test_is_a_gate(self) -> None:
        assert isinstance(RunCodeBudget(max_failures=5), RunCodeGate)

    def test_five_failures_take_it_off_and_say_so_once(self) -> None:
        told: list[tuple[str, int]] = []
        budget = RunCodeBudget(
            max_failures=5, on_disabled=lambda reason, n: told.append((reason, n))
        )

        for _ in range(4):
            budget.record(FAILED)
        assert budget.allows_run_code() is True
        budget.record(FAILED)
        budget.record(FAILED)

        assert budget.allows_run_code() is False
        assert budget.disabled_reason == "max_failures"
        assert told == [("max_failures", 5)]
        assert budget.halted() is False

    def test_a_success_does_not_reset_the_count(self) -> None:
        budget = RunCodeBudget(max_failures=2)

        budget.record(FAILED)
        budget.record(RunCodeOutcome(ok=True))
        budget.record(FAILED)

        assert budget.failures == 2 and budget.allows_run_code() is False
        assert budget.successes == 1

    def test_a_look_around_is_a_call_but_no_failure(self) -> None:
        """U6 addendum: run_code ending without emit_result."""
        told: list[tuple[str, int]] = []
        budget = RunCodeBudget(
            max_failures=2, on_disabled=lambda reason, n: told.append((reason, n))
        )
        look = RunCodeOutcome(ok=False, error_code="NO_RESULT", no_result=True)

        for _ in range(3):
            budget.record(look)

        assert budget.failures == 0 and budget.no_results == 3
        assert budget.calls == 3
        assert budget.allows_run_code() is True and told == []

    def test_a_failed_cancel_takes_it_off_at_once(self) -> None:
        told: list[tuple[str, int]] = []
        budget = RunCodeBudget(
            max_failures=5, on_disabled=lambda reason, n: told.append((reason, n))
        )

        budget.record(RunCodeOutcome(ok=False, timed_out=True, cancel_failed=True))

        assert budget.allows_run_code() is False
        assert told == [("cancel_failed", 1)]

    def test_a_change_halts_everything(self) -> None:
        changes: list[tuple[str, ...]] = []
        budget = RunCodeBudget(max_failures=5, on_tampered=changes.append)

        budget.record(RunCodeOutcome(ok=False, tampered=True, changed=("parquet",)))
        budget.record(RunCodeOutcome(ok=False, tampered=True, changed=("library",)))

        assert budget.halted() is True and budget.allows_run_code() is False
        assert budget.failures == 0
        assert changes == [("parquet",)]
        assert budget.changed == ("parquet",)

    def test_a_failed_check_halts_without_a_change(self) -> None:
        """review-01 R-03: the check itself failed; nothing is called tampered."""
        changes: list[tuple[str, ...]] = []
        failed: list[int] = []
        budget = RunCodeBudget(
            max_failures=5,
            on_tampered=changes.append,
            on_verify_failed=lambda: failed.append(1),
        )

        budget.record(RunCodeOutcome(ok=False, verify_failed=True))
        budget.record(RunCodeOutcome(ok=False, verify_failed=True))

        assert budget.halted() is True and budget.allows_run_code() is False
        assert budget.verify_failed is True
        assert changes == [] and budget.changed == ()
        assert failed == [1]
        assert budget.failures == 0


_RESULT = EmittedResult(
    population=100,
    population_source="source_1",
    segments=(
        {"segment_key": "high", "size": 30, "features": []},
        {"segment_key": "low", "size": 70, "features": []},
    ),
    statistics=({"name": "welch_t", "value": 3.21, "p_value": 0.0021},),
)


def _segment(
    sid: str, call: str, key: str | None, evidence: tuple[str, ...]
) -> Segment:
    spec: dict = {"tool_call_id": call}
    if key is not None:
        spec["segment_key"] = key
    return Segment(
        id=sid,
        label=sid,
        definition={"kind": "derived", "source": "source_1", "spec": spec},
        size=30,
        share=0.3,
        features=(),
        evidence_ids=evidence,
    )


def _evidence(eid: str, location: str, statistic: dict | None) -> Evidence:
    return Evidence(
        id=eid,
        source_id="f1",
        location=location,
        kind=EvidenceKind.BEHAVIOR,
        claim="買う回数が多い",
        statistic=statistic,
    )


class TestFilterEvidence:
    def test_keeps_what_a_successful_run_recounted(self) -> None:
        segments = [_segment("s1", "tc-2", "high", ("e1",))]
        evidence = [_evidence("e1", "tc-2", {"value": 30, "population": 100})]

        kept_segments, kept_evidence = filter_evidence(
            segments, evidence, {"tc-1": None, "tc-2": _RESULT}
        )

        assert [s.id for s in kept_segments] == ["s1"]
        assert [e.id for e in kept_evidence] == ["e1"]

    def test_drops_what_points_at_a_failed_run(self) -> None:
        segments = [
            _segment("s1", "tc-1", "high", ("e1",)),
            _segment("s2", "tc-2", "high", ("e2",)),
        ]
        evidence = [
            _evidence("e1", "tc-1", {"value": 30}),
            _evidence("e2", "tc-2", {"value": 30}),
        ]

        kept_segments, kept_evidence = filter_evidence(
            segments, evidence, {"tc-1": None, "tc-2": _RESULT}
        )

        assert [s.id for s in kept_segments] == ["s2"]
        assert [e.id for e in kept_evidence] == ["e2"]

    def test_a_number_the_result_does_not_hold_is_dropped(self) -> None:
        evidence = [
            _evidence("e1", "tc-2", {"value": 30, "share": 0.3}),
            # e.g. a number printed to stdout, never recounted
            _evidence("e2", "tc-2", {"value": 4242}),
            _evidence("e3", "tc-2", None),
        ]

        _, kept = filter_evidence([], evidence, {"tc-2": _RESULT})

        assert [e.id for e in kept] == ["e1"]

    def test_a_statistic_the_code_passed_is_not_recounted(self) -> None:
        """review-01 R-02: p values and effect sizes come from the AI's code."""
        evidence = [
            _evidence("e1", "tc-2", {"value": 3.21, "p_value": 0.0021}),
            _evidence("e2", "tc-2", {"value": 30, "p_value": 0.0021}),
            _evidence("e3", "tc-2", {"value": 70, "population": 100}),
        ]

        _, kept = filter_evidence([], evidence, {"tc-2": _RESULT})

        assert [e.id for e in kept] == ["e3"]

    def test_a_feature_value_the_code_passed_is_not_recounted(self) -> None:
        result = EmittedResult(
            population=100,
            population_source="source_1",
            segments=(
                {
                    "segment_key": "high",
                    "size": 30,
                    "features": [{"name": "spend", "value": 5123.5}],
                },
            ),
        )
        evidence = [
            _evidence("e1", "tc-2", {"value": 5123.5}),
            _evidence("e2", "tc-2", {"value": 30}),
        ]

        _, kept = filter_evidence([], evidence, {"tc-2": result})

        assert [e.id for e in kept] == ["e2"]

    def test_a_quote_cannot_rest_on_run_code(self) -> None:
        quoted = _evidence("e1", "tc-2", {"value": 30}).update(quote="発言")

        _, kept = filter_evidence([], [quoted], {"tc-2": _RESULT})

        assert kept == []

    def test_a_segment_without_a_known_key_is_dropped(self) -> None:
        segments = [
            _segment("s1", "tc-2", "middle", ("e1",)),
            _segment("s2", "tc-2", None, ("e1",)),
        ]
        evidence = [_evidence("e1", "tc-2", {"value": 30})]

        kept, _ = filter_evidence(segments, evidence, {"tc-2": _RESULT})

        assert kept == []

    def test_a_layer_the_library_counted_keeps_its_size_as_evidence(self) -> None:
        """The code's own numbers are not accepted, the layer's size still is."""
        segments = [_segment("s1", "tc-2", "high", ("e1",))]
        evidence = [_evidence("e1", "tc-2", {"value": 4242})]  # never recounted

        kept_segments, kept_evidence = filter_evidence(
            segments, evidence, {"tc-2": _RESULT}
        )

        assert [s.id for s in kept_segments] == ["s1"]
        (made,) = kept_evidence
        assert kept_segments[0].evidence_ids == (made.id,)
        assert made.location == "tc-2"
        assert made.statistic == {"value": 30, "population": 100}

    def test_a_segment_left_without_evidence_is_dropped(self) -> None:
        segment = _segment("s1", "tc-9", None, ("e1",))  # a fixed tool's call
        evidence = [_evidence("e1", "tc-1", {"value": 30})]  # a failed run

        kept, _ = filter_evidence([segment], evidence, {"tc-1": None})

        assert kept == []

    def test_fixed_tools_are_untouched(self) -> None:
        segment = _segment("s1", "tc-9", None, ("e1", "e2"))
        evidence = [
            _evidence("e1", "tc-9", {"value": 12345}),
            _evidence("e2", "tc-1", {"value": 30}),
        ]

        kept_segments, kept_evidence = filter_evidence(
            [segment], evidence, {"tc-1": None}
        )

        assert kept_segments[0].evidence_ids == ("e1",)
        assert [e.id for e in kept_evidence] == ["e1"]
