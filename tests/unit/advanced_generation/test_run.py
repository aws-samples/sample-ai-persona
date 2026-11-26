"""AdvancedPersonaGenerationManager: one run from the start to its end."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from src.config import config
from src.managers.advanced_generation.analysis_policy import AnalysisPolicy
from src.managers.advanced_generation.errors import AdvancedGenerationError
from src.managers.advanced_generation.finishing import FinishingStage
from src.managers.components.analysis_record_archive import record_key
from src.models.advanced_generation import GenerationStatus, ResultPartName
from src.models.errors import ErrorCode
from src.services.stores.advanced_generation_store import AdvancedGenerationStore
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture, assert_not_leaked
from tests.unit.advanced_generation.helpers import (
    _NO_WATCH,
    ADMIN,
    URL_MARK,
    _assert_cancelled_and_released,
    _candidate_count,
    _cluster_fallback,
    _current,
    _Env,
    _events,
    _held_at_stop,
    _model_down,
    _ObservedStore,
    _record_exists,
    _start,
)

pytestmark = pytest.mark.unit


class TestPersonaCheck:
    def test_a_synthesis_that_leaves_a_layer_out_is_written_again_once(
        self, env: _Env
    ) -> None:
        env.agents.synthesis_mode = "same_layer_first"
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.DONE
        assert env.agents.calls.count("synthesis") == 2

    def test_a_synthesis_that_still_leaves_a_layer_out_fails_the_generation(
        self, env: _Env
    ) -> None:
        env.agents.synthesis_mode = "same_layer"
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.FAILED
        assert generation.error_code == ErrorCode.GENERATION_VERIFICATION_FAILED.value
        assert env.agents.calls.count("synthesis") == 2

    def test_a_persona_without_evidence_is_written_again_with_the_reason(
        self, env: _Env, caplog: pytest.LogCaptureFixture
    ) -> None:
        env.agents.synthesis_mode = "no_evidence_first"
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        caplog.set_level("INFO")

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.DONE
        first, again = env.agents.synthesis_prompts
        assert "evidence_ids" in again and "1 人目" in again
        assert "1 人目" not in first
        # how many personas had no evidence, and why (none written / unknown)
        assert "personas without evidence: 1 of 2 (unknown ids 1)" in caplog.text

    def test_a_persona_still_without_evidence_fails_the_generation(
        self, env: _Env
    ) -> None:
        env.agents.synthesis_mode = "no_evidence"
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.FAILED
        assert generation.error_code == ErrorCode.GENERATION_VERIFICATION_FAILED.value
        assert env.agents.calls.count("synthesis") == 2

    def test_a_layer_left_out_is_told_on_the_second_writing(self, env: _Env) -> None:
        env.agents.synthesis_mode = "same_layer_first"
        manager = env.manager()

        manager.run(_start(manager, env.ready_file()))

        first, again = env.agents.synthesis_prompts
        assert "segment_id" in again.split(first, 1)[-1]


class TestRowsAreNotPeopleParam:
    @staticmethod
    def _state(profile: dict[str, Any]) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            budget=None,
            detailed_fallback=None,
            joins=[],
            tables=[SimpleNamespace(alias="source_1", profile=profile)],
            profile=profile,
            reading=AnalysisPolicy.read_tables([("source_1", "f.csv", profile)]),
        )

    def test_a_history_is_recorded_as_counting_rows(self) -> None:
        profile = {
            "columns": [
                {"name": "注文ID", "type": "VARCHAR", "all_distinct": True},
                {"name": "顧客ID", "type": "VARCHAR", "all_distinct": False},
            ]
        }

        params = FinishingStage._extra_run_params(self._state(profile))

        assert params["rows_are_not_people"] is True

    def test_a_table_of_people_records_nothing(self) -> None:
        profile = {
            "columns": [{"name": "顧客ID", "type": "VARCHAR", "all_distinct": True}]
        }

        params = FinishingStage._extra_run_params(self._state(profile))

        assert "rows_are_not_people" not in params


class TestSegmentFeatures:
    def test_the_synthesis_gets_the_features_the_library_counted(
        self, env: _Env
    ) -> None:
        def analysis(tools: dict, holder: dict, source_id: str) -> dict[str, Any]:
            output = _cluster_fallback(tools, holder)
            for segment in output["segments"]:
                # free text the agent wrote: nothing backs it
                segment["features"] = [
                    {"name": "年齢", "value": "43.7歳", "baseline": "別の層は41歳"}
                ]
            return output

        env.agents.analysis_fn = analysis
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        synthesis = next(
            d for d in env.agents.data_seen if "segments" in d and "purpose" in d
        )
        segments = json.loads(synthesis["segments"])
        features = [f for s in segments for f in s["features"]]
        assert features, "the cluster features reach the synthesis"
        assert all(f["kind"] in ("mean", "share") for f in features)
        assert all(isinstance(f["value"], float) for f in features)
        assert not any("別の層" in str(f) for f in features)


class TestRun:
    def test_runs_every_stage_and_writes_the_result(self, env: _Env, capsys) -> None:
        logs = LogCapture(capsys)
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.DONE
        assert generation.finished_at is not None
        assert generation.plan is not None
        assert generation.analysis_record_id == generation_id
        assert len(generation.candidate_ids or ()) == 2
        kinds = [kind for kind, _ in _events(env, generation_id)]
        stages = [s["status"] for k, s in _events(env, generation_id) if k == "stage"]
        assert stages == [
            "preparing",
            "planning",
            "running",
            "synthesizing",
            "verifying",
        ]
        assert "progress" in kinds and "plan" in kinds and kinds[-1] == "result"
        # The end released the lock and the mark and stopped the session.
        assert env.generations.get_lock(ADMIN.sub) is None
        released = env.files.get(file.source_file_id)
        assert released is not None and released.in_use_by is None
        assert len(env.sandbox.stopped) == 1
        # Result parts, candidates and the pending record.
        segments = env.generations.read_result_parts(
            generation_id, ResultPartName.SEGMENTS
        )
        assert sum(s["size"] for s in segments) == 300
        candidate = env.candidates.get((generation.candidate_ids or ())[0])
        assert candidate is not None and candidate.owner_sub == ADMIN.sub
        context = candidate.generation_context or {}
        assert context["mode"] == "advanced"
        assert context["analysis_record_id"] == generation_id
        assert context["segment_id"] in {s["id"] for s in segments}
        tags = env.s3.get_object_tags(record_key(ADMIN.sub, generation_id))
        assert tags == {"pending": "true"}
        # The download URL was the presigned GET of the file, made for 900 s.
        assert len(env.sandbox.download_urls) == 1
        assert file.s3_key in env.sandbox.download_urls[0]
        # Only the allowed events, and no URL in logs, events or the item.
        stage_logs = logs.events("advanced_generation.stage")
        assert {r["stage"] for r in stage_logs} >= {"preparing", "verifying"}
        (finished,) = logs.events("advanced_generation.finished")
        assert finished["status"] == "done" and finished["rows_read"] == 300
        assert_not_leaked(
            URL_MARK,
            logs.records(),
            [e.to_dict() for e in env.generations.read_events(generation_id)],
            generation.to_dict(),
            env.s3.download_file(
                env.s3.path_of(record_key(ADMIN.sub, generation_id))
            ).decode(),
        )
        # The next generation of the same user can start.
        _start(manager, env.ready_file())

    def test_zero_segments_fail_as_analysis_failed(self, env: _Env) -> None:
        env.agents.analysis_mode = "zero"
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.FAILED
        assert generation.error_code == ErrorCode.GENERATION_ANALYSIS_FAILED.value
        kind, summary = _events(env, generation_id)[-1]
        assert kind == "error"
        assert summary["error_code"] == ErrorCode.GENERATION_ANALYSIS_FAILED.value
        assert env.generations.get_lock(ADMIN.sub) is None
        assert len(env.sandbox.stopped) == 1

    def test_persona_without_segment_fails_the_verification(self, env: _Env) -> None:
        env.agents.synthesis_mode = "orphan"
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_VERIFICATION_FAILED.value

    def test_sixty_minutes_is_a_timeout(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        env.sandbox.on_profile = lambda: env.clock.advance(minutes=61)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert generation.error_context == {"limit": "generation_minutes", "value": 60}
        assert env.agents.calls == []

    def test_preparation_over_twenty_minutes_is_a_timeout(self, env: _Env) -> None:
        # the 20-minute preparation limit is a timeout
        env.sandbox.hold_prepare_rows = 1000
        manager = env.manager(
            sleep=lambda seconds: env.clock.advance(minutes=5),
        )
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.FAILED
        assert generation.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert generation.error_context == {"limit": "prepare_minutes", "value": 20}
        assert len(env.sandbox.cancelled) == 1
        assert env.agents.calls == []
        # the end still runs: the session stops, the lock goes, finished_at is set
        assert len(env.sandbox.stopped) == 1
        assert generation.finished_at is not None
        assert env.generations.get_lock(ADMIN.sub) is None
        kinds = [kind for kind, _ in _events(env, generation_id)]
        assert "error" in kinds

    def test_preparation_is_not_stopped_before_twenty_minutes(self, env: _Env) -> None:
        env.sandbox.hold_prepare_rows = 10
        polls: list[float] = []

        def sleep(seconds: float) -> None:
            polls.append(seconds)
            if env.sandbox.hold_prepare_rows is None:
                time.sleep(0.01)  # the real preparation is finishing
                return
            env.clock.advance(minutes=5)
            if len(polls) == 3:  # 15 minutes in: let the preparation finish
                env.sandbox.hold_prepare_rows = None

        manager = env.manager(sleep=sleep)
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.DONE
        assert env.sandbox.cancelled == []

    def test_preparation_is_polled_at_the_configured_interval(
        self, env: _Env, monkeypatch
    ) -> None:
        # the interval comes from config, not from the manager
        monkeypatch.setattr(config, "ADVANCED_GENERATION_PREPARE_POLL_SECONDS", 2)
        env.sandbox.hold_prepare_rows = 10
        polls: list[float] = []

        def sleep(seconds: float) -> None:
            polls.append(seconds)
            env.sandbox.hold_prepare_rows = None
            time.sleep(0.01)

        manager = env.manager(sleep=sleep, poll_seconds=None)
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        assert polls and polls[0] == 2

    def test_no_model_stage_starts_with_under_ten_minutes_left(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        env.sandbox.on_profile = lambda: env.clock.advance(minutes=51)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert env.agents.calls == []

    def test_unreadable_table_is_source_unreadable(self, env: _Env) -> None:
        manager = env.manager()
        file = env.ready_file(data=b"id,age\n")
        generation_id = _start(manager, file)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_SOURCE_UNREADABLE.value

    def test_model_failure_keeps_only_code_and_cause(self, env: _Env, caplog) -> None:
        env.agents.fail_planning = True
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_SANDBOX_UNAVAILABLE.value
        assert generation.error_context == {"cause": "model"}
        assert_not_leaked("ThrottlingException", generation.to_dict())

    def test_refused_download_gets_one_new_url(self, env: _Env) -> None:
        env.sandbox.refuse_downloads = 1
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.DONE
        assert len(env.sandbox.download_urls) == 2

    def test_second_refused_download_fails(self, env: _Env) -> None:
        env.sandbox.refuse_downloads = 2
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_SANDBOX_UNAVAILABLE.value
        assert generation.error_context == {
            "cause": "code_interpreter",
            "http_status": 403,
        }

    def test_failed_stop_is_logged_and_the_end_still_runs(
        self, env: _Env, caplog
    ) -> None:
        env.sandbox.stop_error = RuntimeError("stop failed")
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.DONE
        assert generation.finished_at is not None
        assert env.generations.get_lock(ADMIN.sub) is None
        assert "RuntimeError" in caplog.text
        assert "stop failed" not in caplog.text

    def test_result_over_the_size_limit_fails(self, env: _Env, monkeypatch) -> None:
        monkeypatch.setattr(config, "ADVANCED_GENERATION_PARTS_TOTAL_MAX_BYTES", 100)
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_RESULT_TOO_LARGE.value

    def test_generation_item_over_its_size_limit_fails_before_done(
        self, env: _Env, monkeypatch
    ) -> None:
        # an item is capped at 64KB: measure the body written at done first
        monkeypatch.setattr(config, "ADVANCED_GENERATION_ITEM_MAX_BYTES", 600)
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.FAILED
        assert generation.error_code == ErrorCode.GENERATION_RESULT_TOO_LARGE.value
        assert generation.analysis_run is None
        assert generation.candidate_ids is None
        for name in ResultPartName:
            assert env.generations.read_result_parts(generation_id, name) == []

    def test_generation_item_within_its_size_limit_is_done(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.DONE
        size = AdvancedGenerationStore.item_size(generation.to_dict())
        assert 0 < size <= config.ADVANCED_GENERATION_ITEM_MAX_BYTES

    def test_heartbeat_is_written_while_running(self, env: _Env) -> None:
        manager = env.manager(heartbeat_seconds=0.005)
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.heartbeat_at is not None

    def test_abort_start_fails_it_and_returns_the_slot(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=1)
        generation_id = _start(manager, env.ready_file())

        manager.abort_start(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.FAILED
        assert env.generations.get_lock(ADMIN.sub) is None
        _start(manager, env.ready_file())  # the slot is free again


class TestStageProgressMessages:
    """The screen's log says what is going on in every stage, not only the rows."""

    def test_every_stage_tells_what_it_does_without_the_users_data(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        messages = [
            str(s.get("message", ""))
            for k, s in _events(env, generation_id)
            if k == "progress"
        ]
        text = "\n".join(messages)
        assert "分析の計画を作っています" in text
        assert "計画ができました" in text
        assert "分析を始めます" in text
        assert "分析が終わりました" in text
        assert "ペルソナ" in text and "作っています" in text
        assert "検証" in text
        # the order follows the stages
        order = [
            next(i for i, m in enumerate(messages) if needle in m)
            for needle in (
                "分析の計画を作っています",
                "計画ができました",
                "分析を始めます",
                "分析が終わりました",
                "検証できました",
            )
        ]
        assert order == sorted(order)

    def test_each_tool_call_is_told_as_it_finishes(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        messages = [
            str(s.get("message", ""))
            for k, s in _events(env, generation_id)
            if k == "progress"
        ]
        calls = len(env.holder["service"].calls)
        told = [m for m in messages if m.startswith("操作 ")]
        assert calls >= 1 and len(told) == calls


class TestEndOrder:
    """Review fix 1: the holds go before the end shows and the session stops."""

    def test_a_failure_releases_before_the_error_and_the_session_stop(
        self, env: _Env
    ) -> None:
        store = _ObservedStore(env)
        manager = env.manager(generation_store=store)
        file = env.ready_file()
        generation_id = _start(manager, file)
        stops = _held_at_stop(env, file)
        env.agents.on_analysis = _model_down

        manager.run(generation_id)

        assert _current(env, generation_id).status is GenerationStatus.FAILED
        assert ("error", False) in store.held_at_event
        assert stops == [False]
        # the same file starts again at once (no 409 after a failed run)
        _start(manager, file)

    def test_done_releases_before_the_result_and_the_session_stop(
        self, env: _Env
    ) -> None:
        store = _ObservedStore(env)
        manager = env.manager(generation_store=store)
        file = env.ready_file()
        generation_id = _start(manager, file)
        stops = _held_at_stop(env, file)

        manager.run(generation_id)

        generation = _current(env, generation_id)
        assert generation.status is GenerationStatus.DONE
        assert generation.finished_at is not None
        assert ("result", False) in store.held_at_event
        assert stops == [False]

    def test_a_start_frees_a_lock_left_by_an_ended_generation(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=2)
        file = env.ready_file()
        first = _start(manager, file)
        # ended, but its lock and mark were not released (yet)
        env.generations.transition(
            first, GenerationStatus.PREPARING, GenerationStatus.FAILED
        )

        second = _start(manager, file)

        lock = env.generations.get_lock(ADMIN.sub)
        assert lock is not None and lock.generation_id == second
        mark = env.files.get(file.source_file_id)
        assert mark is not None and mark.in_use_by == second
        assert _current(env, first).status is GenerationStatus.FAILED

    def test_a_live_generations_lock_still_refuses_the_start(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=2)
        file = env.ready_file()
        first = _start(manager, file)

        with raises_code(
            AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING
        ):
            _start(manager, env.ready_file())
        lock = env.generations.get_lock(ADMIN.sub)
        assert lock is not None and lock.generation_id == first


class TestNothingKeptAfterALateEnd:
    """Nothing is kept after the last checkpoint either."""

    def _stall(self, env: _Env, manager, generation_id: str) -> None:
        env.clock.advance(minutes=6)  # no heartbeat: a reader ends it
        manager.stream_events(ADMIN, generation_id)

    def _assert_nothing_kept(self, env: _Env, generation_id: str) -> None:
        generation = _current(env, generation_id)
        assert generation.candidate_ids is None
        assert _candidate_count(env) == 0
        assert not _record_exists(env, generation_id)
        kinds = [k for k, _ in _events(env, generation_id)]
        assert "result" not in kinds

    def test_an_end_by_a_reader_before_the_results_keeps_nothing(
        self, env: _Env
    ) -> None:
        store = _ObservedStore(env)
        manager = env.manager(generation_store=store, **_NO_WATCH)
        file = env.ready_file()
        generation_id = _start(manager, file)
        store.after_verifying = lambda: self._stall(env, manager, generation_id)

        manager.run(generation_id)

        assert _current(env, generation_id).error_code == "generation_stalled"
        self._assert_nothing_kept(env, generation_id)
        _start(manager, file)

    def test_an_end_by_a_reader_while_writing_removes_what_was_written(
        self, env: _Env
    ) -> None:
        store = _ObservedStore(env)
        manager = env.manager(generation_store=store, **_NO_WATCH)
        generation_id = _start(manager, env.ready_file())
        store.before_parts = lambda: self._stall(env, manager, generation_id)

        manager.run(generation_id)

        assert _current(env, generation_id).error_code == "generation_stalled"
        self._assert_nothing_kept(env, generation_id)

    def test_a_cancel_before_the_results_ends_cancelled(self, env: _Env) -> None:
        store = _ObservedStore(env)
        manager = env.manager(generation_store=store, **_NO_WATCH)
        file = env.ready_file()
        generation_id = _start(manager, file)
        store.after_verifying = lambda: manager.request_control(
            ADMIN, generation_id, "cancel"
        )

        manager.run(generation_id)

        _assert_cancelled_and_released(env, generation_id, file)
        self._assert_nothing_kept(env, generation_id)

    def test_a_cancel_while_writing_ends_cancelled_and_removes_them(
        self, env: _Env
    ) -> None:
        store = _ObservedStore(env)
        manager = env.manager(generation_store=store, **_NO_WATCH)
        file = env.ready_file()
        generation_id = _start(manager, file)
        store.before_parts = lambda: manager.request_control(
            ADMIN, generation_id, "cancel"
        )

        manager.run(generation_id)

        _assert_cancelled_and_released(env, generation_id, file)
        self._assert_nothing_kept(env, generation_id)
