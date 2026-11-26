"""AdvancedPersonaGenerationManager: the cancel, the reader's judgement and the control logs."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from src.config import config
from src.managers.advanced_generation.errors import AdvancedGenerationError
from src.managers.components.analysis_record_archive import record_key
from src.models.advanced_generation import GenerationStatus
from src.models.errors import ErrorCode
from src.services.s3_service import S3ServiceError
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture, assert_not_leaked
from tests.unit.advanced_generation.helpers import (
    ADMIN,
    FAST_CONTROL,
    OTHER,
    START,
    _assert_cancelled_and_released,
    _await_card,
    _current,
    _Env,
    _events,
    _flaky,
    _interview,
    _left_alone,
    _start,
    _start_confirming,
    _start_with,
    _Thread,
    _wait_for,
)

pytestmark = pytest.mark.unit


class TestCancel:
    """Cancel."""

    def test_a_cancel_is_written_for_the_owners_running_generation(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.request_control(ADMIN, generation_id, "cancel")

        request = _current(env, generation_id).control_request
        assert request is not None and request.action.value == "cancel"
        assert request.requested_at == START

    def test_someone_elses_or_a_missing_generation_is_not_found(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.request_control(OTHER, generation_id, "cancel")
        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.request_control(ADMIN, "missing", "cancel")
        assert _current(env, generation_id).control_request is None

    def test_a_finished_generation_cannot_be_cancelled(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        manager.run(generation_id)

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_ALREADY_FINISHED
        ):
            manager.request_control(ADMIN, generation_id, "cancel")

    def test_approve_needs_a_waiting_generation_without_a_cancel(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_NOT_AWAITING_PLAN
        ):
            manager.request_control(ADMIN, generation_id, "approve")
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
        manager.request_control(ADMIN, generation_id, "cancel")
        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_NOT_AWAITING_PLAN
        ):
            manager.request_control(ADMIN, generation_id, "replan", revised_purpose="")

    def test_a_cancel_while_the_plan_waits_ends_it(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        file = env.ready_file()
        generation_id = _start_confirming(manager, file)
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)

        manager.request_control(ADMIN, generation_id, "cancel")
        run.join()

        _assert_cancelled_and_released(env, generation_id, file)
        assert len(env.sandbox.stopped) == 1
        assert "analysis" not in env.agents.calls

    def test_a_cancel_stops_the_preparation_command(self, env: _Env) -> None:
        env.sandbox.hold_prepare_rows = 10
        manager = env.manager(**FAST_CONTROL)
        file = env.ready_file()
        generation_id = _start(manager, file)
        run = _Thread(manager, generation_id)
        _wait_for(lambda: bool(env.sandbox.started))

        manager.request_control(ADMIN, generation_id, "cancel")
        run.join()

        _assert_cancelled_and_released(env, generation_id, file)
        assert len(env.sandbox.cancelled) == 1
        assert env.agents.calls == []
        assert len(env.sandbox.stopped) == 1
        # AC9.3.2: the same file starts again at once
        env.sandbox.hold_prepare_rows = None
        _start(manager, file)

    def test_a_cancel_during_a_model_call_takes_effect_when_it_returns(
        self, env: _Env
    ) -> None:
        manager = env.manager(**FAST_CONTROL)
        file = env.ready_file()
        generation_id = _start(manager, file)

        def cancel_while_running() -> None:
            manager.request_control(ADMIN, generation_id, "cancel")
            time.sleep(0.2)  # the watch reads it while the model "runs"

        env.agents.on_analysis = cancel_while_running
        manager.run(generation_id)

        _assert_cancelled_and_released(env, generation_id, file)
        assert "synthesis" not in env.agents.calls
        with pytest.raises(S3ServiceError):
            env.s3.get_object_tags(record_key(ADMIN.sub, generation_id))

    def test_a_cancel_drops_the_rest_of_the_extraction(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 1)
        manager = env.manager(**FAST_CONTROL)
        doc = env.ready_document(_interview(8))
        generation_id = _start_with(manager, doc)

        def cancel_at_the_first(number: int) -> None:
            if number == 1:
                manager.request_control(ADMIN, generation_id, "cancel")
                time.sleep(0.2)

        env.agents.on_extraction = cancel_at_the_first
        manager.run(generation_id)

        _assert_cancelled_and_released(env, generation_id, doc)
        assert env.agents.calls.count("extraction") <= 2
        assert "planning" not in env.agents.calls

    def test_a_cancel_of_a_generation_without_a_thread_ends_it_at_once(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)
        env.generations.transition(
            generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.PREPARING,
            updates={"session_id": "session-lost"},
        )
        env.clock.advance(minutes=5, seconds=1)  # no heartbeat for 5 min

        manager.request_control(ADMIN, generation_id, "cancel")

        _assert_cancelled_and_released(env, generation_id, file)
        assert env.sandbox.stopped_by_id == ["session-lost"]

    def test_the_watch_survives_failed_reads_and_cancels_later(self, env: _Env) -> None:
        env.sandbox.hold_prepare_rows = 10
        store = _flaky(env)
        store.watch_read_failures = 20
        manager = env.manager(generation_store=store, **FAST_CONTROL)
        file = env.ready_file()
        generation_id = _start(manager, file)
        run = _Thread(manager, generation_id)
        _wait_for(lambda: bool(env.sandbox.started))

        manager.request_control(ADMIN, generation_id, "cancel")
        run.join()

        assert store.watch_read_failures == 0
        _assert_cancelled_and_released(env, generation_id, file)

    def test_failing_heartbeats_do_not_stop_the_run(self, env: _Env) -> None:
        store = _flaky(env)
        store.heartbeat_fails = True
        manager = env.manager(
            generation_store=store, control_poll_seconds=0.001, heartbeat_seconds=0.001
        )
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        assert _current(env, generation_id).status is GenerationStatus.DONE


class TestReaderJudgement:
    """Judgement by a reader."""

    def test_the_session_id_is_kept_once_the_session_starts(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        assert _current(env, generation_id).session_id == env.sandbox.stopped[0]

    def test_five_minutes_without_a_heartbeat_fail_as_stalled(self, env: _Env) -> None:
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)
        _left_alone(env, generation_id)
        env.clock.advance(minutes=5, seconds=1)

        progress = manager.stream_events(ADMIN, generation_id)
        manager.stream_events(ADMIN, generation_id)  # judged once only

        assert progress.generation.status is GenerationStatus.FAILED
        generation = _current(env, generation_id)
        assert generation.error_code == ErrorCode.GENERATION_STALLED.value
        errors = [s for k, s in _events(env, generation_id) if k == "error"]
        assert errors == [{"error_code": "generation_stalled", "kind": "transient"}]
        assert env.generations.get_lock(ADMIN.sub) is None
        released = env.files.get(file.source_file_id)
        assert released is not None and released.in_use_by is None
        assert env.sandbox.stopped_by_id == ["s-lost"]
        assert generation.finished_at is not None

    def test_exactly_five_minutes_is_not_stalled(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        env.clock.advance(minutes=5)

        progress = manager.stream_events(ADMIN, generation_id)

        assert progress.generation.status is GenerationStatus.PREPARING
        assert env.sandbox.stopped_by_id == []

    def test_a_waiting_generation_without_heartbeat_is_stalled_too(
        self, env: _Env
    ) -> None:
        manager = env.manager()
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
            until=START + timedelta(hours=2),
        )
        env.clock.advance(minutes=6)

        assert manager.running_generation_id(ADMIN) is None
        assert _current(env, generation_id).error_code == "generation_stalled"

    def test_the_plan_wait_is_ended_by_a_reader_too(self, env: _Env) -> None:
        manager = env.manager()
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
            until=START + timedelta(hours=2),
        )
        env.clock.advance(minutes=15, seconds=1)
        env.generations.heartbeat(generation_id, now=env.clock.now)  # still alive

        manager.stream_events(ADMIN, generation_id)

        generation = _current(env, generation_id)
        assert generation.status is GenerationStatus.FAILED
        assert generation.error_context == {"limit": "plan_wait_minutes", "value": 15}
        assert env.generations.get_lock(ADMIN.sub) is None

    def test_a_stalled_lock_does_not_stop_the_next_start(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=2)
        file = env.ready_file()
        first = _start(manager, file)
        _left_alone(env, first)
        env.clock.advance(minutes=6)

        second = _start(manager, file)

        assert second != first
        assert _current(env, first).error_code == "generation_stalled"
        lock = env.generations.get_lock(ADMIN.sub)
        assert lock is not None and lock.generation_id == second

    def test_a_running_generation_still_holds_the_lock(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=2)
        file = env.ready_file()
        first = _start(manager, file)
        env.clock.advance(minutes=4)

        with raises_code(
            AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING
        ):
            _start(manager, env.ready_file())
        assert _current(env, first).status is GenerationStatus.PREPARING

    def test_a_plan_request_for_a_stalled_one_is_not_awaiting(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        env.clock.advance(minutes=6)

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_NOT_AWAITING_PLAN
        ):
            manager.request_control(ADMIN, generation_id, "approve")
        assert _current(env, generation_id).error_code == "generation_stalled"

    def test_a_thread_ended_by_a_reader_stops_without_writing(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=1, control_poll_seconds=0.01)
        file = env.ready_file()
        generation_id = _start_confirming(manager, file)
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)  # no heartbeat: heartbeat_seconds is long
        env.clock.advance(minutes=6)

        manager.stream_events(ADMIN, generation_id)
        run.join()

        kinds = [k for k, _ in _events(env, generation_id)]
        assert kinds.count("error") == 1 and "cancelled" not in kinds
        assert _current(env, generation_id).error_code == "generation_stalled"
        assert len(env.sandbox.stopped) == 1  # its own session, stopped quietly
        _start(manager, file)  # the slot went back


class TestControlLogs:
    """Control logs."""

    def test_a_replan_and_a_cancel_are_logged_without_text(
        self, env: _Env, capsys
    ) -> None:
        logs = LogCapture(capsys)
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file())
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)
        manager.request_control(
            ADMIN, generation_id, "replan", revised_purpose="社外秘の指示"
        )
        _await_card(env, generation_id, replans=1)
        manager.request_control(ADMIN, generation_id, "cancel")
        run.join()

        requested = logs.events("advanced_generation.control_requested")
        assert [r["action"] for r in requested] == ["replan", "cancel"]
        taken = logs.events("advanced_generation.control_taken")
        assert [t["action"] for t in taken] == ["replan", "cancel"]
        assert all(isinstance(t["latency_ms"], int) for t in taken)
        (finished,) = logs.events("advanced_generation.finished")
        assert finished["status"] == "cancelled" and finished["replan_count"] == 1
        assert "waited_seconds" in finished
        session_id = _current(env, generation_id).session_id
        assert session_id
        assert_not_leaked("社外秘", logs.records())
        assert_not_leaked(session_id, logs.records())

    def test_the_reader_and_the_plan_wait_are_logged(self, env: _Env, capsys) -> None:
        logs = LogCapture(capsys)
        manager = env.manager()
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
            until=START + timedelta(hours=2),
        )
        env.clock.advance(minutes=15, seconds=1)
        env.generations.heartbeat(generation_id, now=env.clock.now)

        manager.stream_events(ADMIN, generation_id)

        (expired,) = logs.events("advanced_generation.plan_wait_expired")
        assert expired["limit"] == "plan_wait_minutes"
        assert expired["waited_seconds"] == 901
        (reader,) = logs.events("advanced_generation.ended_by_reader")
        assert reader["judged_by"] == "reader"
        assert reader["error_code"] == ErrorCode.GENERATION_TIMEOUT.value
