"""AdvancedPersonaGenerationManager: the AI detailed analysis and an unreachable sandbox."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.models.advanced_generation import GenerationStatus
from src.models.errors import ErrorCode
from tests.log_helpers import LogCapture, assert_not_leaked
from tests.unit.advanced_generation.helpers import (
    _ALLOWED,
    _DISABLED_NOTICE,
    _EMIT,
    _ESCAPE_THEN_EMIT,
    ADMIN,
    _call_id,
    _candidate_count,
    _cluster_fallback,
    _code_analysis,
    _current,
    _detailed_env,
    _Env,
    _failed,
    _FailingCheckSandbox,
    _interview,
    _NoLibraryHashSandbox,
    _NoParquetHashSandbox,
    _progress,
    _purchases_csv,
    _record_exists,
    _ScanSandbox,
    _start,
    _start_detailed,
    _UnreachableSandbox,
    _with_sandbox,
)

pytestmark = pytest.mark.unit


class TestSandboxUnreachable:
    """Unreachable Code Interpreter."""

    def test_only_the_advanced_generation_fails(self, env: _Env, capsys) -> None:
        from src.managers import persona_generation_manager as ordinary

        logs = LogCapture(capsys)
        env.sandbox = _UnreachableSandbox(
            read_object=lambda key: b"", root_dir=env.sandbox.root_dir
        )
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        agent = Mock()
        agent.create_generation_agent.return_value = Mock()
        agent.run_persona_generation.return_value = (
            ordinary._PersonaListOutput(
                personas=[
                    ordinary._PersonaOutput(
                        name="田中",
                        age=30,
                        occupation="会社員",
                        background="背景",
                        values=["v"],
                        pain_points=["p"],
                        goals=["g"],
                    )
                ]
            ),
            [],
        )
        normal = ordinary.PersonaGenerationManager(
            agent_service=agent,
            dataset_analysis_service=Mock(),
            candidate_store=env.candidates,
            s3_service=Mock(),
            clock=env.clock,
        )

        manager.run(generation_id)
        result = normal.generate(
            ADMIN,
            file_contents=[("十分な長さのインタビュー本文です。".encode(), "a.txt")],
            data_type="interview",
            persona_count=1,
        )

        generation = _current(env, generation_id)
        assert generation.status is GenerationStatus.FAILED
        assert generation.error_code == ErrorCode.GENERATION_SANDBOX_UNAVAILABLE.value
        assert generation.error_context == {"cause": "code_interpreter"}
        assert len(result.personas) == 1
        # AC9.5.1: the exception text is nowhere
        assert_not_leaked(
            "ConnectTimeoutError",
            logs.records(),
            [e.to_dict() for e in env.generations.read_events(generation_id)],
            generation.to_dict(),
        )
        assert env.generations.get_lock(ADMIN.sub) is None


class TestDetailedAnalysisChoice:
    """Decided at the start and kept on the generation."""

    def test_allowed_code_interpreter_and_checked_is_on(self, env: _Env) -> None:
        env = _detailed_env(env)

        gid = _start_detailed(env.manager(), env.ready_file())

        assert _current(env, gid).use_detailed_analysis is True

    @pytest.mark.parametrize("case", ["not_allowed", "in_process", "unchecked"])
    def test_otherwise_it_is_off(self, env: _Env, case: str) -> None:
        if case != "in_process":
            env = _detailed_env(env)
        env.settings.get.return_value = {
            **_ALLOWED,
            "allow_detailed_analysis": case != "not_allowed",
        }

        gid = _start_detailed(
            env.manager(), env.ready_file(), checked=case != "unchecked"
        )

        assert _current(env, gid).use_detailed_analysis is False

    def test_documents_only_is_off(self, env: _Env) -> None:
        env = _detailed_env(env)

        gid = _start_detailed(env.manager(), env.ready_document(_interview()))

        assert _current(env, gid).use_detailed_analysis is False

    def test_a_change_of_the_setting_does_not_reach_a_started_run(
        self, env: _Env
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        env.settings.get.return_value = {**_ALLOWED, "allow_detailed_analysis": False}
        env.agents.analysis_fn = _code_analysis()

        manager.run(gid)

        assert _current(env, gid).status is GenerationStatus.DONE
        assert "run_code" in env.agents.tools

    def test_off_gives_no_run_code_to_the_agent(self, env: _Env) -> None:
        env.settings.get.return_value = {**_ALLOWED, "allow_detailed_analysis": False}
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())

        manager.run(gid)

        assert "run_code" not in env.agents.tools
        assert "load_playbook" not in env.agents.tools


class TestRunCodeInTheGeneration:
    def test_five_failures_go_on_with_the_fixed_functions(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        env.agents.analysis_fn = _code_analysis(failures=6)
        logs = LogCapture(capsys)

        manager.run(gid)

        generation = _current(env, gid)
        assert generation.status is GenerationStatus.DONE
        assert _progress(env, gid).count(_DISABLED_NOTICE) == 1
        (disabled,) = logs.events("advanced_generation.run_code_disabled")
        assert disabled["status"] == "disabled"
        assert disabled["reason"] == "max_failures" and disabled["failures"] == 5
        params = generation.analysis_run.params["detailed_analysis"]
        assert params == {"used": True, "fell_back": True, "failures": 5}
        (finished,) = logs.events("advanced_generation.finished")
        assert finished["run_code_disabled"] is True
        assert finished["run_code_failures"] == 5
        assert finished["detailed_analysis_used"] is True

    def test_failures_never_reach_the_synthesis_or_the_record(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        env.agents.analysis_fn = _code_analysis(failures=3)
        logs = LogCapture(capsys)

        manager.run(gid)

        assert _current(env, gid).status is GenerationStatus.DONE
        synthesis = json.dumps(env.agents.data_seen[-1], ensure_ascii=False)
        records = logs.records()
        for secret in ("boom-secret", "print-secret", "raise ValueError"):
            assert secret not in synthesis
            assert_not_leaked(secret, records)
        record = manager.get_record(ADMIN, gid)
        written = [b for b in record.code_blocks if b["kind"] == "ai_written"]
        assert [b["code"] for b in written] == [_EMIT]
        calls = [c for c in record.tool_calls if c["tool"] == "run_code"]
        assert [c["ok"] for c in calls] == [False, False, False, True]
        assert all("error_code" in c for c in calls[:3])
        run = _current(env, gid).analysis_run
        assert written[0]["sha256"] in run.code_sha256

    def test_a_changed_parquet_fails_without_a_record(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        file = env.ready_file()
        gid = _start_detailed(manager, file)
        env.agents.analysis_fn = lambda tools, holder, source_id: (
            tools["run_code"](
                code="open('../../prepared/" + file.source_file_id + ".parquet', 'ab')"
                ".write(b'x')"
            ),
            _cluster_fallback(tools, holder),
        )[1]
        logs = LogCapture(capsys)

        manager.run(gid)

        assert (
            _failed(env, gid).error_code == ErrorCode.GENERATION_ANALYSIS_FAILED.value
        )
        assert _candidate_count(env) == 0 and not _record_exists(env, gid)
        (tamper,) = logs.events("advanced_generation.tamper_detected")
        assert tamper["status"] == "detected" and tamper["changed"] == ["parquet"]

    def test_a_change_after_the_last_run_is_found_before_the_synthesis(
        self, env: _Env
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        file = env.ready_file()
        gid = _start_detailed(manager, file)

        def tamper(holder: dict) -> None:
            session = env.sandbox._workdirs
            (workdir,) = session.values()
            parquet = Path(workdir) / "prepared" / f"{file.source_file_id}.parquet"
            with parquet.open("ab") as f:
                f.write(b"x")

        env.agents.analysis_fn = _code_analysis(after=tamper)

        manager.run(gid)

        assert (
            _failed(env, gid).error_code == ErrorCode.GENERATION_ANALYSIS_FAILED.value
        )
        assert "synthesis" not in env.agents.calls
        assert _candidate_count(env) == 0


class TestDetailedAnalysisFailsClosed:
    """No hash to compare and a failed check."""

    @pytest.mark.parametrize("cls", [_NoParquetHashSandbox, _NoLibraryHashSandbox])
    def test_without_a_hash_the_fixed_functions_go_on(
        self, env: _Env, capsys: pytest.CaptureFixture[str], cls: type
    ) -> None:
        env = _with_sandbox(env, cls)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        env.agents.analysis_fn = lambda tools, holder, _: _cluster_fallback(
            tools, holder
        )
        logs = LogCapture(capsys)

        manager.run(gid)

        generation = _current(env, gid)
        assert generation.status is GenerationStatus.DONE
        assert "run_code" not in env.agents.tools
        assert "load_playbook" not in env.agents.tools
        assert _progress(env, gid).count(_DISABLED_NOTICE) == 1
        (disabled,) = logs.events("advanced_generation.run_code_disabled")
        assert disabled["status"] == "disabled"
        assert disabled["reason"] == "missing_hash" and disabled["failures"] == 0
        assert generation.analysis_run.params["detailed_analysis"] == {
            "used": False,
            "fell_back": True,
            "failures": 0,
            "reason": "missing_hash",
        }
        (finished,) = logs.events("advanced_generation.finished")
        assert finished["run_code_disabled"] is True
        assert logs.events("advanced_generation.tamper_detected") == []

    @pytest.mark.parametrize("failing", ["verify_hashes", "finalize_ai_result"])
    def test_a_failed_check_fails_the_generation_but_is_no_tamper(
        self, env: _Env, capsys: pytest.CaptureFixture[str], failing: str
    ) -> None:
        env = _with_sandbox(env, _FailingCheckSandbox)
        env.sandbox.failing = failing
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        env.agents.analysis_fn = _code_analysis()
        logs = LogCapture(capsys)

        manager.run(gid)

        assert (
            _failed(env, gid).error_code == ErrorCode.GENERATION_ANALYSIS_FAILED.value
        )
        assert "synthesis" not in env.agents.calls
        assert _candidate_count(env) == 0 and not _record_exists(env, gid)
        assert logs.events("advanced_generation.tamper_detected") == []
        (disabled,) = logs.events("advanced_generation.run_code_disabled")
        assert disabled["status"] == "disabled"
        assert disabled["reason"] == "verify_failed"

    def test_a_process_left_at_the_final_check_fails_as_a_tamper(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _with_sandbox(env, _ScanSandbox)
        env.sandbox.scan = {"process_scan": "ok", "leftover_processes": 1}
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        env.agents.analysis_fn = _code_analysis()
        logs = LogCapture(capsys)

        manager.run(gid)

        assert (
            _failed(env, gid).error_code == ErrorCode.GENERATION_ANALYSIS_FAILED.value
        )
        assert "synthesis" not in env.agents.calls
        (tamper,) = logs.events("advanced_generation.tamper_detected")
        assert tamper["changed"] == ["process"]

    @pytest.mark.parametrize(
        "scan",
        [{"process_scan": "unavailable"}, {}],
        ids=["unavailable", "missing"],
    )
    def test_a_final_scan_that_could_not_run_fails_closed_but_is_no_tamper(
        self, env: _Env, capsys: pytest.CaptureFixture[str], scan: dict
    ) -> None:
        env = _with_sandbox(env, _ScanSandbox)
        env.sandbox.scan = scan
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        env.agents.analysis_fn = _code_analysis()
        logs = LogCapture(capsys)

        manager.run(gid)

        assert (
            _failed(env, gid).error_code == ErrorCode.GENERATION_ANALYSIS_FAILED.value
        )
        assert logs.events("advanced_generation.tamper_detected") == []
        (disabled,) = logs.events("advanced_generation.run_code_disabled")
        assert disabled["reason"] == "verify_failed"

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork (POSIX)")
    def test_code_that_leaves_a_process_fails_the_generation(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        children: list[int] = []

        def collect(holder: dict) -> None:
            # read before the session folder goes, to stop them in any case
            root = Path(env.sandbox.root_dir)
            children.extend(
                int(p.read_text()) for p in root.glob("*/ai_work/*/child.pid")
            )

        env.agents.analysis_fn = _code_analysis(code=_ESCAPE_THEN_EMIT, after=collect)
        logs = LogCapture(capsys)
        try:
            manager.run(gid)

            assert children
            assert (
                _failed(env, gid).error_code
                == ErrorCode.GENERATION_ANALYSIS_FAILED.value
            )
            assert "synthesis" not in env.agents.calls
            (tamper,) = logs.events("advanced_generation.tamper_detected")
            assert tamper["changed"] == ["process"]
        finally:
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


_EMIT_PEOPLE = (
    'p = person_profile(read_table("source_1"), "id")\n'
    'p["seg"] = ["low" if v < 340 else "high" for v in p["amountの合計"]]\n'
    'emit_result(p, "seg", "people:source_1", id_column="id")\n'
)


def _history_csv(people: int = 40, *, without_id: int = 2, extra: str = "") -> bytes:
    """Only a history (two rows per person), with rows that name no one."""
    data = _purchases_csv(people).decode() + ",999\n" * without_id + extra
    return data.encode()


def _rows_cluster(tools: dict, holder: dict, source_id: str) -> dict:
    """The fixed cluster on the history's rows (no run_code result)."""
    text = tools["cluster_segments"](columns=["amount"], source="source_1")
    call_id = _call_id(text)
    output = holder["service"].output_of(call_id)
    segments, evidence = [], []
    for cluster in output["clusters"]:
        i = cluster["index"]
        segments.append(
            {
                "id": f"seg-{i}",
                "label": f"層 {i}",
                "definition": {
                    "kind": "cluster",
                    "source": "source_1",
                    "spec": {
                        "tool_call_id": call_id,
                        "cluster_index": i,
                    },
                },
                "size": cluster["size"],
                "share": cluster["share"],
                "features": [],
                "evidence_ids": [f"ev-{i}"],
            }
        )
        evidence.append(
            {
                "id": f"ev-{i}",
                "source_id": "x",
                "location": call_id,
                "kind": "attribute",
                "claim": "金額",
            }
        )
    return {"segments": segments, "evidence": evidence}


class TestPeopleOfAHistory:
    """With only a history table, its people are the population (people:)."""

    def test_the_people_back_the_segments_and_the_record_tells_the_rows_left_out(
        self, env: _Env
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file(data=_history_csv()))
        env.agents.analysis_fn = _code_analysis(code=_EMIT_PEOPLE)

        manager.run(gid)

        generation = _current(env, gid)
        assert generation.status is GenerationStatus.DONE
        params = generation.analysis_run.params
        assert params["people_from_history"] == {
            "table": "people.csv",
            "id_column": "id",
            "people": 40,
            "source_rows": 82,
            "rows_without_id": 2,
            "one_id_holds_many_rows": False,
        }
        # the counts are of people now, not of rows
        assert "rows_are_not_people" not in params
        result = manager.get_result(ADMIN, gid)
        assert sorted(s.size for s in result.segments) == [20, 20]

    def test_one_id_with_a_large_share_of_the_rows_is_pointed_out(
        self, env: _Env
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        # an ID that may stand for "no one" (such as guests) holds many rows
        data = _history_csv(extra="0,5\n" * 30)
        gid = _start_detailed(manager, env.ready_file(data=data))
        env.agents.analysis_fn = _code_analysis(code=_EMIT_PEOPLE)

        manager.run(gid)

        params = _current(env, gid).analysis_run.params["people_from_history"]
        assert params["one_id_holds_many_rows"] is True

    def test_fixed_functions_on_a_history_still_count_rows(self, env: _Env) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file(data=_history_csv()))
        env.agents.analysis_fn = _rows_cluster

        manager.run(gid)

        params = _current(env, gid).analysis_run.params
        assert params.get("rows_are_not_people") is True
        assert "people_from_history" not in params


_EMIT_HISTORY_AS_PEOPLE = (
    'df = read_table("source_1")\ndf["seg"] = "a"\n'
    'emit_result(df, "seg", "source_1", id_column="id")\n'
)


def _refused_then_rows(tools: dict, holder: dict, source_id: str) -> dict:
    """A refused run_code result, then the fixed cluster on the rows."""
    tools["run_code"](code=_EMIT_HISTORY_AS_PEOPLE)
    return _rows_cluster(tools, holder, source_id)


class TestRecordShowsWhatEachStepFound:
    """The record keeps counted aggregates of each step, never rows."""

    def test_an_accepted_result_and_the_tables_reading_are_in_the_record(
        self, env: _Env
    ) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file(data=_history_csv()))
        env.agents.analysis_fn = _code_analysis(code=_EMIT_PEOPLE)

        manager.run(gid)

        record = manager.get_record(ADMIN, gid)
        (table,) = record.table_summaries
        assert table["name"] == "people.csv" and table["rows_read"] == 82
        assert table["missing"] == [{"name": "id", "count": 2}]
        (run,) = [c for c in record.tool_calls if c["tool"] == "run_code"]
        result = run["result"]
        assert result["population"] == 40 and result["unit"] == "people"
        assert result["history"] == {
            "id_column": "id",
            "source_rows": 82,
            "rows_without_id": 2,
        }
        assert sorted(g["size"] for g in result["groups"]) == [20, 20]

    def test_a_refused_result_keeps_its_reason_and_counts_only(self, env: _Env) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file(data=_history_csv()))
        env.agents.analysis_fn = _refused_then_rows

        manager.run(gid)

        record = manager.get_record(ADMIN, gid)
        refused, cluster = record.tool_calls[-2:]
        assert refused["result"] == {
            "failure": "POPULATION_ID_MISSING",
            "counts": {"rows_without_id": 2},
        }
        # the fixed functions on a history count rows
        assert cluster["result"]["unit"] == "rows"
        assert cluster["result"]["population"] == 82

    def test_what_the_code_printed_never_reaches_the_record(self, env: _Env) -> None:
        env = _detailed_env(env)
        manager = env.manager()
        gid = _start_detailed(manager, env.ready_file())
        env.agents.analysis_fn = _code_analysis(failures=2)

        manager.run(gid)

        record = manager.get_record(ADMIN, gid)
        # the steps' results (the accepted code itself is kept as code)
        shown = json.dumps(
            [record.tool_calls, record.table_summaries], ensure_ascii=False
        )
        for secret in ("print-secret", "77777", "boom-secret"):
            assert secret not in shown
        assert '"failure": "RUN_CODE_FAILED"' in shown
