"""SandboxService: the analysis sandbox of advanced generation (contract C10).

One session per generation (BR6.1). The fixed library
``sandbox/persona_analysis.py`` is written into the session and its functions
are called by name with JSON arguments (contract C9). Two versions keep the
same contract:

- :class:`CodeInterpreterSandbox` calls AgentCore Code Interpreter through the
  boto3 ``bedrock-agentcore`` client directly (tech-stack-decisions.md).
  Transient failures are retried here, 3 times after 1, 2 and 4 seconds, and
  botocore's own retry is off so they do not stack (NFR5.16).
- :class:`InProcessSandbox` runs the same library in this process (tests, and
  U7 for environments without Code Interpreter).

This Service never imports ``S3Service`` (no Service-to-Service imports): the
Manager makes the short presigned GET URL and passes it as ``download_url``
(BR6.2). The URL goes into the session's command only; it is never logged,
returned or put in an exception (NFR3.6).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

from ..models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)

#: ``cause`` put in the error context when Code Interpreter fails (NFR5.16).
CAUSE_CODE_INTERPRETER = "code_interpreter"
#: Waits before each retry of a transient failure (reliability-design.md §3).
RETRY_DELAYS_SECONDS: tuple[float, ...] = (1, 2, 4)
#: Library file name inside the session, and the work folder (on disk).
_LIBRARY_NAME = "persona_analysis.py"
_WORKDIR = "work"
#: Time limit of one download inside the session (security-design.md §3).
_DOWNLOAD_TIMEOUT_SECONDS = 600
_DONE_STATUSES = frozenset({"completed", "failed", "canceled", "cancelled"})
_RETRYABLE_CODES = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "ServiceQuotaExceededException",
        "InternalServerException",
        "ServiceException",
        "RetryableConflictException",
    }
)
_QUOTA_CODES = frozenset({"ServiceQuotaExceededException"})
# Exception members of the InvokeCodeInterpreter event stream.
_STREAM_ERRORS = {
    "throttlingException": "ThrottlingException",
    "serviceQuotaExceededException": "ServiceQuotaExceededException",
    "internalServerException": "InternalServerException",
    "accessDeniedException": "AccessDeniedException",
    "conflictException": "ConflictException",
    "resourceNotFoundException": "ResourceNotFoundException",
    "validationException": "ValidationException",
}


class SandboxServiceError(CodedError):
    """The sandbox failed. ``context`` is ``{"cause": "code_interpreter"}``."""

    code = ErrorCode.GENERATION_SANDBOX_UNAVAILABLE


class SandboxUnavailableError(SandboxServiceError):
    """Code Interpreter could not be reached or refused the request."""


class SandboxCapacityError(SandboxServiceError):
    """The session quota stayed exhausted after the retries."""


@dataclass(frozen=True)
class SandboxSession:
    """Handle of one generation's session (its ID may be logged)."""

    session_id: str
    generation_id: str


class SandboxService(Protocol):
    """Contract C10. Both versions below satisfy it."""

    @property
    def library_sha256(self) -> str:
        """SHA-256 of the library run in the session (AnalysisRun.code_sha256)."""
        ...

    def start_session(
        self, generation_id: str, lifetime_seconds: int
    ) -> SandboxSession: ...

    def put_files(
        self, session: SandboxSession, files: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]: ...

    def call(
        self, session: SandboxSession, function: str, args: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def start_command(
        self, session: SandboxSession, function: str, args: Mapping[str, Any]
    ) -> str: ...

    def poll_command(
        self, session: SandboxSession, command_id: str
    ) -> dict[str, Any]: ...

    def cancel_command(self, session: SandboxSession, command_id: str) -> None: ...

    def stop_session(self, session: SandboxSession) -> None: ...

    def stop_session_by_id(self, session_id: str) -> None:
        """Stop a session known only by its ID (U5: a reader ends a generation)."""
        ...


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _error(cls: type[SandboxServiceError], message: str) -> SandboxServiceError:
    return cls(message, context={"cause": CAUSE_CODE_INTERPRETER})


def _failure(code: str, **context: Any) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code}
    if context:
        error["context"] = context
    return {"ok": False, "error": error}


def _size(result: Mapping[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False, default=str))


def limit_output(result: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Cut a C9 result to ``max_chars`` characters and say so (NFR5.10, NFR5.36).

    The longest list in the value (rows, columns, clusters) is halved until the
    result fits. When the value has no list left to halve, its longest string
    is cut instead (an output made of one long text goes on, cut). ``truncated``
    is set on the result and on the value. A result that cannot be cut (numbers
    alone over the limit) becomes ``OUTPUT_TOO_LARGE``.
    """
    if _size(result) <= max_chars:
        return result
    value = result.get("value")
    if result.get("ok") and isinstance(value, dict):
        cut = dict(value)
        lists = [k for k, v in cut.items() if isinstance(v, list) and v]
        while lists:
            longest = max(lists, key=lambda k: _size({"v": cut[k]}))
            cut[longest] = cut[longest][: len(cut[longest]) // 2]
            cut["truncated"] = True
            candidate = {"ok": True, "value": cut, "truncated": True}
            if _size(candidate) <= max_chars:
                return candidate
            lists = [k for k, v in cut.items() if isinstance(v, list) and v]
        shortened = _cut_strings(cut, max_chars)
        if shortened is not None:
            return shortened
    return _failure("OUTPUT_TOO_LARGE", limit=max_chars)


def _cut_strings(cut: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    """Shorten the longest string of ``cut`` until the result fits, or ``None``."""
    cut = dict(cut)
    cut["truncated"] = True
    step = 1
    while True:
        candidate = {"ok": True, "value": cut, "truncated": True}
        overflow = _size(candidate) - max_chars
        if overflow <= 0:
            return candidate
        strings = [k for k, v in cut.items() if isinstance(v, str) and v]
        if not strings:
            return None
        longest = max(strings, key=lambda k: len(cut[k]))
        # Escapes (quotes, newlines) make the text longer on the wire than in
        # memory, so a cut by the overflow may fall short: cut harder each round.
        cut[longest] = cut[longest][: max(len(cut[longest]) - overflow * step, 0)]
        step *= 2


def _parse_result(stdout: str) -> dict[str, Any]:
    """The last JSON line the library printed; anything else is dropped."""
    # split("\n"), not splitlines(): a stray U+0085 is not a line break here.
    for line in reversed(stdout.strip().split("\n")):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            return _failure("BAD_OUTPUT")
        if isinstance(parsed, dict) and isinstance(parsed.get("ok"), bool):
            return parsed
        return _failure("BAD_OUTPUT")
    return _failure("BAD_OUTPUT")


def _encode_args(args: Mapping[str, Any]) -> str:
    return base64.b64encode(
        json.dumps(dict(args), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def _library_command(function: str, args: Mapping[str, Any]) -> str:
    # base64 keeps the arguments free of shell quoting. They may hold the
    # presigned URL (download), so the command is never logged.
    return (
        f"python {_LIBRARY_NAME} --workdir {_WORKDIR} {function} {_encode_args(args)}"
    )


def _sha256_of(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------------------
# Code Interpreter version
# ---------------------------------------------------------------------------


class _TransientStreamError(Exception):
    """An exception event inside the response stream."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _error_code_of(error: Exception) -> str:
    from botocore.exceptions import (
        ClientError,
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ReadTimeoutError,
    )

    if isinstance(error, _TransientStreamError):
        return error.error_code
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        status = int(
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        )
        if status >= 500 and code not in _RETRYABLE_CODES:
            return "InternalServerException"
        return code
    if isinstance(
        error,
        (
            EndpointConnectionError,
            ConnectionClosedError,
            ConnectTimeoutError,
            ReadTimeoutError,
        ),
    ):
        return "ConnectionError"
    return type(error).__name__


class CodeInterpreterSandbox:
    """SandboxService on AgentCore Code Interpreter (SANDBOX network mode)."""

    def __init__(
        self,
        *,
        code_interpreter_id: str,
        region_name: str,
        library_path: str,
        client: Any = None,
        max_output_chars: int = 20_000,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.code_interpreter_id = code_interpreter_id
        self.library_path = library_path
        self.max_output_chars = max_output_chars
        self.client = client or self._create_client(region_name)
        self._sleep = sleep
        self._commands: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._library_sha256 = _sha256_of(library_path)

    @staticmethod
    def _create_client(region_name: str) -> Any:
        import boto3
        from botocore.config import Config as BotoConfig

        # botocore's own retry is off; this class retries (NFR5.16).
        return boto3.client(
            "bedrock-agentcore",
            region_name=region_name,
            config=BotoConfig(
                connect_timeout=10,
                read_timeout=300,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )

    @property
    def library_sha256(self) -> str:
        return self._library_sha256

    # --- retries -----------------------------------------------------------

    def _retrying(self, operation: str, fn: Callable[[], Any]) -> Any:
        attempts = len(RETRY_DELAYS_SECONDS) + 1
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 - classified below
                code = _error_code_of(e)
            retryable = code in _RETRYABLE_CODES or code == "ConnectionError"
            logger.warning(
                "code interpreter %s failed: error_code=%s attempt=%d",
                operation,
                code,
                attempt + 1,
            )
            if not retryable:
                # The message of the original error may echo the command (and
                # so the URL); it is dropped, not chained.
                raise _error(
                    SandboxUnavailableError, f"{operation} failed ({code})"
                ) from None
            if attempt < attempts - 1:
                self._sleep(RETRY_DELAYS_SECONDS[attempt])
        cls = SandboxCapacityError if code in _QUOTA_CODES else SandboxUnavailableError
        raise _error(cls, f"{operation} failed after {attempts} attempts ({code})")

    # --- one InvokeCodeInterpreter call --------------------------------------

    def _invoke(
        self, session: SandboxSession, name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Run one tool and merge the structured content of the stream."""

        def once() -> dict[str, Any]:
            response = self.client.invoke_code_interpreter(
                codeInterpreterIdentifier=self.code_interpreter_id,
                sessionId=session.session_id,
                name=name,
                arguments=dict(arguments),
            )
            return self._read_stream(response.get("stream"))

        return dict(self._retrying(name, once))

    @staticmethod
    def _events(stream: Any) -> Iterable[Mapping[str, Any]]:
        # A stubbed stream is one event (a mapping); the real one iterates.
        if stream is None:
            return []
        if isinstance(stream, Mapping):
            return [stream]
        events: Iterable[Mapping[str, Any]] = stream
        return events

    def _read_stream(self, stream: Any) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        stdout: list[str] = []
        for event in self._events(stream):
            for key, error_code in _STREAM_ERRORS.items():
                if key in event:
                    raise _TransientStreamError(error_code)
            result = event.get("result") or {}
            structured = dict(result.get("structuredContent") or {})
            if "stdout" in structured:
                stdout.append(str(structured.pop("stdout")))
            merged.update(structured)
        if stdout:
            merged["stdout"] = "".join(stdout)
        return merged

    # --- C10 ---------------------------------------------------------------

    def start_session(
        self, generation_id: str, lifetime_seconds: int
    ) -> SandboxSession:
        """Open the generation's session and write the library into it."""

        def start() -> Any:
            return self.client.start_code_interpreter_session(
                codeInterpreterIdentifier=self.code_interpreter_id,
                name=f"gen-{generation_id}"[:100],
                sessionTimeoutSeconds=int(lifetime_seconds),
            )

        response = self._retrying("StartCodeInterpreterSession", start)
        session = SandboxSession(
            session_id=str(response["sessionId"]), generation_id=generation_id
        )
        logger.info(
            "code interpreter session started: session_id=%s generation_id=%s",
            session.session_id,
            generation_id,
        )
        with open(self.library_path, encoding="utf-8") as f:
            source = f.read()
        try:
            self._invoke(
                session,
                "writeFiles",
                {"content": [{"path": _LIBRARY_NAME, "text": source}]},
            )
        except SandboxServiceError:
            self._stop_quietly(session)
            raise
        return session

    def put_files(
        self, session: SandboxSession, files: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Bring each file in from its ``download_url`` (BR6.2).

        Returns ``[{source_file_id, ok, bytes, http_status}]``; a refused or
        expired URL is ``ok=False`` with its status, for the Manager to make a
        new URL once.
        """
        results: list[dict[str, Any]] = []
        for file in files:
            url = file.get("download_url")
            if not url:
                raise ValueError("the Code Interpreter sandbox needs a download_url")
            source_id = str(file["source_file_id"])
            outcome = self._run_library(
                session,
                "download",
                {
                    "source_id": source_id,
                    "url": str(url),
                    "timeout_seconds": _DOWNLOAD_TIMEOUT_SECONDS,
                },
            )
            value = outcome.get("value") if outcome.get("ok") else None
            value = value if isinstance(value, dict) else {}
            results.append(
                {
                    "source_file_id": source_id,
                    "ok": bool(value.get("ok", False)),
                    "bytes": int(value.get("bytes", 0) or 0),
                    "http_status": value.get("http_status"),
                }
            )
            logger.info(
                "sandbox file brought in: session_id=%s ok=%s http_status=%s",
                session.session_id,
                results[-1]["ok"],
                results[-1]["http_status"],
            )
        return results

    def _run_library(
        self, session: SandboxSession, function: str, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        content = self._invoke(
            session, "executeCommand", {"command": _library_command(function, args)}
        )
        # stderr is dropped: it may echo arguments (NFR3.6).
        return _parse_result(str(content.get("stdout", "")))

    def call(
        self, session: SandboxSession, function: str, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Run one library function and return the C9 result (cut to the limit)."""
        result = self._run_library(session, function, args)
        return limit_output(result, self.max_output_chars)

    def start_command(
        self, session: SandboxSession, function: str, args: Mapping[str, Any]
    ) -> str:
        """Start a long function (the preparation) without waiting for it."""
        command_id = uuid.uuid4().hex
        command = _library_command(function, {**dict(args), "progress_id": command_id})
        content = self._invoke(session, "startCommandExecution", {"command": command})
        task_id = content.get("taskId")
        if not task_id:
            raise _error(SandboxUnavailableError, "startCommandExecution gave no task")
        with self._lock:
            self._commands[(session.session_id, command_id)] = str(task_id)
        return command_id

    def _task_of(self, session: SandboxSession, command_id: str) -> str:
        with self._lock:
            task_id = self._commands.get((session.session_id, command_id))
        if task_id is None:
            raise _error(SandboxUnavailableError, "unknown command")
        return task_id

    def poll_command(self, session: SandboxSession, command_id: str) -> dict[str, Any]:
        """``{done, status, progress, result}`` of a started command (C10)."""
        task_id = self._task_of(session, command_id)
        task = self._invoke(session, "getTask", {"taskId": task_id})
        status = str(task.get("taskStatus", "")).lower()
        progress_raw = self._invoke(
            session,
            "executeCommand",
            {"command": f"cat {_WORKDIR}/progress/{command_id}.json"},
        ).get("stdout", "")
        done = status in _DONE_STATUSES
        result: Optional[dict[str, Any]] = None
        if done:
            parsed = _parse_result(str(task.get("stdout", "")))
            result = limit_output(parsed, self.max_output_chars)
            with self._lock:
                self._commands.pop((session.session_id, command_id), None)
        return {
            "done": done,
            "status": status,
            "progress": _parse_progress(str(progress_raw)),
            "result": result,
        }

    def cancel_command(self, session: SandboxSession, command_id: str) -> None:
        """Stop a started command (the preparation's 20-minute limit)."""
        task_id = self._task_of(session, command_id)
        self._invoke(session, "stopTask", {"taskId": task_id})
        with self._lock:
            self._commands.pop((session.session_id, command_id), None)

    def stop_session(self, session: SandboxSession) -> None:
        """Stop the session; raises after the retries (the caller logs it)."""
        self.stop_session_by_id(session.session_id)

    def stop_session_by_id(self, session_id: str) -> None:
        """Stop a session by its ID; raises after the retries (NFR5.48)."""

        def stop() -> Any:
            return self.client.stop_code_interpreter_session(
                codeInterpreterIdentifier=self.code_interpreter_id,
                sessionId=session_id,
            )

        self._retrying("StopCodeInterpreterSession", stop)
        with self._lock:
            for key in [k for k in self._commands if k[0] == session_id]:
                self._commands.pop(key)
        logger.info("code interpreter session stopped: session_id=%s", session_id)

    def _stop_quietly(self, session: SandboxSession) -> None:
        try:
            self.stop_session(session)
        except SandboxServiceError:
            logger.warning(
                "code interpreter session left to expire: session_id=%s",
                session.session_id,
            )


def _parse_progress(text: str) -> Optional[dict[str, Any]]:
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# In-process version
# ---------------------------------------------------------------------------


def _load_library(path: str) -> ModuleType:
    """Load the library from its file (the app never imports ``sandbox``)."""
    spec = importlib.util.spec_from_file_location("_persona_analysis_in_process", path)
    if spec is None or spec.loader is None:
        raise _error(SandboxUnavailableError, "the analysis library cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Command:
    """A library function running on its own thread (the preparation)."""

    def __init__(self, run: Callable[[], dict[str, Any]], progress_path: str) -> None:
        self.progress_path = progress_path
        self.result: Optional[dict[str, Any]] = None
        self._run = run
        self._thread = threading.Thread(target=self._work, daemon=True)

    def _work(self) -> None:
        self.result = self._run()

    def start(self) -> None:
        self._thread.start()

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()


class InProcessSandbox:
    """SandboxService that runs the library in this process (tests, U7).

    Each session gets its own folder under ``root_dir``. Files are read by
    their ``s3_key`` through ``read_object`` (or fetched from ``download_url``).
    """

    def __init__(
        self,
        *,
        library_path: str,
        root_dir: str,
        read_object: Optional[Callable[[str], bytes]] = None,
        max_output_chars: int = 20_000,
    ) -> None:
        self.root_dir = root_dir
        self.max_output_chars = max_output_chars
        self._read_object = read_object
        self._library = _load_library(library_path)
        self._library_sha256 = _sha256_of(library_path)
        self._workdirs: dict[str, str] = {}
        self._commands: dict[tuple[str, str], _Command] = {}
        self._lock = threading.Lock()

    @property
    def library_sha256(self) -> str:
        return self._library_sha256

    def workdir_of(self, session: SandboxSession) -> str:
        with self._lock:
            workdir = self._workdirs.get(session.session_id)
        if workdir is None:
            raise _error(SandboxUnavailableError, "the session is not open")
        return workdir

    def start_session(
        self, generation_id: str, lifetime_seconds: int
    ) -> SandboxSession:
        session = SandboxSession(
            session_id=f"local-{uuid.uuid4().hex}", generation_id=generation_id
        )
        workdir = os.path.join(self.root_dir, session.session_id)
        os.makedirs(workdir, exist_ok=False)
        with self._lock:
            self._workdirs[session.session_id] = workdir
        return session

    def _dispatch(
        self, session: SandboxSession, function: str, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = self._library.dispatch(function, dict(args), self.workdir_of(session))
        return dict(result)

    def put_files(
        self, session: SandboxSession, files: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        workdir = self.workdir_of(session)
        results: list[dict[str, Any]] = []
        for file in files:
            source_id = str(file["source_file_id"])
            if file.get("download_url"):
                outcome = self._dispatch(
                    session,
                    "download",
                    {"source_id": source_id, "url": str(file["download_url"])},
                )
                value = outcome.get("value") or {}
                results.append({"source_file_id": source_id, **value})
                continue
            results.append(self._copy_object(workdir, source_id, file.get("s3_key")))
        return results

    def _copy_object(self, workdir: str, source_id: str, s3_key: Any) -> dict[str, Any]:
        failed = {
            "source_file_id": source_id,
            "ok": False,
            "bytes": 0,
            "http_status": None,
        }
        if self._read_object is None or not s3_key:
            return failed
        try:
            data = self._read_object(str(s3_key))
        except Exception as e:  # noqa: BLE001 - reported as not brought in
            logger.warning(
                "in-process sandbox read failed: error_type=%s", type(e).__name__
            )
            return failed
        dest = os.path.join(workdir, "sources", source_id)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return {
            "source_file_id": source_id,
            "ok": True,
            "bytes": len(data),
            "http_status": None,
        }

    def call(
        self, session: SandboxSession, function: str, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        return limit_output(
            self._dispatch(session, function, args), self.max_output_chars
        )

    def start_command(
        self, session: SandboxSession, function: str, args: Mapping[str, Any]
    ) -> str:
        workdir = self.workdir_of(session)
        command_id = uuid.uuid4().hex
        full_args = {**dict(args), "progress_id": command_id}
        command = _Command(
            lambda: dict(self._library.dispatch(function, full_args, workdir)),
            os.path.join(workdir, "progress", f"{command_id}.json"),
        )
        with self._lock:
            self._commands[(session.session_id, command_id)] = command
        command.start()
        return command_id

    def poll_command(self, session: SandboxSession, command_id: str) -> dict[str, Any]:
        with self._lock:
            command = self._commands.get((session.session_id, command_id))
        if command is None:
            raise _error(SandboxUnavailableError, "unknown command")
        done = command.done
        progress = None
        if os.path.exists(command.progress_path):
            with open(command.progress_path, encoding="utf-8") as f:
                progress = _parse_progress(f.read())
        result = None
        if done and command.result is not None:
            result = limit_output(dict(command.result), self.max_output_chars)
        return {
            "done": done,
            "status": "completed" if done else "running",
            "progress": progress,
            "result": result,
        }

    def cancel_command(self, session: SandboxSession, command_id: str) -> None:
        # A Python thread cannot be stopped; the session folder goes at stop.
        with self._lock:
            self._commands.pop((session.session_id, command_id), None)

    def stop_session(self, session: SandboxSession) -> None:
        self.stop_session_by_id(session.session_id)

    def stop_session_by_id(self, session_id: str) -> None:
        """Remove the session's folder; an unknown ID does nothing."""
        with self._lock:
            workdir = self._workdirs.pop(session_id, None)
            for key in [k for k in self._commands if k[0] == session_id]:
                self._commands.pop(key)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
