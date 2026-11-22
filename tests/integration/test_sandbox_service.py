"""SandboxService (contract C10): Code Interpreter version and in-process version.

The Code Interpreter version is checked with ``botocore.stub.Stubber`` on a
real ``bedrock-agentcore`` client, so the request shapes are validated against
the service model without touching AWS (NFR9.3). The in-process version runs
``sandbox/persona_analysis.py`` in this process and must behave the same.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from src.models.errors import ErrorCode
from src.services.sandbox_service import (
    CodeInterpreterSandbox,
    InProcessSandbox,
    SandboxCapacityError,
    SandboxSession,
    SandboxUnavailableError,
    limit_output,
)

_LIBRARY = (
    Path(__file__).resolve().parent.parent.parent / "sandbox" / "persona_analysis.py"
)
_CI_ID = "persona_ci-abc123"
_SESSION_ID = "sess-1"
_URL = (
    "https://bucket.s3.ap-northeast-1.amazonaws.com/sources/u/f1"
    "?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIAEXAMPLE"
)
_CSV = "id,age,job\n1,30,学生\n2,45,会社員\n3,70,無職\n4,33,学生\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Code Interpreter version (Stubber)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Any:
    return boto3.client(
        "bedrock-agentcore",
        region_name="ap-northeast-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture()
def stub(client: Any) -> Any:
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


@pytest.fixture()
def sleeps() -> list[float]:
    return []


@pytest.fixture()
def sandbox(client: Any, sleeps: list[float]) -> CodeInterpreterSandbox:
    return CodeInterpreterSandbox(
        code_interpreter_id=_CI_ID,
        region_name="ap-northeast-1",
        library_path=str(_LIBRARY),
        client=client,
        max_output_chars=2_000,
        sleep=sleeps.append,
    )


def _session() -> SandboxSession:
    return SandboxSession(session_id=_SESSION_ID, generation_id="gen1")


def _invoke_params(name: str, arguments: Any = ANY) -> dict[str, Any]:
    return {
        "codeInterpreterIdentifier": _CI_ID,
        "sessionId": _SESSION_ID,
        "name": name,
        "arguments": arguments,
    }


def _stream(structured: dict[str, Any] | None = None) -> dict[str, Any]:
    # The stubbed stream is one event; the real one is an EventStream of them.
    return {
        "sessionId": _SESSION_ID,
        "stream": {"result": {"content": [], "structuredContent": structured or {}}},
    }


def _stdout(result: dict[str, Any], stderr: str = "") -> dict[str, Any]:
    return _stream(
        {
            "stdout": "warming up\n" + json.dumps(result) + "\n",
            "stderr": stderr,
            "exitCode": 0,
        }
    )


def _decoded_args(command: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(command.rsplit(" ", 1)[-1]))


class _CommandCapture:
    """An ``expected_params`` value that keeps the command it was compared to."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict) and "command" in other:
            self.commands.append(str(other["command"]))
            return True
        return False


class TestCodeInterpreterSession:
    def test_client_does_not_retry_by_itself(self) -> None:
        sandbox = CodeInterpreterSandbox(
            code_interpreter_id=_CI_ID,
            region_name="ap-northeast-1",
            library_path=str(_LIBRARY),
        )

        assert sandbox.client.meta.config.retries["total_max_attempts"] == 1

    def test_start_session_sets_the_lifetime_and_writes_the_library(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        stub.add_response(
            "start_code_interpreter_session",
            {
                "codeInterpreterIdentifier": _CI_ID,
                "sessionId": _SESSION_ID,
                "createdAt": "2026-11-19T00:00:00Z",
            },
            {
                "codeInterpreterIdentifier": _CI_ID,
                "name": "gen-gen1",
                "sessionTimeoutSeconds": 3900,
            },
        )
        stub.add_response(
            "invoke_code_interpreter",
            _stream(),
            _invoke_params(
                "writeFiles",
                {
                    "content": [
                        {
                            "path": "persona_analysis.py",
                            "text": _LIBRARY.read_text(encoding="utf-8"),
                        }
                    ]
                },
            ),
        )

        session = sandbox.start_session("gen1", lifetime_seconds=3900)

        assert session == SandboxSession(session_id=_SESSION_ID, generation_id="gen1")
        assert (
            sandbox.library_sha256 == hashlib.sha256(_LIBRARY.read_bytes()).hexdigest()
        )

    def test_stop_session(self, sandbox: CodeInterpreterSandbox, stub: Any) -> None:
        stub.add_response(
            "stop_code_interpreter_session",
            {
                "codeInterpreterIdentifier": _CI_ID,
                "sessionId": _SESSION_ID,
                "lastUpdatedAt": "2026-11-19T00:00:00Z",
            },
            {"codeInterpreterIdentifier": _CI_ID, "sessionId": _SESSION_ID},
        )

        sandbox.stop_session(_session())


class TestCodeInterpreterFiles:
    def test_put_files_downloads_with_the_url_and_never_logs_it(
        self,
        sandbox: CodeInterpreterSandbox,
        stub: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        capture = _CommandCapture()
        stub.add_response(
            "invoke_code_interpreter",
            _stdout(
                {"ok": True, "value": {"ok": True, "bytes": 42, "http_status": 200}},
                stderr=f"curl: something about {_URL}",
            ),
            _invoke_params("executeCommand", capture),
        )

        results = sandbox.put_files(
            _session(), [{"source_file_id": "f1", "s3_key": "x", "download_url": _URL}]
        )

        assert results == [
            {"source_file_id": "f1", "ok": True, "bytes": 42, "http_status": 200}
        ]
        assert _decoded_args(capture.commands[0]) == {
            "source_id": "f1",
            "url": _URL,
            "timeout_seconds": 600,
        }
        assert " download " in capture.commands[0]
        assert "X-Amz" not in caplog.text
        assert "X-Amz" not in json.dumps(results)

    def test_put_files_reports_a_refused_url(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        stub.add_response(
            "invoke_code_interpreter",
            _stdout(
                {"ok": True, "value": {"ok": False, "bytes": 0, "http_status": 403}}
            ),
            _invoke_params("executeCommand"),
        )

        results = sandbox.put_files(
            _session(), [{"source_file_id": "f1", "download_url": _URL}]
        )

        assert results[0]["ok"] is False
        assert results[0]["http_status"] == 403

    def test_put_files_needs_a_download_url(
        self, sandbox: CodeInterpreterSandbox
    ) -> None:
        with pytest.raises(ValueError):
            sandbox.put_files(_session(), [{"source_file_id": "f1", "s3_key": "k"}])


class TestCodeInterpreterCall:
    def test_call_returns_the_last_json_line(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        capture = _CommandCapture()
        stub.add_response(
            "invoke_code_interpreter",
            _stdout({"ok": True, "value": {"rows": 4, "columns": []}}),
            _invoke_params("executeCommand", capture),
        )

        result = sandbox.call(_session(), "profile_dataset", {"source_id": "f1"})

        assert result == {"ok": True, "value": {"rows": 4, "columns": []}}
        assert " profile_dataset " in capture.commands[0]
        assert _decoded_args(capture.commands[0]) == {"source_id": "f1"}

    def test_call_cuts_a_long_output_and_says_so(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        rows = [[i, "x" * 40] for i in range(500)]
        stub.add_response(
            "invoke_code_interpreter",
            _stdout({"ok": True, "value": {"columns": ["id", "v"], "rows": rows}}),
            _invoke_params("executeCommand"),
        )

        result = sandbox.call(_session(), "run_query", {"source_id": "f1"})

        assert result["ok"] is True
        assert result["truncated"] is True
        assert 0 < len(result["value"]["rows"]) < 500
        assert len(json.dumps(result, ensure_ascii=False)) <= 2_000

    def test_a_stray_unicode_line_separator_does_not_break_the_result(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        """dev の Code Interpreter が長い出力に U+0085 を混ぜた（splitlines が行を割る）。"""
        stub.add_response(
            "invoke_code_interpreter",
            _stream(
                {
                    "stdout": 'warming up\r\n{"ok": true, "value": {"v": "a\x85b"}}\r\n',
                    "exitCode": 0,
                }
            ),
            _invoke_params("executeCommand"),
        )

        result = sandbox.call(_session(), "profile_dataset", {"source_id": "f1"})

        assert result == {"ok": True, "value": {"v": "a\x85b"}}

    def test_output_that_is_not_json_is_a_failure(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"stdout": "Traceback ...", "exitCode": 1}),
            _invoke_params("executeCommand"),
        )

        result = sandbox.call(_session(), "profile_dataset", {"source_id": "f1"})

        assert result == {"ok": False, "error": {"code": "BAD_OUTPUT"}}


class TestCodeInterpreterCommand:
    def test_start_and_poll_until_done(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        capture = _CommandCapture()
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"taskId": "task-1", "taskStatus": "submitted"}),
            _invoke_params("startCommandExecution", capture),
        )
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"taskId": "task-1", "taskStatus": "submitted"}),
            _invoke_params("getTask", {"taskId": "task-1"}),
        )
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"stdout": json.dumps({"phase": "transcode", "rows": 120})}),
            _invoke_params("executeCommand"),
        )
        report = {"ok": True, "value": {"rows_read": 300}}
        stub.add_response(
            "invoke_code_interpreter",
            _stream(
                {
                    "taskId": "task-1",
                    "taskStatus": "completed",
                    "stdout": json.dumps(report) + "\n",
                    "exitCode": 0,
                }
            ),
            _invoke_params("getTask", {"taskId": "task-1"}),
        )
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"stdout": json.dumps({"phase": "finished", "rows": 300})}),
            _invoke_params("executeCommand"),
        )

        command_id = sandbox.start_command(
            _session(), "prepare", {"source_id": "f1", "metadata": {}}
        )
        running = sandbox.poll_command(_session(), command_id)
        finished = sandbox.poll_command(_session(), command_id)

        assert _decoded_args(capture.commands[0])["progress_id"] == command_id
        assert running == {
            "done": False,
            "status": "submitted",
            "progress": {"phase": "transcode", "rows": 120},
            "result": None,
        }
        assert finished == {
            "done": True,
            "status": "completed",
            "progress": {"phase": "finished", "rows": 300},
            "result": report,
        }

    def test_cancel_command_stops_the_task(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"taskId": "task-9", "taskStatus": "submitted"}),
            _invoke_params("startCommandExecution"),
        )
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"taskId": "task-9", "taskStatus": "canceled"}),
            _invoke_params("stopTask", {"taskId": "task-9"}),
        )

        command_id = sandbox.start_command(_session(), "prepare", {"source_id": "f1"})
        sandbox.cancel_command(_session(), command_id)

    def test_unknown_command(self, sandbox: CodeInterpreterSandbox) -> None:
        with pytest.raises(SandboxUnavailableError):
            sandbox.poll_command(_session(), "nope")


class TestPrepareBoundArgument:
    """BR16.1: the bad-row limit travels as an argument of ``prepare``."""

    def test_code_interpreter_command_carries_the_limit(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        capture = _CommandCapture()
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"taskId": "task-1", "taskStatus": "submitted"}),
            _invoke_params("startCommandExecution", capture),
        )

        sandbox.start_command(
            _session(), "prepare", {"source_id": "f1", "max_excluded_percent": 5}
        )

        assert _decoded_args(capture.commands[0])["max_excluded_percent"] == 5


class TestDocumentArguments:
    """U4 NFR5.39: document functions get integers, strings and string lists."""

    def test_prepare_document_carries_the_chunk_sizes(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        capture = _CommandCapture()
        stub.add_response(
            "invoke_code_interpreter",
            _stream({"taskId": "task-1", "taskStatus": "submitted"}),
            _invoke_params("startCommandExecution", capture),
        )

        sandbox.start_command(
            _session(),
            "prepare_document",
            {
                "source_id": "f1",
                "source_index": 2,
                "format": "pdf",
                "chunk_chars": 15_000,
                "overlap_chars": 500,
            },
        )

        args = _decoded_args(capture.commands[0])
        assert args["source_index"] == 2
        assert args["chunk_chars"] == 15_000 and args["overlap_chars"] == 500
        assert "progress_id" in args

    def test_verify_quotes_carries_japanese_quotes_whole(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        capture = _CommandCapture()
        stub.add_response(
            "invoke_code_interpreter",
            _stdout({"ok": True, "value": {"found": [True, False]}}),
            _invoke_params("executeCommand", capture),
        )
        quotes = ["受付で30分も待たされた", 'a "quoted"\nline']

        result = sandbox.call(
            _session(),
            "verify_quotes",
            {"source_id": "f1", "quotes": quotes, "min_chars": 10},
        )

        args = _decoded_args(capture.commands[0])
        assert args["quotes"] == quotes and args["min_chars"] == 10
        assert result["value"]["found"] == [True, False]

    def test_a_cut_chunk_is_marked_for_the_caller(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        stub.add_response(
            "invoke_code_interpreter",
            _stdout({"ok": True, "value": {"text": "あ" * 5_000, "next_offset": None}}),
            _invoke_params("executeCommand"),
        )

        result = sandbox.call(
            _session(), "document_chunk", {"source_id": "f1", "index": 0, "offset": 0}
        )

        assert result["value"]["truncated"] is True


class TestLimitOutput:
    """BR17.3, NFR5.36: an output over the limit is cut and goes on."""

    def test_a_long_string_alone_is_cut_and_marked(self) -> None:
        result = {"ok": True, "value": {"summary": "あ" * 5_000, "n": 3}}

        cut = limit_output(result, 1_000)

        assert cut["ok"] is True
        assert cut["truncated"] is True
        assert cut["value"]["truncated"] is True
        assert cut["value"]["n"] == 3
        assert 0 < len(cut["value"]["summary"]) < 1_000
        assert len(json.dumps(cut, ensure_ascii=False)) <= 1_000

    def test_the_longest_string_is_the_one_cut(self) -> None:
        result = {"ok": True, "value": {"short": "keep me", "long": "x" * 3_000}}

        cut = limit_output(result, 500)

        assert cut["value"]["short"] == "keep me"
        assert len(cut["value"]["long"]) < 500

    def test_escaped_characters_still_fit(self) -> None:
        result = {"ok": True, "value": {"text": '"\n' * 3_000}}

        cut = limit_output(result, 800)

        assert len(json.dumps(cut, ensure_ascii=False)) <= 800
        assert cut["truncated"] is True

    def test_numbers_only_over_the_limit_is_too_large(self) -> None:
        result = {"ok": True, "value": {f"k{i}": i for i in range(2_000)}}

        cut = limit_output(result, 500)

        assert cut["ok"] is False
        assert cut["error"]["code"] == "OUTPUT_TOO_LARGE"

    def test_a_list_is_halved_as_before(self) -> None:
        result = {"ok": True, "value": {"rows": [[i, "y" * 20] for i in range(500)]}}

        cut = limit_output(result, 2_000)

        assert cut["truncated"] is True
        assert 0 < len(cut["value"]["rows"]) < 500
        assert len(json.dumps(cut, ensure_ascii=False)) <= 2_000

    def test_a_result_that_fits_is_returned_as_is(self) -> None:
        result = {"ok": True, "value": {"a": 1}}

        assert limit_output(result, 1_000) is result


class TestCodeInterpreterRetries:
    def test_two_throttles_then_success(
        self, sandbox: CodeInterpreterSandbox, stub: Any, sleeps: list[float]
    ) -> None:
        for _ in range(2):
            stub.add_client_error(
                "invoke_code_interpreter",
                service_error_code="ThrottlingException",
                http_status_code=429,
            )
        stub.add_response(
            "invoke_code_interpreter",
            _stdout({"ok": True, "value": {"rows": 1, "columns": []}}),
            _invoke_params("executeCommand"),
        )

        result = sandbox.call(_session(), "profile_dataset", {"source_id": "f1"})

        assert result["ok"] is True
        assert sleeps == [1, 2]

    def test_four_failures_give_up_with_the_cause(
        self, sandbox: CodeInterpreterSandbox, stub: Any, sleeps: list[float]
    ) -> None:
        for _ in range(4):
            stub.add_client_error(
                "invoke_code_interpreter",
                service_error_code="InternalServerException",
                http_status_code=500,
            )

        with pytest.raises(SandboxUnavailableError) as raised:
            sandbox.call(_session(), "profile_dataset", {"source_id": "f1"})

        assert raised.value.code == ErrorCode.GENERATION_SANDBOX_UNAVAILABLE
        assert raised.value.context == {"cause": "code_interpreter"}
        assert sleeps == [1, 2, 4]

    def test_a_throttle_inside_the_stream_is_retried(
        self, sandbox: CodeInterpreterSandbox, stub: Any, sleeps: list[float]
    ) -> None:
        stub.add_response(
            "invoke_code_interpreter",
            {
                "sessionId": _SESSION_ID,
                "stream": {"throttlingException": {"message": "slow down"}},
            },
            _invoke_params("executeCommand"),
        )
        stub.add_response(
            "invoke_code_interpreter",
            _stdout({"ok": True, "value": {"rows": 1, "columns": []}}),
            _invoke_params("executeCommand"),
        )

        assert sandbox.call(_session(), "profile_dataset", {"source_id": "f1"})["ok"]
        assert sleeps == [1]

    def test_quota_exhaustion_is_a_capacity_error(
        self, sandbox: CodeInterpreterSandbox, stub: Any
    ) -> None:
        for _ in range(4):
            stub.add_client_error(
                "start_code_interpreter_session",
                service_error_code="ServiceQuotaExceededException",
                http_status_code=402,
            )

        with pytest.raises(SandboxCapacityError) as raised:
            sandbox.start_session("gen1", lifetime_seconds=3900)

        assert raised.value.context == {"cause": "code_interpreter"}

    def test_a_validation_error_is_not_retried_and_hides_the_command(
        self,
        sandbox: CodeInterpreterSandbox,
        stub: Any,
        sleeps: list[float],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        stub.add_client_error(
            "invoke_code_interpreter",
            service_error_code="ValidationException",
            service_message=f"bad command {_URL}",
            http_status_code=400,
        )

        with pytest.raises(SandboxUnavailableError) as raised:
            sandbox.put_files(
                _session(), [{"source_file_id": "f1", "download_url": _URL}]
            )

        assert sleeps == []
        assert raised.value.__cause__ is None
        assert "X-Amz" not in str(raised.value)
        assert "X-Amz" not in caplog.text


# ---------------------------------------------------------------------------
# In-process version (same contract)
# ---------------------------------------------------------------------------


@pytest.fixture()
def local(tmp_path: Path) -> InProcessSandbox:
    objects = {"sources/u/f1": _CSV}
    return InProcessSandbox(
        library_path=str(_LIBRARY),
        root_dir=str(tmp_path),
        read_object=objects.__getitem__,
        max_output_chars=2_000,
    )


def _poll_until_done(
    sandbox: InProcessSandbox, session: SandboxSession, command_id: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = sandbox.poll_command(session, command_id)
        if state["done"]:
            return state
        time.sleep(0.05)
    raise AssertionError("command did not finish")


class TestInProcessSandbox:
    def test_prepare_then_profile(self, local: InProcessSandbox) -> None:
        session = local.start_session("gen1", lifetime_seconds=3900)
        put = local.put_files(
            session, [{"source_file_id": "f1", "s3_key": "sources/u/f1"}]
        )
        command_id = local.start_command(
            session,
            "prepare",
            {"source_id": "f1", "metadata": {"encoding": "utf-8", "delimiter": ","}},
        )
        state = _poll_until_done(local, session, command_id)
        profile = local.call(session, "profile_dataset", {"source_id": "f1"})

        assert put == [
            {
                "source_file_id": "f1",
                "ok": True,
                "bytes": len(_CSV),
                "http_status": None,
            }
        ]
        assert state["status"] == "completed"
        assert state["result"]["value"]["rows_read"] == 4
        assert state["progress"]["phase"] == "finished"
        assert profile["value"]["rows"] == 4

    def test_prepare_takes_the_bad_row_limit(self, tmp_path: Path) -> None:
        rows = "".join(f"{i},30\n" for i in range(3, 103))
        data = ("id,age\n1,30\n2,40,extra\n" + rows).encode()
        sandbox = InProcessSandbox(
            library_path=str(_LIBRARY),
            root_dir=str(tmp_path),
            read_object={"k": data}.__getitem__,
            max_output_chars=20_000,
        )
        session = sandbox.start_session("gen1", lifetime_seconds=60)
        sandbox.put_files(session, [{"source_file_id": "f1", "s3_key": "k"}])
        args = {"source_id": "f1", "metadata": {}}

        strict = sandbox.call(session, "prepare", {**args, "max_excluded_percent": 0})
        relaxed = sandbox.call(session, "prepare", {**args, "max_excluded_percent": 2})

        assert strict["error"]["code"] == "TOO_MANY_BAD_ROWS"
        assert relaxed["ok"] is True

    def test_long_output_is_cut(self, local: InProcessSandbox) -> None:
        many = "id,v\n" + "".join(f"{i},{'x' * 40}\n" for i in range(500))
        sandbox = InProcessSandbox(
            library_path=str(_LIBRARY),
            root_dir=local.root_dir,
            read_object={"k": many.encode()}.__getitem__,
            max_output_chars=2_000,
        )
        session = sandbox.start_session("gen1", lifetime_seconds=60)
        sandbox.put_files(session, [{"source_file_id": "f1", "s3_key": "k"}])
        sandbox.call(session, "prepare", {"source_id": "f1", "metadata": {}})

        result = sandbox.call(
            session,
            "run_query",
            {"source_id": "f1", "sql": "SELECT * FROM dataset", "max_rows": 500},
        )

        assert result["truncated"] is True
        assert len(json.dumps(result, ensure_ascii=False)) <= 2_000

    def test_stop_session_removes_the_workdir(self, local: InProcessSandbox) -> None:
        session = local.start_session("gen1", lifetime_seconds=60)
        workdir = Path(local.workdir_of(session))
        assert workdir.is_dir()

        local.stop_session(session)

        assert not workdir.exists()
        with pytest.raises(SandboxUnavailableError):
            local.call(session, "profile_dataset", {"source_id": "f1"})

    def test_a_missing_object_is_reported_as_not_ok(
        self, local: InProcessSandbox
    ) -> None:
        session = local.start_session("gen1", lifetime_seconds=60)

        put = local.put_files(session, [{"source_file_id": "f2", "s3_key": "nope"}])

        assert put[0]["ok"] is False

    def test_a_document_is_prepared_then_read_and_its_quotes_checked(
        self, tmp_path: Path
    ) -> None:
        # U4: the same contract runs prepare_document, document_chunk, verify_quotes
        text = "# 第1章\n" + "受付で30分も待たされたのが不満です。\n" * 50
        sandbox = InProcessSandbox(
            library_path=str(_LIBRARY),
            root_dir=str(tmp_path),
            read_object={"k": text.encode("utf-8")}.__getitem__,
            max_output_chars=20_000,
        )
        session = sandbox.start_session("gen1", lifetime_seconds=60)
        sandbox.put_files(session, [{"source_file_id": "f1", "s3_key": "k"}])
        command_id = sandbox.start_command(
            session,
            "prepare_document",
            {
                "source_id": "f1",
                "source_index": 0,
                "format": "md",
                "encoding": "utf-8",
                "chunk_chars": 15_000,
                "overlap_chars": 500,
            },
        )

        state = _poll_until_done(sandbox, session, command_id)
        chunk = sandbox.call(
            session,
            "document_chunk",
            {"source_id": "f1", "index": 0, "offset": 0, "max_chars": 10_000},
        )
        checked = sandbox.call(
            session,
            "verify_quotes",
            {
                "source_id": "f1",
                "quotes": ["受付で30分も待たされた", "どこにも書いていない言葉です"],
                "min_chars": 10,
            },
        )

        assert state["result"]["value"]["chunks"] == 1
        assert chunk["value"]["text"].startswith("# 第1章")
        assert checked["value"]["found"] == [True, False]
