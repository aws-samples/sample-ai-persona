"""AdvancedPersonaGenerationManager: the preparation of the tables and how they read."""

from __future__ import annotations

import json
import time
from unittest.mock import Mock

import pytest

from src.config import config
from src.managers.advanced_generation.errors import AdvancedGenerationError
from src.models.advanced_generation import GenerationStatus, ResultPartName
from src.models.errors import ErrorCode
from src.services.s3_service import S3ServiceError
from tests.advanced_helpers import SOURCE_FILES_TABLE
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.advanced_generation.helpers import (
    _EMIT_JOIN,
    ADMIN,
    FAST_CONTROL,
    REFRESH_FIELDS,
    _await_card,
    _bad_column_csv,
    _cluster_fallback,
    _code_analysis,
    _csv,
    _current,
    _detailed_env,
    _ended_cleanly,
    _Env,
    _events,
    _failed,
    _purchases_csv,
    _refresh,
    _start,
    _start_confirming,
    _start_detailed,
    _start_with,
    _Thread,
    _transient,
)

pytestmark = pytest.mark.unit


class TestRefreshBeforePreparing:
    def test_copies_the_object_before_the_preparation(
        self, env: _Env, monkeypatch, capsys
    ) -> None:
        logs = LogCapture(capsys)
        manager = env.manager()
        file = env.ready_file()
        order: list[str] = []
        refresh = Mock(side_effect=lambda key: order.append("refresh") or "crc==")
        monkeypatch.setattr(env.s3, "refresh_object", refresh)
        real_start = env.sandbox.start_session
        monkeypatch.setattr(
            env.sandbox,
            "start_session",
            lambda *a, **k: order.append("session") or real_start(*a, **k),
        )
        generation_id = _start(manager, file)

        manager.run(generation_id)

        assert order[:2] == ["refresh", "session"]
        refresh.assert_called_once_with(file.s3_key)
        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.DONE
        (event,) = logs.events("advanced_generation.source_refreshed")
        assert set(event) <= REFRESH_FIELDS
        assert event["status"] == "done" and event["size_bytes"] == file.size_bytes
        assert isinstance(event["duration_ms"], int)

    def test_one_transient_failure_is_sent_again(self, env: _Env, monkeypatch) -> None:
        refresh = _refresh(env, monkeypatch, _transient(), "crc==")
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        assert refresh.call_count == 2
        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.DONE

    def test_two_failures_fail_the_generation_and_release_the_mark(
        self, env: _Env, monkeypatch, capsys
    ) -> None:
        logs = LogCapture(capsys)
        _refresh(env, monkeypatch, _transient(), _transient())
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.FAILED
        assert generation.error_code == ErrorCode.S3_OPERATION_FAILED.value
        assert env.sandbox.download_urls == []
        found = env.files.get(file.source_file_id)
        assert found is not None and found.in_use_by is None
        assert found.checksum == "crc=="
        (event,) = logs.events("advanced_generation.source_refreshed")
        assert event["status"] == "failed"
        assert event["error_code"] == ErrorCode.S3_OPERATION_FAILED.value

    def test_an_object_gone_from_s3_is_unreadable(self, env: _Env, monkeypatch) -> None:
        refresh = _refresh(
            env,
            monkeypatch,
            S3ServiceError("gone", code=ErrorCode.S3_OBJECT_NOT_FOUND),
        )
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        assert refresh.call_count == 1
        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_SOURCE_UNREADABLE.value

    def test_a_different_checksum_is_unreadable_without_a_retry(
        self, env: _Env, monkeypatch
    ) -> None:
        refresh = _refresh(env, monkeypatch, "changed==")
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        assert refresh.call_count == 1
        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.error_code == ErrorCode.GENERATION_SOURCE_UNREADABLE.value
        assert env.sandbox.download_urls == []

    def test_an_empty_checksum_is_filled_and_used_for_the_record(
        self, env: _Env, monkeypatch
    ) -> None:
        _refresh(env, monkeypatch, "new==")
        file = env.ready_file()
        env.files.client.update_item(
            TableName=SOURCE_FILES_TABLE,
            Key={"source_file_id": {"S": file.source_file_id}},
            UpdateExpression="SET checksum = :e",
            ExpressionAttributeValues={":e": {"S": ""}},
        )
        manager = env.manager()
        generation_id = _start(manager, file)

        manager.run(generation_id)

        found = env.files.get(file.source_file_id)
        assert found is not None and found.checksum == "new=="
        record = manager.get_record(ADMIN, generation_id)
        assert record.source_checksums == ("new==",)

    def test_no_checksum_from_s3_is_not_compared(self, env: _Env, monkeypatch) -> None:
        _refresh(env, monkeypatch, None)
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.DONE

    def test_the_record_keeps_the_file_ids_and_display_names(
        self, env: _Env, monkeypatch
    ) -> None:
        _refresh(env, monkeypatch, "crc==")
        file = env.ready_file()
        env.files.client.update_item(
            TableName=SOURCE_FILES_TABLE,
            Key={"source_file_id": {"S": file.source_file_id}},
            UpdateExpression="SET display_seq = :n",
            ExpressionAttributeValues={":n": {"N": "2"}},
        )
        manager = env.manager()
        generation_id = _start(manager, file)

        manager.run(generation_id)

        record = manager.get_record(ADMIN, generation_id)
        assert record.source_file_ids == (file.source_file_id,)
        assert record.source_display_names == ("people.csv (2)",)


class TestPrepareFailureContext:
    """Counts and line numbers reach error_context."""

    def test_too_many_bad_rows_carries_counts_and_line_numbers(self, env: _Env) -> None:
        manager = env.manager()
        file = env.ready_file(data=_bad_column_csv(200, 5))  # 40 bad rows
        generation_id = _start(manager, file)

        manager.run(generation_id)

        generation = _failed(env, generation_id)
        assert generation.error_code == ErrorCode.GENERATION_SOURCE_UNREADABLE.value
        context = generation.error_context or {}
        assert context["rows_read"] == 160
        assert context["rows_excluded"] == 40
        assert context["row_examples"] == [2, 7, 12]
        assert env.agents.calls == []
        _ended_cleanly(env, file)

    def test_one_percent_or_less_goes_on(self, env: _Env) -> None:
        manager = env.manager()
        file = env.ready_file(data=_bad_column_csv(300, 0) + b"1,2,3,4,5\n")
        generation_id = _start(manager, file)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.DONE

    def test_out_of_memory_carries_rows_and_columns(self, env: _Env) -> None:
        env.sandbox.fake_prepare_result = {
            "ok": False,
            "error": {
                "code": "OUT_OF_MEMORY",
                "context": {"rows_read": 1_200_000, "columns_count": 80},
            },
        }
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)

        manager.run(generation_id)

        generation = _failed(env, generation_id)
        assert generation.error_code == ErrorCode.GENERATION_CAPACITY.value
        assert generation.error_context == {
            "rows_read": 1_200_000,
            "columns_count": 80,
        }

    def test_unsafe_values_are_dropped(self, env: _Env) -> None:
        env.sandbox.fake_prepare_result = {
            "ok": False,
            "error": {
                "code": "TOO_MANY_BAD_ROWS",
                "context": {
                    "rows_read": "12",
                    "rows_excluded": 5,
                    "columns_count": True,
                    "row_examples": [1, 2, 3, 4, 5, 6],
                    "secret": 7,
                },
            },
        }
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        context = _failed(env, generation_id).error_context or {}
        assert context.get("rows_excluded") == 5
        assert "rows_read" not in context
        assert "row_examples" not in context
        assert "secret" not in context

    def test_a_list_with_a_non_integer_is_dropped(self, env: _Env) -> None:
        env.sandbox.fake_prepare_result = {
            "ok": False,
            "error": {
                "code": "TOO_MANY_BAD_ROWS",
                "context": {"row_examples": [3, "a row value"]},
            },
        }
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = _failed(env, generation_id)
        assert generation.error_code == ErrorCode.GENERATION_SOURCE_UNREADABLE.value
        assert not generation.error_context

    def test_an_error_without_context_is_as_before(self, env: _Env) -> None:
        env.sandbox.fake_prepare_result = {
            "ok": False,
            "error": {"code": "SOURCE_UNREADABLE"},
        }
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        generation = _failed(env, generation_id)
        assert generation.error_code == ErrorCode.GENERATION_SOURCE_UNREADABLE.value
        assert not generation.error_context

    def test_the_manager_passes_the_limit_to_the_preparation(
        self, env: _Env, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_SOURCE_MAX_EXCLUDED_PERCENT", 50)
        manager = env.manager()
        generation_id = _start(
            manager, env.ready_file(data=_bad_column_csv(200, 5))
        )  # 20% bad

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation is not None and generation.status is GenerationStatus.DONE


class TestInsufficientData:
    """Fewer rows than persona count x 10 stop before planning."""

    def test_ten_rows_for_five_personas_stop(self, env: _Env) -> None:
        manager = env.manager()
        file = env.ready_file(data=_csv(10))
        generation_id = _start(manager, file, count=5)

        manager.run(generation_id)

        generation = _failed(env, generation_id)
        assert generation.error_code == ErrorCode.GENERATION_INSUFFICIENT_DATA.value
        assert generation.error_context == {
            "rows_read": 10,
            "required_rows": 50,
            "persona_count": 5,
        }
        assert env.agents.calls == []
        _ended_cleanly(env, file)

    def test_fifty_rows_for_five_personas_go_on(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file(data=_csv(50)), count=5)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert (
            generation is not None and generation.status is not GenerationStatus.FAILED
        )
        assert "planning" in env.agents.calls

    def test_forty_nine_rows_for_five_personas_stop(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file(data=_csv(49)), count=5)

        manager.run(generation_id)

        code = _failed(env, generation_id).error_code
        assert code == ErrorCode.GENERATION_INSUFFICIENT_DATA.value

    def test_the_bad_row_stop_comes_first(self, env: _Env) -> None:
        # 10 rows, 1 bad (about 9%): both limits are hit, the 1% one is reported
        manager = env.manager()
        data = _bad_column_csv(11, 11)
        generation_id = _start(manager, env.ready_file(data=data), count=5)

        manager.run(generation_id)

        code = _failed(env, generation_id).error_code
        assert code == ErrorCode.GENERATION_SOURCE_UNREADABLE.value

    def test_the_multiplier_comes_from_config(self, env: _Env, monkeypatch) -> None:
        monkeypatch.setattr(config, "ADVANCED_MIN_ROWS_PER_PERSONA", 100)
        manager = env.manager()
        generation_id = _start(manager, env.ready_file(data=_csv(150)), count=2)

        manager.run(generation_id)

        context = _failed(env, generation_id).error_context or {}
        assert context["required_rows"] == 200


class TestPrepareNotice:
    """A notice every 60 seconds while the rows stand still."""

    @staticmethod
    def _messages(env: _Env, generation_id: str) -> list[str]:
        return [
            str(summary.get("message", ""))
            for kind, summary in _events(env, generation_id)
            if kind == "progress"
        ]

    def test_a_notice_every_sixty_seconds_without_new_rows(self, env: _Env) -> None:
        env.sandbox.hold_prepare_rows = 10
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) <= 3:
                env.clock.advance(seconds=60)
            else:
                env.sandbox.hold_prepare_rows = None
                time.sleep(0.01)

        manager = env.manager(sleep=sleep)
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        notices = [
            m for m in self._messages(env, generation_id) if "前準備を続けています" in m
        ]
        assert notices == [
            "前準備を続けています（1 分経過）",
            "前準備を続けています（2 分経過）",
            "前準備を続けています（3 分経過）",
        ]

    def test_no_notice_inside_the_sixty_seconds(self, env: _Env) -> None:
        env.sandbox.hold_prepare_rows = 10
        count = {"n": 0}

        def sleep(seconds: float) -> None:
            count["n"] += 1
            if count["n"] <= 5:
                env.clock.advance(seconds=10)
            else:
                env.sandbox.hold_prepare_rows = None
                time.sleep(0.01)

        manager = env.manager(sleep=sleep)
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        notices = [
            m for m in self._messages(env, generation_id) if "前準備を続けています" in m
        ]
        assert notices == []

    def test_changing_rows_do_not_add_notices(self, env: _Env) -> None:
        env.sandbox.hold_prepare_rows = 10
        count = {"n": 0}

        def sleep(seconds: float) -> None:
            count["n"] += 1
            if count["n"] <= 3:
                env.clock.advance(seconds=60)
                env.sandbox.hold_prepare_rows = 10 + count["n"] * 100
            else:
                env.sandbox.hold_prepare_rows = None
                time.sleep(0.01)

        manager = env.manager(sleep=sleep)
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        notices = [
            m for m in self._messages(env, generation_id) if "前準備を続けています" in m
        ]
        assert notices == []

    def test_the_interval_comes_from_config(self, env: _Env, monkeypatch) -> None:
        monkeypatch.setattr(config, "ADVANCED_GENERATION_PREPARE_NOTICE_SECONDS", 120)
        env.sandbox.hold_prepare_rows = 10
        count = {"n": 0}

        def sleep(seconds: float) -> None:
            count["n"] += 1
            if count["n"] <= 2:
                env.clock.advance(seconds=60)
            else:
                env.sandbox.hold_prepare_rows = None
                time.sleep(0.01)

        manager = env.manager(sleep=sleep)
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        notices = [
            m for m in self._messages(env, generation_id) if "前準備を続けています" in m
        ]
        assert notices == ["前準備を続けています（2 分経過）"]


class TestPreparedLog:
    """Counts and time of the preparation, no values."""

    def test_a_done_preparation_logs_counts_reasons_time_and_memory(
        self, env: _Env, capsys
    ) -> None:
        logs = LogCapture(capsys)
        manager = env.manager()
        data = _bad_column_csv(300, 0) + b"1,2,3,4,5\n"
        generation_id = _start(manager, env.ready_file(data=data))

        manager.run(generation_id)

        (prepared,) = logs.events("advanced_generation.prepared")
        assert prepared["generation_id"] == generation_id
        assert prepared["status"] == "done"
        assert prepared["rows_read"] == 300
        assert prepared["rows_excluded"] == 1
        assert sum(prepared["excluded_reasons"].values()) == 1
        assert isinstance(prepared["duration_ms"], int)
        assert prepared["memory_limit"] == "4GB"
        assert "row_examples" not in prepared

    def test_a_stopped_preparation_logs_counts_from_the_context(
        self, env: _Env, capsys
    ) -> None:
        logs = LogCapture(capsys)
        manager = env.manager()
        generation_id = _start(manager, env.ready_file(data=_bad_column_csv(200, 5)))

        manager.run(generation_id)

        (prepared,) = logs.events("advanced_generation.prepared")
        assert prepared["status"] == "failed"
        assert prepared["error_code"] == ErrorCode.GENERATION_SOURCE_UNREADABLE.value
        assert prepared["rows_read"] == 160 and prepared["rows_excluded"] == 40
        assert "row_examples" not in prepared
        assert "memory_limit" not in prepared

    def test_missing_counts_are_left_out(self, env: _Env, capsys) -> None:
        logs = LogCapture(capsys)
        env.sandbox.fake_prepare_result = {
            "ok": False,
            "error": {"code": "OUT_OF_MEMORY"},
        }
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        manager.run(generation_id)

        (prepared,) = logs.events("advanced_generation.prepared")
        assert prepared["status"] == "failed"
        assert "rows_read" not in prepared and "rows_excluded" not in prepared


class TestDataReading:
    """The plan card shows how the tables read, and the user may fix it."""

    def test_the_card_has_the_proposed_reading(self, env: _Env) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file())
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)

        (reading,) = _current(env, generation_id).data_reading or ()

        assert reading.name == "people.csv" and reading.kind == "people"
        assert reading.person_id == "id" and "id" in reading.skip
        manager.request_control(ADMIN, generation_id, "cancel")
        run.join()

    def test_a_choice_at_approve_is_kept_and_told_to_the_analysis(
        self, env: _Env
    ) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file())
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)

        manager.decide_plan(
            ADMIN,
            generation_id,
            "approve",
            data_reading={
                "source_1": {"kind": "people", "person_id": "id", "skip": ["income"]}
            },
        )
        run.join()

        generation = _current(env, generation_id)
        (reading,) = generation.data_reading or ()
        assert generation.status is GenerationStatus.DONE
        assert reading.skip == ("id", "income")
        assert '"income"' in env.agents.analysis_data["data_reading"]

    def test_a_replan_with_a_corrected_reading_suggests_the_history_steps(
        self, env: _Env
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager(**FAST_CONTROL)
        generation_id = manager.start_generation(
            ADMIN,
            source_file_ids=[env.ready_file().source_file_id],
            purpose="30 代",
            persona_count=2,
            confirm_plan=True,
            use_detailed_analysis=True,
        )
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)
        assert "person_profile:" not in env.agents.planning_prompts[0]

        manager.decide_plan(
            ADMIN,
            generation_id,
            "replan",
            revised_purpose="30 代",
            data_reading={
                "source_1": {"kind": "history", "person_id": "id", "skip": ["id"]}
            },
        )
        _await_card(env, generation_id, replans=1)

        assert "person_profile:" in env.agents.planning_prompts[-1]
        manager.request_control(ADMIN, generation_id, "cancel")
        run.join()

    @pytest.mark.parametrize(
        "choice",
        [
            {"kind": "weird", "person_id": "id", "skip": []},
            {"kind": "people", "person_id": "nope", "skip": []},
            {"kind": "people", "person_id": "id", "skip": ["nope"]},
        ],
    )
    def test_a_choice_the_table_cannot_take_is_refused(
        self, env: _Env, choice: dict
    ) -> None:
        manager = env.manager(**FAST_CONTROL)
        generation_id = _start_confirming(manager, env.ready_file())
        run = _Thread(manager, generation_id)
        _await_card(env, generation_id)

        with raises_code(AdvancedGenerationError, ErrorCode.REQUEST_INVALID):
            manager.decide_plan(
                ADMIN, generation_id, "approve", data_reading={"source_1": choice}
            )
        with raises_code(AdvancedGenerationError, ErrorCode.REQUEST_INVALID):
            manager.decide_plan(
                ADMIN, generation_id, "approve", data_reading={"source_9": choice}
            )

        assert _current(env, generation_id).control_request is None
        manager.request_control(ADMIN, generation_id, "cancel")
        run.join()


class TestSeveralTables:
    def test_tables_are_prepared_in_order_and_the_hash_is_not_stored(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        people, buys = env.ready_file(), env.ready_file(data=_purchases_csv())
        gid = _start_with(manager, people, buys)
        env.agents.analysis_fn = lambda tools, holder, _: _cluster_fallback(
            tools, holder
        )

        manager.run(gid)

        prepared = [a["source_id"] for f, a in env.sandbox.started if f == "prepare"]
        assert prepared == [people.source_file_id, buys.source_file_id]
        assert _current(env, gid).status is GenerationStatus.DONE
        parts = env.generations.read_result_parts(gid, ResultPartName.PREPARE_REPORTS)
        assert len(parts) == 2
        assert all("parquet_sha256" not in p for p in parts)
        assert "key_overlap" not in [f for f, _ in env.sandbox.started]
        assert env.holder["policies"][-1].sources == ("source_1", "source_2")

    def test_a_common_id_column_is_planned_and_recorded_when_on(
        self, env: _Env
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        people, buys = env.ready_file(), env.ready_file(data=_purchases_csv())
        gid = _start_detailed(manager, people, buys)
        env.agents.analysis_fn = _code_analysis()

        manager.run(gid)

        assert "key_overlap" in [f for f, _ in env.sandbox.started]
        plan_data = env.agents.data_seen[0]
        joins = json.loads(plan_data["join_plans"])
        assert joins == [
            {
                "axis": "source_1",
                "table": "source_2",
                "key_column": "id",
                "match_rate": pytest.approx(0.6667, abs=1e-3),
            }
        ]
        # the analysis kept to source_1, so only the plan is kept
        params = _current(env, gid).analysis_run.params
        assert "join" not in params
        plan = params["join_plan"]
        assert plan["axis"] == "source_1"
        assert plan["tables"][0]["table"] == "source_2"
        assert plan["tables"][0]["key_column"] == "id"
        assert plan["tables"][0]["matched_people"] == 200
        assert "joined_columns" not in plan

    def test_a_join_the_analysis_used_is_recorded_as_joined(self, env: _Env) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        people, buys = env.ready_file(), env.ready_file(data=_purchases_csv())
        gid = _start_detailed(manager, people, buys)
        env.agents.analysis_fn = _code_analysis(code=_EMIT_JOIN)

        manager.run(gid)

        params = _current(env, gid).analysis_run.params
        assert "join_plan" not in params
        join = params["join"]
        assert join["axis"] == "source_1"
        assert join["tables"][0]["table"] == "source_2"
        assert join["tables"][0]["matched_people"] == 200
        assert join["joined_columns"][0] == "id"
        assert "source_2_amount_sum" in join["joined_columns"]

    def test_no_common_column_is_analysed_apart(self, env: _Env) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        other = "page,views\n" + "".join(f"p{i},{i}\n" for i in range(40))
        people, pages = env.ready_file(), env.ready_file(data=other.encode())
        gid = _start_detailed(manager, people, pages)
        env.agents.analysis_fn = _code_analysis()

        manager.run(gid)

        assert _current(env, gid).status is GenerationStatus.DONE
        assert "join_plans" not in env.agents.data_seen[0]
        assert "join" not in _current(env, gid).analysis_run.params
