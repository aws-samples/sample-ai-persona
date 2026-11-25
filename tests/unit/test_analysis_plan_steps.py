"""The plan as a sequence of steps (U6 addendum: multi-step plan).

- ``AnalysisPlan.steps``: up to four steps; ``method`` stays the method of the
  step that makes the segments. A plan without steps (old records) reads as
  one step of its ``method``.
- ``AnalysisPolicy.guide_candidates``: the step candidates from the
  instruction and the shape of the columns, not only from keywords.
- ``AnalysisPolicy.settle_plan``: drops steps that cannot run (detailed-only
  kinds without the detailed analysis, unknown tables or columns), caps them
  at four and falls back to one step.
- Prompts: the candidates reach the planning request, the analysis request
  lists one guide per step, the detailed note lists the three playbooks.
"""

from __future__ import annotations

import pytest

from src.managers.advanced_generation import prompts
from src.managers.advanced_generation.analysis_policy import AnalysisPolicy
from src.models.analysis import (
    MAX_PLAN_STEPS,
    AnalysisMethod,
    AnalysisPlan,
    AnalysisStep,
    AnalysisStepKind,
    JoinPlan,
    step_method,
)

pytestmark = pytest.mark.unit

K = AnalysisStepKind


def _plan(*steps: AnalysisStep, method: AnalysisMethod = AnalysisMethod.FILTER):
    return AnalysisPlan(
        interpreted_purpose="目的",
        questions=("q",),
        method=method,
        rationale="理由",
        budget={},
        steps=tuple(steps) if steps else None,
    )


# --- the model -----------------------------------------------------------------


class TestAnalysisStepModel:
    def test_round_trip_keeps_the_steps_and_drops_none(self) -> None:
        plan = _plan(
            AnalysisStep(K.JOIN, "結合の計画がある"),
            AnalysisStep(K.RFM, "購買の履歴", table="source_2", column="金額"),
        )

        data = plan.to_dict()

        assert data["steps"] == [
            {"kind": "join", "reason": "結合の計画がある"},
            {
                "kind": "rfm",
                "reason": "購買の履歴",
                "table": "source_2",
                "column": "金額",
            },
        ]
        assert AnalysisPlan.from_dict(data) == plan

    def test_a_plan_without_steps_writes_no_steps(self) -> None:
        assert "steps" not in _plan().to_dict()

    def test_an_old_record_reads_as_one_step_of_its_method(self) -> None:
        old = {
            "interpreted_purpose": "p",
            "questions": ["q"],
            "method": "cluster",
            "rationale": "指示が空欄",
            "budget": {},
        }

        plan = AnalysisPlan.from_dict(old)

        assert plan.steps is None
        assert plan.plan_steps() == (AnalysisStep(K.CLUSTER, "指示が空欄"),)
        assert plan.to_dict() == old

    def test_more_than_four_steps_are_refused(self) -> None:
        with pytest.raises(ValueError):
            _plan(*[AnalysisStep(K.FILTER, "r")] * (MAX_PLAN_STEPS + 1))

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValueError):
            AnalysisStep.from_dict({"kind": "sql", "reason": "r"})

    @pytest.mark.parametrize(
        ("kind", "method"),
        [
            (K.FILTER, AnalysisMethod.FILTER),
            (K.COMPARE, AnalysisMethod.COMPARE),
            (K.CLUSTER, AnalysisMethod.CLUSTER),
            (K.RFM, AnalysisMethod.FILTER),
            (K.COMPARE_GROUPS, AnalysisMethod.COMPARE),
            (K.JOIN, None),
        ],
    )
    def test_the_method_of_each_kind(
        self, kind: AnalysisStepKind, method: AnalysisMethod | None
    ) -> None:
        assert step_method(kind) is method


# --- the candidates -----------------------------------------------------------

PERSON_ID = {"name": "customer_id", "type": "BIGINT", "all_distinct": True}
AGE = {"name": "年齢", "type": "BIGINT", "all_distinct": False, "distinct": 40}
PLAN = {"name": "会員種別", "type": "VARCHAR", "all_distinct": False, "distinct": 3}
AREA = {"name": "地域", "type": "VARCHAR", "all_distinct": False, "distinct": 30}
BUYER = {"name": "customer_id", "type": "BIGINT", "all_distinct": False}
AMOUNT = {"name": "金額", "type": "DOUBLE", "all_distinct": False, "distinct": 90}
BOUGHT_ON = {"name": "購入日", "type": "DATE", "all_distinct": False}

CUSTOMERS = ("source_1", {"rows": 100, "columns": [PERSON_ID, AGE, AREA]})
PURCHASES = ("source_2", {"rows": 300, "columns": [BUYER, AMOUNT, BOUGHT_ON]})
JOIN = JoinPlan(
    axis="source_1", table="source_2", key_column="customer_id", match_rate=0.9
)


def _kinds(purpose: str, tables, joins=(), detailed: bool = True) -> list[str]:
    return [
        c.kind.value
        for c in AnalysisPolicy.guide_candidates(purpose, tables, joins, detailed)
    ]


class TestGuideCandidates:
    """One test per row of the addendum's table."""

    def test_a_join_plan_comes_first(self) -> None:
        kinds = _kinds("", [CUSTOMERS, PURCHASES], [JOIN])

        assert kinds[0] == "join"

    def test_history_with_a_date_and_a_number_suggests_rfm(self) -> None:
        kinds = _kinds("", [CUSTOMERS, PURCHASES], [JOIN])

        assert kinds[:3] == ["join", "person_profile", "rfm"]

    def test_a_history_suggests_the_person_profile_only_with_the_detailed_analysis(
        self,
    ) -> None:
        assert "person_profile" in _kinds("", [CUSTOMERS, PURCHASES], [JOIN])
        assert "person_profile" not in _kinds(
            "", [CUSTOMERS, PURCHASES], [JOIN], detailed=False
        )
        # a table of one row per person has no history to sum up
        assert "person_profile" not in _kinds("", [CUSTOMERS])

    def test_purchase_words_suggest_rfm(self) -> None:
        kinds = _kinds("優良なお客さまを知りたい", [CUSTOMERS])

        assert "rfm" in kinds

    def test_comparison_words_suggest_compare_then_the_test(self) -> None:
        kinds = _kinds("会員種別で年齢を比べたい", [CUSTOMERS])

        assert kinds == ["compare", "compare_groups"]

    def test_a_few_valued_column_and_a_number_suggest_compare(self) -> None:
        table = ("source_1", {"rows": 100, "columns": [AGE, PLAN]})

        kinds = _kinds("30 代の人", [table])

        assert kinds[:2] == ["compare", "compare_groups"]

    def test_the_test_needs_the_detailed_analysis(self) -> None:
        kinds = _kinds("会員種別で年齢を比べたい", [CUSTOMERS], detailed=False)

        assert kinds == ["compare"]

    def test_empty_with_a_person_id_is_cluster(self) -> None:
        assert _kinds("", [CUSTOMERS], detailed=False) == ["cluster"]

    def test_empty_with_two_numbers_is_cluster(self) -> None:
        table = ("source_1", {"rows": 100, "columns": [AGE, AMOUNT]})

        assert _kinds("", [table], detailed=False) == ["cluster"]

    def test_a_named_target_otherwise_is_filter(self) -> None:
        assert _kinds("東京の人", [CUSTOMERS]) == ["filter"]

    def test_without_the_detailed_analysis_no_detailed_kind(self) -> None:
        kinds = _kinds("優良なお客さま", [CUSTOMERS, PURCHASES], [JOIN], False)

        assert not {"join", "rfm", "compare_groups"} & set(kinds)

    @pytest.mark.parametrize(
        "purpose", ["", "東京の人", "会員種別で比べたい", "優良なお客さま"]
    )
    def test_the_last_candidate_is_the_guide_method(self, purpose: str) -> None:
        candidates = AnalysisPolicy.guide_candidates(
            purpose, [CUSTOMERS, PURCHASES], [JOIN], True
        )
        guide = AnalysisPolicy.guide_method(purpose, CUSTOMERS[1])

        assert step_method(candidates[-1].kind) is guide
        assert all(c.reason for c in candidates)

    def test_no_table_gives_no_candidate(self) -> None:
        assert AnalysisPolicy.guide_candidates("", [], [], True) == []


# --- settling the steps --------------------------------------------------------

TABLES = {"source_1": ["customer_id", "年齢", "地域"], "source_2": ["金額"]}


def _settle(steps: list[dict], *, detailed: bool = True, method: str = "filter"):
    raw = {
        "interpreted_purpose": "p",
        "questions": ["q"],
        "method": method,
        "rationale": "r",
        "deviation_reason": "手順に合わせる",
        "steps": steps,
    }
    plan = AnalysisPolicy.settle_plan(
        raw,
        guide=AnalysisMethod.FILTER,
        attempt=1,
        columns=TABLES["source_1"],
        detailed=detailed,
        tables=TABLES,
    )
    assert plan is not None
    return plan


class TestSettleSteps:
    def test_steps_that_can_run_are_kept(self) -> None:
        plan = _settle(
            [
                {"kind": "join", "reason": "結合"},
                {
                    "kind": "rfm",
                    "reason": "履歴",
                    "table": "source_2",
                    "column": "金額",
                },
            ]
        )

        assert [s.kind for s in plan.plan_steps()] == [K.JOIN, K.RFM]
        assert plan.steps is not None and plan.steps[1].column == "金額"

    def test_without_the_detailed_analysis_the_fixed_tool_replaces_or_drops(
        self,
    ) -> None:
        plan = _settle(
            [
                {"kind": "join", "reason": "結合"},
                {"kind": "rfm", "reason": "履歴"},
                {"kind": "compare_groups", "reason": "検定"},
            ],
            detailed=False,
            method="compare",
        )

        assert [s.kind for s in plan.plan_steps()] == [K.FILTER, K.COMPARE]

    def test_unknown_tables_and_columns_are_dropped(self) -> None:
        plan = _settle(
            [
                {"kind": "filter", "reason": "a", "table": "source_9"},
                {"kind": "filter", "reason": "b", "column": "DROP TABLE"},
                {
                    "kind": "compare",
                    "reason": "c",
                    "table": "source_1",
                    "column": "金額",
                },
                {
                    "kind": "compare",
                    "reason": "d",
                    "table": "source_1",
                    "column": "地域",
                },
            ],
            method="compare",
        )

        assert [s.reason for s in plan.plan_steps()] == ["d"]

    def test_unknown_kinds_are_dropped(self) -> None:
        plan = _settle([{"kind": "sql", "reason": "x"}, {"kind": "filter"}])

        assert [s.kind for s in plan.plan_steps()] == [K.FILTER]

    def test_steps_are_capped_at_four(self) -> None:
        kinds = ("rfm", "compare_groups", "join", "compare", "filter")

        plan = _settle([{"kind": k, "reason": k} for k in kinds])

        steps = plan.plan_steps()
        assert len(steps) == MAX_PLAN_STEPS
        assert step_method(steps[-1].kind) is plan.method

    def test_no_step_left_falls_back_to_one_step_of_the_method(self) -> None:
        plan = _settle([{"kind": "join", "reason": "x"}], detailed=False)

        assert [s.kind for s in plan.plan_steps()] == [K.FILTER]

    def test_the_last_step_always_makes_the_segments_of_the_method(self) -> None:
        # code review: steps ending with a join (which makes no segments)
        plan = _settle(
            [{"kind": "rfm", "reason": "a"}, {"kind": "join", "reason": "b"}]
        )

        kinds = [s.kind for s in plan.plan_steps()]
        assert kinds[-1] is K.FILTER and plan.method is AnalysisMethod.FILTER
        assert step_method(kinds[-1]) is plan.method

    def test_cutting_to_four_keeps_the_step_that_makes_the_segments(self) -> None:
        plan = _settle(
            [
                {"kind": k, "reason": k}
                for k in ("rfm", "compare_groups", "join", "compare", "cluster")
            ],
            method="cluster",
        )

        kinds = [s.kind for s in plan.plan_steps()]
        assert len(kinds) == MAX_PLAN_STEPS
        assert step_method(kinds[-1]) is AnalysisMethod.CLUSTER

    def test_a_kind_is_listed_once_and_the_method_step_moves_to_the_end(self) -> None:
        # real data: "cluster" came twice, once with the whole rationale as reason
        plan = _settle(
            [
                {"kind": "rfm", "reason": "点数化"},
                {"kind": "cluster", "reason": "まとめる"},
                {"kind": "compare_groups", "reason": "比べる"},
                {"kind": "cluster", "reason": "全文の理由" * 20},
            ],
            method="cluster",
        )

        steps = plan.plan_steps()
        assert [s.kind for s in steps] == [K.RFM, K.COMPARE_GROUPS, K.CLUSTER]
        assert steps[-1].reason == "まとめる"

    def test_a_plan_over_joined_tables_starts_with_the_join(self) -> None:
        raw = {
            "interpreted_purpose": "p",
            "questions": ["q"],
            "method": "cluster",
            "rationale": "r",
            "deviation_reason": "層を見つけたい",
            "steps": [
                {"kind": "rfm", "reason": "点数化"},
                {"kind": "cluster", "reason": "まとめる"},
            ],
        }

        def settle(*, joined: bool, detailed: bool = True):
            plan = AnalysisPolicy.settle_plan(
                raw,
                guide=AnalysisMethod.CLUSTER,
                attempt=1,
                columns=TABLES["source_1"],
                detailed=detailed,
                tables=TABLES,
                joined=joined,
            )
            assert plan is not None
            return [s.kind for s in plan.plan_steps()]

        assert settle(joined=True) == [K.JOIN, K.RFM, K.CLUSTER]
        assert settle(joined=False) == [K.RFM, K.CLUSTER]
        # without the detailed analysis there is no join to run
        assert K.JOIN not in settle(joined=True, detailed=False)

    def test_a_method_forced_to_the_guide_rewrites_the_last_step(self) -> None:
        # code review: guide cluster, the agent answered filter twice
        raw = {
            "interpreted_purpose": "p",
            "questions": ["q"],
            "method": "filter",
            "rationale": "r",
            "steps": [{"kind": "filter", "reason": "絞る"}],
        }
        plan = AnalysisPolicy.settle_plan(
            raw,
            guide=AnalysisMethod.CLUSTER,
            attempt=2,
            columns=TABLES["source_1"],
            tables=TABLES,
        )

        assert plan is not None and plan.method is AnalysisMethod.CLUSTER
        assert plan.plan_steps()[-1].kind is K.CLUSTER

    def test_no_steps_given_keeps_the_old_shape(self) -> None:
        plan = _settle([])

        assert plan.steps is None and "steps" not in plan.to_dict()


# --- the prompts -----------------------------------------------------------------


class TestPlanningPrompt:
    def test_the_candidates_reach_the_request(self) -> None:
        candidates = AnalysisPolicy.guide_candidates(
            "", [CUSTOMERS, PURCHASES], [JOIN], True
        )

        text = prompts.planning_prompt(2, 1, candidates=candidates)

        for candidate in candidates:
            assert candidate.kind.value in text and candidate.reason in text

    def test_the_instructions_ask_to_combine_and_explain(self) -> None:
        text = prompts.PLANNING_INSTRUCTIONS

        assert "steps" in text and "組み合わせ" in text
        assert "deviation_reason" in text
        for kind in K:
            assert kind.value in text


class TestAnalysisPromptSteps:
    def test_one_step_plan_is_the_old_request(self) -> None:
        plan = _plan(method=AnalysisMethod.CLUSTER)

        text = prompts.analysis_prompt(plan, 3)

        assert text.splitlines()[0].startswith("手法は cluster です")
        assert "手順 1" not in text

    def test_steps_are_listed_in_order(self) -> None:
        plan = _plan(
            AnalysisStep(K.JOIN, "結合"),
            AnalysisStep(K.RFM, "履歴"),
            AnalysisStep(K.COMPARE, "比べる"),
            method=AnalysisMethod.COMPARE,
        )

        text = prompts.analysis_prompt(plan, 2, joins=[JOIN])

        first, second, third = (text.index(f"手順 {n}") for n in (1, 2, 3))
        assert first < second < third
        assert text.index('load_playbook("rfm")') > second
        assert "手法は compare です" in text[third:]

    def test_join_is_named_only_with_a_join_step(self) -> None:
        with_join = _plan(AnalysisStep(K.JOIN, "結合"), AnalysisStep(K.FILTER, "f"))
        without = _plan(AnalysisStep(K.RFM, "履歴"))

        assert 'load_playbook("join")' in prompts.analysis_prompt(
            with_join, 2, joins=[JOIN]
        )
        text = prompts.analysis_prompt(without, 2, joins=[JOIN])
        assert 'load_playbook("join")' not in text
        assert "customer_id" in text  # still told the tables can be joined

    def test_the_split_line_is_for_a_final_filter_only(self) -> None:
        rfm = _plan(AnalysisStep(K.RFM, "履歴"))
        plain = _plan(AnalysisStep(K.FILTER, "絞る"))

        assert "group_by し" not in prompts.analysis_prompt(rfm, 2)
        assert "group_by し" in prompts.analysis_prompt(plain, 2)


class TestDetailedNote:
    def test_lists_every_playbook_with_when_to_use_it(self) -> None:
        text = prompts.analysis_instructions(detailed=True)

        for name, when in (
            ("join", "結合"),
            ("compare_groups", "検定"),
            ("rfm", "新しさ・頻度・金額"),
        ):
            line = next(ln for ln in text.splitlines() if ln.startswith(f"- {name}"))
            assert when in line

    def test_no_longer_limits_to_one_playbook(self) -> None:
        text = prompts.analysis_instructions(detailed=True)

        assert "1 つ" not in text
        assert "計画の手順に合う定石" in text

    def test_tells_a_look_around_run_is_no_failure(self) -> None:
        assert "結果なし" in prompts.analysis_instructions(detailed=True)
