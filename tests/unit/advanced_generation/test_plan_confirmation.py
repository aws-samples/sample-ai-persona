"""AdvancedPersonaGenerationManager: the plan, its confirmation and the waits."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.managers.advanced_generation.errors import AdvancedGenerationError
from src.models.advanced_generation import GenerationStatus
from src.models.errors import ErrorCode
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.advanced_generation.helpers import (
    _EMIT,
    _EMIT_JOIN,
    ADMIN,
    AWAITING,
    FAST_CONTROL,
    OTHER,
    START,
    _advance,
    _await_card,
    _code_analysis,
    _current,
    _detailed_env,
    _Env,
    _events,
    _purchases_csv,
    _start,
    _start_confirming,
    _start_detailed,
    _Thread,
)

pytestmark = pytest.mark.unit


class TestPlanConfirmation:
    """Plan confirmation."""

    def test_off_runs_straight_to_the_analysis(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = _current(env, generation_id)
        assert generation.status is GenerationStatus.DONE
        assert generation.options["confirm_plan"] is False
        stages = [s["status"] for k, s in _events(env, generation_id) if k == "stage"]
        assert AWAITING.value not in stages

    def test_on_stops_after_the_plan_and_extends_the_lock(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        file = env.ready_file()
        generation_id = _start_confirming(manager, file)
        run = _Thread(manager, generation_id)

        _await_card(env, generation_id)

        generation = _current(env, generation_id)
        assert generation.confirm_plan and generation.plan is not None
        assert generation.awaiting_since == START
        kinds = [k for k, _ in _events(env, generation_id)]
        assert kinds[-1] == "plan"
        assert "analysis" not in env.agents.calls
        # created_at + 75 minutes + waited (0) + 15 minutes
        until = START + timedelta(minutes=75 + 15)
        lock = env.generations.get_lock(ADMIN.sub)
        assert lock is not None and lock.lock_until == until
        mark = env.files.get(file.source_file_id)
        assert mark is not None and mark.in_use_until == until
        manager.request_control(ADMIN, generation_id, "cancel")
        run.join()

    def test_approve_runs_on_and_adds_the_wait(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file())
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)
        _advance(env, generation_id, minutes=10)  # AC8.1.3: 10 minutes of thought

        manager.request_control(ADMIN, generation_id, "approve")
        run.join()

        generation = _current(env, generation_id)
        assert generation.status is GenerationStatus.DONE
        assert generation.waited_seconds == 600
        assert generation.awaiting_since is None
        assert generation.control_request is None
        stages = [s["status"] for k, s in _events(env, generation_id) if k == "stage"]
        assert stages[:4] == ["preparing", "planning", AWAITING.value, "running"]

    def test_replan_replaces_the_purpose_and_plans_again(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file(), purpose="30 代")
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)

        manager.request_control(ADMIN, generation_id, "replan", revised_purpose="20 代")
        _await_card(env, generation_id, replans=1)
        manager.request_control(ADMIN, generation_id, "replan", revised_purpose="")
        _await_card(env, generation_id, replans=2)

        generation = _current(env, generation_id)
        assert generation.purpose == ""
        assert generation.original_purpose == "30 代"  # only the first is kept
        # every plan was made with the instruction of its time
        purposes = [d["purpose"] for d in env.agents.data_seen]
        assert list(dict.fromkeys(purposes)) == ["30 代", "20 代", ""]
        manager.request_control(ADMIN, generation_id, "approve")
        run.join()
        assert _current(env, generation_id).status is GenerationStatus.DONE

    def test_a_revised_purpose_over_the_limit_is_refused(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file())

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_PURPOSE_TOO_LONG
        ):
            manager.request_control(
                ADMIN, generation_id, "replan", revised_purpose="あ" * 2001
            )

    def test_fifteen_minutes_untouched_stop_the_generation(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        file = env.ready_file()
        generation_id = _start_confirming(manager, file)
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)

        env.clock.advance(minutes=15, seconds=1)
        run.join()

        generation = _current(env, generation_id)
        assert generation.status is GenerationStatus.FAILED
        assert generation.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert generation.error_context == {"limit": "plan_wait_minutes", "value": 15}
        assert generation.awaiting_since is None
        assert len(env.sandbox.stopped) == 1
        assert env.generations.get_lock(ADMIN.sub) is None
        released = env.files.get(file.source_file_id)
        assert released is not None and released.in_use_by is None
        assert "analysis" not in env.agents.calls

    def test_sixty_minutes_of_waits_in_total_stop_the_generation(
        self, env: _Env
    ) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file())
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)
        for n in range(1, 5):
            _advance(env, generation_id, minutes=14)
            manager.request_control(ADMIN, generation_id, "replan", revised_purpose="")
            _await_card(env, generation_id, replans=n)
        # every card extends from the deadline, never shrinking it
        lock = env.generations.get_lock(ADMIN.sub)
        assert lock is not None
        assert lock.lock_until == START + timedelta(minutes=75 + 56 + 15)

        env.clock.advance(minutes=4, seconds=1)  # 56 + 4 minutes and a second
        run.join()

        generation = _current(env, generation_id)
        assert generation.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert generation.error_context == {
            "limit": "plan_wait_total_minutes",
            "value": 60,
        }
        assert generation.waited_seconds == 56 * 60

    def test_the_wait_moves_the_sixty_minute_deadline(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file())
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)
        _advance(env, generation_id, minutes=14)
        # 49 minutes of analysis end 63 minutes after the start: within 60 + 14
        env.agents.on_analysis = lambda: env.clock.advance(minutes=49)

        manager.request_control(ADMIN, generation_id, "approve")
        run.join()

        generation = _current(env, generation_id)
        assert generation.status is GenerationStatus.DONE
        assert generation.waited_seconds == 14 * 60


class TestElapsedWithPlanWaits:
    """W5-1.3: the clock does not count the plan waits."""

    def test_a_waiting_generation_stops_its_clock(self, env: _Env) -> None:
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)
        env.generations.transition(
            generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.PLANNING,
            updates={"waited_seconds": 60},
        )
        env.generations.enter_awaiting(
            generation_id,
            ADMIN.sub,
            [file.source_file_id],
            now=START + timedelta(minutes=3),
            until=START + timedelta(hours=2),
        )
        env.clock.advance(minutes=4)

        elapsed = manager.elapsed_clock(ADMIN, generation_id)

        # 3 minutes until the card, less the 60 seconds waited before
        assert elapsed.seconds == 120 and elapsed.running is False


class TestPlanDecisionOnly:
    """Review fix 3: the plan route takes approve and replan only."""

    def _awaiting(self, env: _Env, manager) -> str:
        file = env.ready_file()
        generation_id = _start(manager, file)
        env.generations.transition(
            generation_id, GenerationStatus.PREPARING, GenerationStatus.PLANNING
        )
        env.generations.enter_awaiting(
            generation_id,
            ADMIN.sub,
            [file.source_file_id],
            now=START,
            until=START + timedelta(hours=1),
        )
        return generation_id

    @pytest.mark.parametrize("action", ["cancel", "", "stop"])
    def test_anything_but_approve_or_replan_is_refused(
        self, env: _Env, action: str
    ) -> None:
        manager = env.manager()
        generation_id = self._awaiting(env, manager)

        with raises_code(AdvancedGenerationError, ErrorCode.REQUEST_INVALID):
            manager.decide_plan(ADMIN, generation_id, action)

        generation = _current(env, generation_id)
        assert generation.status is AWAITING
        assert generation.control_request is None

    def test_approve_and_replan_are_written(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = self._awaiting(env, manager)

        manager.decide_plan(ADMIN, generation_id, "replan", revised_purpose="20 代")

        request = _current(env, generation_id).control_request
        assert request is not None and request.action.value == "replan"
        assert request.revised_purpose == "20 代"

    def test_someone_elses_is_still_not_found(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = self._awaiting(env, manager)

        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.decide_plan(OTHER, generation_id, "approve")


class TestMultiStepPlan:
    """The plan as steps, the candidates and the playbooks read."""

    def test_the_plan_keeps_its_steps_and_the_requests_follow_them(
        self, env: _Env
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        people, buys = env.ready_file(), env.ready_file(data=_purchases_csv())
        gid = _start_detailed(manager, people, buys)
        env.agents.plan = {
            **env.agents.plan,
            "steps": [
                {"kind": "join", "reason": "結合できる"},
                {"kind": "cluster", "reason": "まとめる", "table": "source_1"},
            ],
        }
        env.agents.analysis_fn = _code_analysis(code=_EMIT_JOIN)

        manager.run(gid)

        generation = _current(env, gid)
        assert generation.status is GenerationStatus.DONE
        assert generation.plan is not None
        assert [s.kind.value for s in generation.plan.plan_steps()] == [
            "join",
            "cluster",
        ]
        # the guide's candidates reach the planning request
        assert "1. join:" in env.agents.planning_prompts[0]
        assert 'load_playbook("join")' in env.agents.analysis_prompts[0]
        record = manager.get_record(ADMIN, gid)
        assert [s["kind"] for s in record.plan["steps"]] == ["join", "cluster"]

    @pytest.mark.parametrize(
        ("code", "starts"),
        [
            (_EMIT_JOIN, "結合した列: id, "),
            # a merge of its own: no joined columns, but both tables were used
            (
                'c = read_table("source_1")\nb = read_table("source_2")\n'
                'n = b.groupby("id").size().rename("n").reset_index()\n'
                'df = c.merge(n, on="id", how="left")\n'
                'df["seg"] = ["young" if a < 40 else "old" for a in df["age"]]\n'
                'emit_result(df, "seg", "source_1", id_column="id")\n',
                "使った表: ",
            ),
        ],
    )
    def test_the_record_names_the_tables_the_code_used(
        self, env: _Env, code: str, starts: str
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        people, buys = env.ready_file(), env.ready_file(data=_purchases_csv())
        gid = _start_detailed(manager, people, buys)
        env.agents.analysis_fn = _code_analysis(code=code)

        manager.run(gid)

        assert _current(env, gid).status is GenerationStatus.DONE
        record = manager.get_record(ADMIN, gid)
        (written,) = [b for b in record.code_blocks if b["kind"] == "ai_written"]
        assert written["title"].startswith(starts)
        assert buys.display_name in written["title"]

    def test_code_on_one_table_has_no_table_line(self, env: _Env) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(
            manager, env.ready_file(), env.ready_file(data=_purchases_csv())
        )
        env.agents.analysis_fn = _code_analysis(code=_EMIT)

        manager.run(gid)

        record = manager.get_record(ADMIN, gid)
        (written,) = [b for b in record.code_blocks if b["kind"] == "ai_written"]
        assert not written.get("title")

    def test_without_the_detailed_analysis_no_detailed_step_is_kept(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        gid = _start(manager, env.ready_file())
        env.agents.plan = {
            **env.agents.plan,
            "steps": [
                {"kind": "join", "reason": "結合"},
                {"kind": "cluster", "reason": "まとめる"},
            ],
        }

        manager.run(gid)

        generation = _current(env, gid)
        assert generation.status is GenerationStatus.DONE
        assert generation.plan is not None
        assert [s.kind.value for s in generation.plan.plan_steps()] == ["cluster"]
        prompt = env.agents.planning_prompts[0]
        assert "cluster:" in prompt
        for kind in ("join:", "rfm:", "compare_groups:"):
            assert kind not in prompt

    def test_the_record_names_the_playbooks_read(self, env: _Env) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        analysis = _code_analysis()

        def read_then_analyse(tools: dict, holder: dict, source_id: str):
            tools["load_playbook"](name="rfm")
            tools["load_playbook"](name="../secrets")
            return analysis(tools, holder, source_id)

        env.agents.analysis_fn = read_then_analyse

        manager.run(gid)

        record = manager.get_record(ADMIN, gid)
        loads = [c for c in record.tool_calls if c["tool"] == "load_playbook"]
        assert [c.get("playbook") for c in loads] == ["rfm", None]
        assert all(
            "playbook" not in c
            for c in record.tool_calls
            if c["tool"] != "load_playbook"
        )

    def test_look_arounds_never_take_run_code_off(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())

        def look_around_then_emit(tools: dict, holder: dict, source_id: str):
            for _ in range(6):
                tools["run_code"](code='print(read_table("source_1").shape)')
            return _code_analysis()(tools, holder, source_id)

        env.agents.analysis_fn = look_around_then_emit
        logs = LogCapture(capsys)

        manager.run(gid)

        assert _current(env, gid).status is GenerationStatus.DONE
        assert logs.events("advanced_generation.run_code_disabled") == []
        record = manager.get_record(ADMIN, gid)
        calls = [c for c in record.tool_calls if c["tool"] == "run_code"]
        assert [c.get("error_code") for c in calls] == ["NO_RESULT"] * 6 + [None]
        written = [b for b in record.code_blocks if b["kind"] == "ai_written"]
        assert [b["code"] for b in written] == [_EMIT]
