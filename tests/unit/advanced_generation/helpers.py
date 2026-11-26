"""Shared parts of the AdvancedPersonaGenerationManager tests.

DynamoDB and S3 are moto with the real stores; the sandbox is the in-process
version running ``sandbox/persona_analysis.py`` (the session's download from
the presigned URL is replaced by a read of the same key); the agents are a
fake that calls the real tools and answers with the shapes AgentService returns.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest.mock import Mock

from src.config import config
from src.managers.advanced_generation.manager import AdvancedPersonaGenerationManager
from src.managers.components.analysis_record_archive import record_key
from src.managers.shared.source_inspector import inspect_table_head
from src.models.advanced_generation import GenerationStatus
from src.models.authz import Principal, Role
from src.models.errors import ErrorCode
from src.models.source_file import SourceFile, SourceFileStatus, SourceFormat
from src.services.agent_service import AgentServiceError
from src.services.dataset_analysis.analysis_tool_service import AnalysisToolService
from src.services.s3_service import S3Service, S3ServiceError
from src.services.sandbox_service import InProcessSandbox, SandboxSession
from src.services.service_factory import service_factory
from src.services.stores.advanced_generation_store import (
    AdvancedGenerationStore,
    AdvancedGenerationStoreError,
)
from src.services.stores.app_settings_store import AppSettingsStore
from src.services.stores.persona_candidate_store import PersonaCandidateStore
from src.services.stores.source_file_store import SourceFileStore
from tests.advanced_helpers import (
    BUCKET,
    CANDIDATES_TABLE,
    GENERATIONS_TABLE,
    REGION,
    SOURCE_FILES_TABLE,
)

LIBRARY = Path(__file__).resolve().parents[3] / "sandbox" / "persona_analysis.py"
ADMIN = Principal(sub="admin-1", roles=frozenset({Role.ADMIN}))
OTHER = Principal(sub="admin-2", roles=frozenset({Role.ADMIN}))
MEMBER = Principal(sub="member-1", roles=frozenset({Role.MEMBER}))
START = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
URL_MARK = "X-Amz-Signature"


def _csv(rows: int = 300) -> bytes:
    lines = ["id,age,income,job"]
    for i in range(rows):
        young = i % 2 == 0
        age = (24 if young else 61) + i % 5
        income = (300 if young else 900) + i % 11
        lines.append(f"{i},{age},{income},{'学生' if young else '会社員'}")
    return ("\n".join(lines) + "\n").encode("utf-8")


#: Labels the fake extraction and synthesis read evidence from.
_LABELS = {"価値": "value", "不満": "pain", "目標": "goal", "行動": "behavior"}


def _interview(lines: int = 4, *, prefix: str = "") -> str:
    """An interview record whose lines carry a value, a pain and a goal."""
    body = [
        f"{prefix}価値: 家族と過ごす時間を何よりも大切にしていると話した{i}。"
        f"\n{prefix}不満: 平日の夜は受付が混んでいて待ち時間が長すぎると感じる{i}。"
        f"\n{prefix}目標: 来年までに週末の家事を半分に減らしたいと考えている{i}。"
        for i in range(lines)
    ]
    return "# 面談の記録\n" + "\n\n".join(body) + "\n"


def _labelled_items(text: str) -> list[dict[str, Any]]:
    items = []
    for line in text.split("\n"):
        for label, kind in _LABELS.items():
            mark = f"{label}: "
            if mark in line:
                quote = line.split(mark, 1)[1].strip()
                if len(quote) >= 10:
                    items.append(
                        {
                            "kind": kind,
                            "claim": f"{label}についての発言",
                            "quote": quote,
                            "speaker_attributes": [{"name": "年代", "value": "30代"}],
                        }
                    )
    return items


def _full_text_evidence(documents, mode: str) -> list[dict[str, Any]]:
    evidence = []
    for index, text in enumerate(documents):
        for n, item in enumerate(_labelled_items(text)[:6]):
            if mode == "no_goal" and item["kind"] == "goal":
                continue
            evidence.append(
                {
                    "id": f"m{index}-{n}",
                    "document_index": index,
                    "kind": item["kind"],
                    "claim": item["claim"],
                    "quote": item["quote"],
                    "speaker_attributes": item["speaker_attributes"],
                    "segment_id": "seg-0" if n == 0 else None,
                    "assignment_reason": "30代の発言" if n == 0 else None,
                }
            )
    if mode == "fabricate":
        evidence.append(
            {
                "id": "m-fake",
                "document_index": 0,
                "kind": "pain",
                "claim": "作り話",
                "quote": "この文書のどこにも書かれていない作り話の一文です",
            }
        )
    return evidence


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


class _Sandbox(InProcessSandbox):
    """In-process sandbox; the presigned download becomes a read of the key."""

    def __init__(self, *, read_object, root_dir: str) -> None:
        super().__init__(
            library_path=str(LIBRARY), root_dir=root_dir, read_object=read_object
        )
        self.download_urls: list[str] = []
        self.refuse_downloads = 0
        self.stopped: list[str] = []
        self.stop_error: Optional[Exception] = None
        self.on_profile = None
        #: When set, the preparation never finishes (it reports these rows).
        self.hold_prepare_rows: Optional[int] = None
        #: When set, the preparation ends at once with this C9 result.
        self.fake_prepare_result: Optional[dict[str, Any]] = None
        self.cancelled: list[str] = []
        # what was started and called, and fake document preparations
        self.functions: dict[str, str] = {}
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.called: list[tuple[str, dict[str, Any]]] = []
        self.fake_document_result: Optional[dict[str, Any]] = None
        self.truncate_chunks = False
        self.stopped_by_id = []

    def start_command(self, session, function, args):
        command_id = super().start_command(session, function, args)
        self.functions[command_id] = function
        self.started.append((function, dict(args)))
        return command_id

    def poll_command(self, session, command_id):
        function = self.functions.get(command_id)
        if function == "prepare_document" and self.fake_document_result is not None:
            return {
                "done": True,
                "status": "completed",
                "progress": None,
                "result": self.fake_document_result,
            }
        if function == "prepare" and self.fake_prepare_result is not None:
            return {
                "done": True,
                "status": "completed",
                "progress": None,
                "result": self.fake_prepare_result,
            }
        if self.hold_prepare_rows is not None:
            return {
                "done": False,
                "status": "running",
                "progress": {"rows": self.hold_prepare_rows},
                "result": None,
            }
        return super().poll_command(session, command_id)

    def cancel_command(self, session, command_id):
        self.cancelled.append(command_id)
        super().cancel_command(session, command_id)

    def put_files(self, session, files):
        results = []
        for file in files:
            self.download_urls.append(str(file["download_url"]))
            if self.refuse_downloads > 0:
                self.refuse_downloads -= 1
                results.append(
                    {
                        "source_file_id": file["source_file_id"],
                        "ok": False,
                        "bytes": 0,
                        "http_status": 403,
                    }
                )
                continue
            plain = {k: v for k, v in file.items() if k != "download_url"}
            results.extend(super().put_files(session, [plain]))
        return results

    def call(self, session, function, args):
        if function == "profile_dataset" and self.on_profile is not None:
            self.on_profile()
        self.called.append((function, dict(args)))
        result = super().call(session, function, args)
        if function == "document_chunk" and self.truncate_chunks and result.get("ok"):
            result = {**result, "value": {**result["value"], "truncated": True}}
        return result

    def stop_session(self, session: SandboxSession) -> None:
        self.stopped.append(session.session_id)
        super().stop_session(session)
        if self.stop_error is not None:
            raise self.stop_error

    #: sessions a reader stopped by their ID
    stopped_by_id: list[str]

    def stop_session_by_id(self, session_id: str) -> None:
        self.stopped_by_id.append(session_id)
        super().stop_session_by_id(session_id)


class _Agents:
    """Stands for AgentService: calls the real tools, returns the C8 shapes."""

    def __init__(self, factory_holder: dict[str, Any]) -> None:
        self.holder = factory_holder
        self.plan: dict[str, Any] = {
            "interpreted_purpose": "全体の層",
            "questions": ["どんな層があるか"],
            "method": "cluster",
            "rationale": "指示が空欄",
        }
        self.analysis_mode = "cluster"
        self.synthesis_mode = "ok"
        self.fail_planning = False
        self.calls: list[str] = []
        self.data_seen: list[Mapping[str, str]] = []
        self._segments: list[dict[str, Any]] = []
        import threading

        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.extracted_count = 0
        #: The ``may_retry`` each extraction call was given
        self.may_retry: list[Any] = []
        #: The ``purpose`` each extraction call was given
        self.extraction_purposes: list[str] = []
        self.instructions_seen = []
        self.documents_seen = []
        self.assignments = {}

    def create_advanced_planning_agent(self, instructions: str) -> str:
        return "planner"

    def create_advanced_analysis_agent(self, instructions: str, tools: list) -> str:
        self.tools = {t.tool_name: t for t in tools}
        return "analyst"

    def create_advanced_synthesis_agent(self, instructions: str) -> str:
        self.instructions_seen.append(instructions)
        return "synthesizer"

    # --- documents ---------------------------------------------------------------

    instructions_seen: list[str]
    documents_seen: list[list[str]]
    #: "ok", "fabricate" (one quote not in the text), "fail_at:<n>", "none"
    extraction_mode = "ok"
    #: Model answers for the assignment step ({evidence_id: segment_id}).
    assignments: dict[str, Optional[str]]
    #: Full text synthesis: "ok", "no_goal" (no goal evidence), "fabricate"
    full_text_mode = "ok"

    def create_advanced_evidence_agent(self, instructions: str) -> str:
        return "extractor"

    def run_evidence_extraction(
        self,
        agent,
        prompt,
        chunk_text,
        output_schema,
        *,
        document_index,
        chunk_index,
        may_retry=None,
        purpose="",
    ):
        import threading

        with self.lock:
            self.may_retry.append(may_retry)
            self.extraction_purposes.append(purpose)
            self.calls.append("extraction")
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            number = self.extracted_count = self.extracted_count + 1
        try:
            time.sleep(0.02)
            if self.on_extraction is not None:
                self.on_extraction(number)
            if self.extraction_mode.startswith("fail_at:") and number == int(
                self.extraction_mode.split(":")[1]
            ):
                raise AgentServiceError(
                    "advanced evidence agent failed (ThrottlingException)",
                    code=ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                    context={"cause": "model"},
                )
            if self.extraction_mode == "none":
                return None, {"input_tokens": 0, "output_tokens": 0}
            items = _labelled_items(chunk_text)
            if self.extraction_mode == "fabricate" and chunk_index == 0:
                items.append(
                    {
                        "kind": "pain",
                        "claim": "作り話",
                        "quote": "この文書のどこにも書かれていない作り話の一文です",
                    }
                )
            assert threading.current_thread() is not None
            return (
                output_schema(items=items),
                {"input_tokens": 100, "output_tokens": 10},
            )
        finally:
            with self.lock:
                self.running -= 1

    #: the planning and analysis requests, in order
    planning_prompts: list[str] = []
    analysis_prompts: list[str] = []

    def run_advanced_planning(self, agent, prompt, data, documents=()):
        self.planning_prompts = [*self.planning_prompts, prompt]
        self.calls.append("planning")
        self.data_seen.append(data)
        self.documents_seen.append(list(documents))
        if self.fail_planning:
            raise AgentServiceError(
                "advanced planning agent failed (ThrottlingException)",
                code=ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                context={"cause": "model"},
            )
        return dict(self.plan)

    #: called inside the analysis call (the model is "running")
    on_analysis: Any = None
    #: called inside each extraction call
    on_extraction: Any = None

    #: replaces the analysis (tools, holder, source_id) -> output
    analysis_fn: Any = None

    def run_advanced_analysis(self, agent, prompt, data, *, source_id):
        self.analysis_prompts = [*self.analysis_prompts, prompt]
        self.analysis_data = data
        self.calls.append("analysis")
        if self.on_analysis is not None:
            self.on_analysis()
        if self.analysis_fn is not None:
            output = self.analysis_fn(self.tools, self.holder, source_id)
            self._segments = list(output["segments"])
            return output
        if self.analysis_mode == "zero":
            return {"segments": [], "evidence": []}
        text = self.tools["cluster_segments"](columns=["age", "income"])
        call_id = text.splitlines()[0].removeprefix("tool_call_id: ")
        output = self.holder["service"].output_of(call_id)
        segments, evidence = [], []
        for cluster in output["clusters"]:
            i = cluster["index"]
            segments.append(
                {
                    "id": f"seg-{i}",
                    "label": f"層 {i}",
                    "definition": {
                        "kind": "cluster",
                        "spec": {"tool_call_id": call_id, "cluster_index": i},
                    },
                    "size": cluster["size"],
                    "share": cluster["share"],
                    "features": [],
                    "evidence_ids": [f"ev-{i}"],
                    "overlaps": False,
                }
            )
            evidence.append(
                {
                    "id": f"ev-{i}",
                    "source_id": source_id,
                    "location": call_id,
                    "kind": "attribute",
                    "claim": "年齢と収入で分かれる",
                }
            )
        self._segments = segments
        return {"segments": segments, "evidence": evidence}

    def run_advanced_synthesis(self, agent, prompt, data, output_schema, documents=()):
        if "assignments" in output_schema.model_fields:
            self.calls.append("assignment")
            self.data_seen.append(data)
            evidence = json.loads(data["document_evidence"])
            return output_schema(
                assignments=[
                    {
                        "evidence_id": e["id"],
                        "segment_id": self.assignments.get(e["id"]),
                        "reason": "30代の発言" if self.assignments.get(e["id"]) else "",
                    }
                    for e in evidence
                ]
                + [{"evidence_id": "ev-unknown", "segment_id": "seg-0", "reason": "x"}]
            )
        self.calls.append("synthesis")
        self.data_seen.append(data)
        self.documents_seen.append(list(documents))
        document_evidence: list[dict[str, Any]] = []
        if documents:
            document_evidence = _full_text_evidence(documents, self.full_text_mode)
        given = [e for e in json.loads(data.get("evidence", "[]")) if e.get("quote")]
        doc_ids = [e["id"] for e in document_evidence] + [e["id"] for e in given]
        wanted = re.search(r"ちょうど (\d+) 人", prompt)
        segments = self._segments or [{"id": None, "evidence_ids": []}] * int(
            wanted.group(1) if wanted else 2
        )
        personas = []
        same_layer = self.synthesis_mode == "same_layer" or (
            self.synthesis_mode == "same_layer_first"
            and self.calls.count("synthesis") == 1
        )
        for i, segment in enumerate(segments):
            segment_id = segment["id"]
            if same_layer:
                segment = segments[0]
                segment_id = segment["id"]
            if self.synthesis_mode == "orphan" and i == 0:
                segment_id = "seg-missing"
            personas.append(
                {
                    "name": f"山田 {i}",
                    "age": 30 + i,
                    "occupation": "会社員",
                    "background": "背景",
                    "values": ["価値"],
                    "pain_points": ["悩み"],
                    "goals": ["目標"],
                    "segment_id": segment_id,
                    "evidence_ids": list(segment["evidence_ids"])
                    + doc_ids
                    + ["ev-made-up"],
                }
            )
        return output_schema(personas=personas, document_evidence=document_evidence)


class _Env:
    def __init__(self, dynamo: Any, s3_client: Any, tmp_path: Path) -> None:
        self.s3_client = s3_client
        self.s3 = S3Service(BUCKET, REGION)
        self.files = SourceFileStore(
            region=REGION, table_name=SOURCE_FILES_TABLE, client=dynamo
        )
        self.generations = AdvancedGenerationStore(
            region=REGION,
            table_name=GENERATIONS_TABLE,
            source_files_table_name=SOURCE_FILES_TABLE,
            client=dynamo,
        )
        self.candidates = PersonaCandidateStore(
            region=REGION, table_name=CANDIDATES_TABLE, client=dynamo
        )
        self.settings = Mock(spec=AppSettingsStore)
        self.settings.get.return_value = None
        self.clock = _Clock()
        self.sandbox = _Sandbox(
            read_object=lambda key: s3_client.get_object(Bucket=BUCKET, Key=key)[
                "Body"
            ].read(),
            root_dir=str(tmp_path),
        )
        self.holder: dict[str, Any] = {}
        self.agents = _Agents(self.holder)

    def factory(self, session, policy, **kwargs):
        if "gate" in kwargs and kwargs["gate"] is not None:
            # the same Service, polling a running run_code quickly
            sandbox = kwargs.pop("sandbox")
            service = AnalysisToolService(
                sandbox, session, policy, poll_seconds=0.02, **kwargs
            )
        else:
            service = service_factory.create_analysis_tools(session, policy, **kwargs)
        self.holder["service"] = service
        self.holder.setdefault("policies", []).append(policy)
        return service

    def manager(self, **overrides: Any) -> AdvancedPersonaGenerationManager:
        kwargs: dict[str, Any] = {
            "settings_store": self.settings,
            "generation_store": self.generations,
            "source_file_store": self.files,
            "candidate_store": self.candidates,
            "s3_service": self.s3,
            "agent_service": self.agents,
            "sandbox": self.sandbox,
            "tool_factory": self.factory,
            "clock": self.clock,
            "sleep": lambda seconds: time.sleep(0.01),
            "max_concurrent": 3,
            "heartbeat_seconds": 3600,
            "poll_seconds": 0,
        }
        kwargs.update(overrides)
        return AdvancedPersonaGenerationManager(**kwargs)

    def ready_file(
        self, owner: str = ADMIN.sub, data: Optional[bytes] = None
    ) -> SourceFile:
        data = _csv() if data is None else data
        draft = SourceFile.create_new(
            owner_sub=owner,
            filename="people.csv",
            size_bytes=len(data),
            part_count=1,
            now=START - timedelta(hours=1),
        )
        self.s3_client.put_object(Bucket=BUCKET, Key=draft.s3_key, Body=data)
        metadata = inspect_table_head(
            data, source_format=SourceFormat.CSV, total_size=len(data), now=START
        )
        ready = draft.update(
            status=SourceFileStatus.READY, metadata=metadata, checksum="crc=="
        )
        self.files.put_new(ready)
        return ready

    def ready_document(
        self,
        data: bytes | str,
        *,
        filename: str = "面談.md",
        owner: str = ADMIN.sub,
        inspect: bool = True,
    ) -> SourceFile:
        """A ready document inspected like an upload (or not, ``inspect``)."""
        from src.managers.shared.source_inspector import inspect_document
        from src.models.source_file import (
            DocumentMetadata,
            SourceKind,
            format_of_filename,
        )

        raw = data.encode("utf-8") if isinstance(data, str) else data
        fmt = format_of_filename(filename)
        assert fmt is not None
        draft = SourceFile.create_new(
            owner_sub=owner,
            filename=filename,
            size_bytes=len(raw),
            part_count=1,
            now=START - timedelta(hours=1),
            kind=SourceKind.DOCUMENT,
        )
        self.s3_client.put_object(Bucket=BUCKET, Key=draft.s3_key, Body=raw)
        metadata = (
            inspect_document(
                raw,
                raw,
                source_format=fmt,
                total_size=len(raw),
                now=START,
                chars_per_mb=20_000,
            )
            if inspect
            else DocumentMetadata(
                format=fmt, estimated_chars=1, inspected_bytes=1, inspected_at=START
            )
        )
        ready = draft.update(
            status=SourceFileStatus.READY, metadata=metadata, checksum="crc=="
        )
        self.files.put_new(ready)
        return ready


def _start_with(manager, *files: SourceFile, count: int = 2, purpose: str = "") -> str:
    return manager.start_generation(
        ADMIN,
        source_file_ids=[f.source_file_id for f in files],
        purpose=purpose,
        persona_count=count,
    )


def _start(manager, file: SourceFile, *, count: int = 2, principal=ADMIN) -> str:
    return manager.start_generation(
        principal,
        source_file_ids=[file.source_file_id],
        purpose="",
        persona_count=count,
    )


def _events(env: _Env, generation_id: str) -> list[tuple[str, dict]]:
    return [
        (e.event_type.value, json.loads(e.summary or "{}"))
        for e in env.generations.read_events(generation_id)
    ]


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("end step failed")


# --- Reuse and expiry -----------------------------------------------------

DAYS = config.ADVANCED_SOURCE_RETENTION_DAYS
REFRESH_FIELDS = {
    "event",
    "ts",
    "generation_id",
    "source_file_id",
    "status",
    "size_bytes",
    "duration_ms",
    "error_code",
}


def _refresh(env: _Env, monkeypatch, *results: Any) -> Mock:
    mock = Mock(side_effect=list(results))
    monkeypatch.setattr(env.s3, "refresh_object", mock)
    return mock


def _transient() -> S3ServiceError:
    return S3ServiceError("down", code=ErrorCode.S3_OPERATION_FAILED)


def _bad_column_csv(rows: int, bad_every: int) -> bytes:
    lines = ["id,age,income,job"]
    for i in range(rows):
        extra = ",x" if bad_every and i % bad_every == 0 else ""
        lines.append(f"{i},{30 + i % 7},{300 + i},学生{extra}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _failed(env: _Env, generation_id: str) -> Any:
    generation = env.generations.get(generation_id)
    assert generation is not None
    assert generation.status is GenerationStatus.FAILED
    return generation


def _ended_cleanly(env: _Env, file: SourceFile) -> None:
    assert env.generations.get_lock(ADMIN.sub) is None
    released = env.files.get(file.source_file_id)
    assert released is not None and released.in_use_by is None
    assert len(env.sandbox.stopped) == 1


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _progress(env: _Env, generation_id: str) -> list[str]:
    return [
        str(s.get("message", ""))
        for kind, s in _events(env, generation_id)
        if kind == "progress"
    ]


def _ended_cleanly_all(env: _Env, *files: SourceFile) -> None:
    assert env.generations.get_lock(ADMIN.sub) is None
    for file in files:
        released = env.files.get(file.source_file_id)
        assert released is not None and released.in_use_by is None
    assert len(env.sandbox.stopped) == 1


# =============================================================================
# The generation's controls
# =============================================================================

#: The watch and the plan wait read every 10 ms; the heartbeat goes as often.
FAST_CONTROL = {"control_poll_seconds": 0.01, "heartbeat_seconds": 0.01}
AWAITING = GenerationStatus.AWAITING_PLAN_APPROVAL


def _wait_for(predicate, *, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("the condition was not reached in time")


class _Thread:
    """``manager.run`` in its own thread, like the router's pool."""

    def __init__(self, manager, generation_id: str) -> None:
        import threading

        # daemon: a run that never ends must not keep the test process alive
        self.thread = threading.Thread(
            target=manager.run, args=(generation_id,), daemon=True
        )
        self.thread.start()

    def join(self) -> None:
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "the run did not end"


def _current(env: _Env, generation_id: str):
    generation = env.generations.get(generation_id)
    assert generation is not None
    return generation


def _start_confirming(manager, file: SourceFile, *, purpose: str = "30 代") -> str:
    return manager.start_generation(
        ADMIN,
        source_file_ids=[file.source_file_id],
        purpose=purpose,
        persona_count=2,
        confirm_plan=True,
    )


def _await_card(env: _Env, generation_id: str, *, replans: int = 0) -> None:
    _wait_for(
        lambda: (
            (g := env.generations.get(generation_id)) is not None
            and g.status is AWAITING
            and g.replan_count == replans
            and _events(env, generation_id)[-1][0] == "plan"
        )
    )


def _advance(env: _Env, generation_id: str, **delta: float) -> None:
    """Move the clock and wait until the watch has beaten at the new time."""
    env.clock.advance(**delta)
    _wait_for(lambda: _current(env, generation_id).heartbeat_at == env.clock.now)


class _FlakyStore(AdvancedGenerationStore):
    """The real store whose watch reads or heartbeats fail on demand."""

    watch_read_failures = 0
    heartbeat_fails = False

    def get(self, generation_id: str):
        import threading

        if (
            threading.current_thread().name == "advanced-generation-watch"
            and self.watch_read_failures > 0
        ):
            self.watch_read_failures -= 1
            raise AdvancedGenerationStoreError("get_item unreachable")
        return super().get(generation_id)

    def heartbeat(self, generation_id: str, *, now: datetime) -> bool:
        if self.heartbeat_fails:
            raise AdvancedGenerationStoreError("update_item unreachable")
        return super().heartbeat(generation_id, now=now)


def _flaky(env: _Env) -> _FlakyStore:
    return _FlakyStore(
        region=REGION,
        table_name=GENERATIONS_TABLE,
        source_files_table_name=SOURCE_FILES_TABLE,
        client=env.generations.client,
    )


def _assert_cancelled_and_released(env: _Env, generation_id: str, file) -> None:
    generation = _current(env, generation_id)
    assert generation.status is GenerationStatus.CANCELLED
    assert generation.error_code is None and generation.control_request is None
    assert generation.candidate_ids is None and generation.finished_at is not None
    kinds = [k for k, _ in _events(env, generation_id)]
    assert kinds[-1] == "cancelled" and "error" not in kinds
    assert env.generations.get_lock(ADMIN.sub) is None
    released = env.files.get(file.source_file_id)
    assert released is not None and released.in_use_by is None


def _left_alone(env: _Env, generation_id: str, *, session_id: str = "s-lost") -> None:
    """A started generation whose task went away (no heartbeat, a session)."""
    env.generations.transition(
        generation_id,
        GenerationStatus.PREPARING,
        GenerationStatus.PREPARING,
        updates={"session_id": session_id},
    )


class _UnreachableSandbox(_Sandbox):
    """Code Interpreter that cannot be reached."""

    def start_session(self, generation_id: str, lifetime_seconds: int):
        from src.services.sandbox_service import SandboxUnavailableError

        raise SandboxUnavailableError(
            "StartCodeInterpreterSession failed: ConnectTimeoutError at endpoint-x",
            context={"cause": "code_interpreter"},
        )


# --- Review fixes -------------------------------------------------------------


class _ObservedStore(AdvancedGenerationStore):
    """The real store that notes the holds at each event and runs hooks.

    ``after_verifying`` runs right after the move into ``verifying``;
    ``before_parts`` right before the result parts are written (after the
    record and the candidates).
    """

    def __init__(self, env: _Env) -> None:
        super().__init__(
            region=REGION,
            table_name=GENERATIONS_TABLE,
            source_files_table_name=SOURCE_FILES_TABLE,
            client=env.generations.client,
        )
        self.env = env
        self.held_at_event: list[tuple[str, bool]] = []
        self.after_verifying: Any = None
        self.before_parts: Any = None

    def append_event(self, generation_id, event_type, **kwargs):
        lock = self.env.generations.get_lock(ADMIN.sub)
        held = lock is not None and lock.generation_id == generation_id
        self.held_at_event.append((event_type.value, held))
        return super().append_event(generation_id, event_type, **kwargs)

    def transition(self, generation_id, expected, new_status, **kwargs):
        moved = super().transition(generation_id, expected, new_status, **kwargs)
        if moved and new_status is GenerationStatus.VERIFYING and self.after_verifying:
            self.after_verifying()
        return moved

    def put_result_parts(self, generation_id, parts, **kwargs):
        if self.before_parts is not None:
            self.before_parts()
        return super().put_result_parts(generation_id, parts, **kwargs)


def _held_at_stop(env: _Env, file: SourceFile) -> list[bool]:
    """Whether the lock or the mark was still held each time the session stopped."""
    seen: list[bool] = []
    stop = env.sandbox.stop_session

    def observed(session: SandboxSession) -> None:
        mark = env.files.get(file.source_file_id)
        seen.append(
            env.generations.get_lock(ADMIN.sub) is not None
            or (mark is not None and mark.in_use_by is not None)
        )
        stop(session)

    env.sandbox.stop_session = observed  # type: ignore[method-assign]
    return seen


def _candidate_count(env: _Env) -> int:
    return int(
        env.candidates.client.scan(TableName=CANDIDATES_TABLE, Select="COUNT")["Count"]
    )


def _record_exists(env: _Env, generation_id: str) -> bool:
    try:
        env.s3.get_object_tags(record_key(ADMIN.sub, generation_id))
    except S3ServiceError:
        return False
    return True


def _model_down() -> None:
    raise AgentServiceError(
        "advanced analysis agent failed (ThrottlingException)",
        code=ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
        context={"cause": "model"},
    )


#: The watch never reads: only the check right before the results can see it.
_NO_WATCH = {"control_poll_seconds": 3600}


# ---------------------------------------------------------------------------
# The AI detailed analysis and several tables
# ---------------------------------------------------------------------------

_ALLOWED = {"audience_mode": "everyone", "allow_detailed_analysis": True, "version": 1}
_EMIT = (
    'df = read_table("source_1")\n'
    'df["seg"] = ["young" if a < 40 else "old" for a in df["age"]]\n'
    'print("print-secret 77777")\n'
    'emit_result(df, "seg", "source_1", id_column="id")\n'
)
_EMIT_JOIN = (
    'df = join_tables("source_1", "id", [{"source": "source_2",'
    ' "aggregations": [{"column": "amount", "func": "sum"}]}])\n'
    'df["seg"] = ["young" if a < 40 else "old" for a in df["age"]]\n'
    'emit_result(df, "seg", "join:source_1+source_2", id_column="id")\n'
)
_DISABLED_NOTICE = "AI による詳細分析をやめて、事前定義された関数で続けます"


class _CodeSandbox(_Sandbox):
    """The in-process library, told apart as Code Interpreter (run_code given)."""

    @property
    def kind(self) -> str:
        return "code_interpreter"


def _purchases_csv(people: int = 200) -> bytes:
    lines = ["id,amount"]
    for i in range(people):
        lines += [f"{i},{100 + i}", f"{i},{200 + i}"]
    return ("\n".join(lines) + "\n").encode()


def _detailed_env(env: _Env) -> _Env:
    env.sandbox = _CodeSandbox(
        read_object=env.sandbox._read_object,
        root_dir=env.sandbox.root_dir,
    )
    env.settings.get.return_value = dict(_ALLOWED)
    return env


def _start_detailed(manager, *files: SourceFile, checked: bool = True) -> str:
    return manager.start_generation(
        ADMIN,
        source_file_ids=[f.source_file_id for f in files],
        purpose="",
        persona_count=2,
        use_detailed_analysis=checked,
    )


def _call_id(text: str) -> str:
    return text.splitlines()[0].removeprefix("tool_call_id: ")


def _from_emitted(service: Any, call_id: str) -> dict[str, Any]:
    result = service.emitted_results()[call_id]
    segments, evidence = [], []
    for n, item in enumerate(result.segments):
        key, size = item["segment_key"], item["size"]
        segments.append(
            {
                "id": f"seg-{n}",
                "label": key,
                "definition": {
                    "kind": "derived",
                    "source": result.population_source,
                    "spec": {"tool_call_id": call_id, "segment_key": key},
                },
                "size": size,
                "share": round(size / result.population, 3),
                "features": [],
                "evidence_ids": [f"ev-{n}"],
                "overlaps": False,
            }
        )
        evidence.append(
            {
                "id": f"ev-{n}",
                "source_id": "x",
                "location": call_id,
                "kind": "attribute",
                "claim": f"{key} の層",
                "statistic": {"value": size, "population": result.population},
            }
        )
    return {"segments": segments, "evidence": evidence}


def _cluster_fallback(tools: dict, holder: dict) -> dict[str, Any]:
    text = tools["cluster_segments"](columns=["age", "income"], source="source_1")
    call_id = _call_id(text)
    output = holder["service"].output_of(call_id)
    segments: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    if output is None:  # the tools halted (a change was found)
        return {"segments": segments, "evidence": evidence}
    for cluster in output["clusters"]:
        i = cluster["index"]
        segments.append(
            {
                "id": f"seg-{i}",
                "label": f"層 {i}",
                "definition": {
                    "kind": "cluster",
                    "source": "source_1",
                    "spec": {"tool_call_id": call_id, "cluster_index": i},
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
                "claim": "年齢と収入",
            }
        )
    return {"segments": segments, "evidence": evidence}


def _code_analysis(failures: int = 0, *, after=None, code: str = _EMIT):
    def analysis(tools: dict, holder: dict, source_id: str) -> dict[str, Any]:
        for i in range(failures):
            tools["run_code"](code=f"raise ValueError('boom-secret {i}')")
        text = tools["run_code"](code=code)
        service = holder["service"]
        if after is not None:
            after(holder)
        if _call_id(text) in service.emitted_results():
            return _from_emitted(service, _call_id(text))
        return _cluster_fallback(tools, holder)

    return analysis


class _NoParquetHashSandbox(_CodeSandbox):
    """The preparation reports no ``parquet_sha256``."""

    def poll_command(self, session, command_id):
        status = super().poll_command(session, command_id)
        result = status.get("result")
        if (
            self.functions.get(command_id) == "prepare"
            and isinstance(result, dict)
            and isinstance(result.get("value"), dict)
        ):
            value = {k: v for k, v in result["value"].items() if k != "parquet_sha256"}
            status = {**status, "result": {**result, "value": value}}
        return status


class _NoLibraryHashSandbox(_CodeSandbox):
    """The session holds no library hash to compare with."""

    @property
    def library_sha256(self) -> str:
        return ""


class _FailingCheckSandbox(_CodeSandbox):
    """The isolated check itself fails for one function."""

    failing: str = "verify_hashes"

    def call_verified(self, session, function, args):
        if function == self.failing:
            return {"ok": False, "error_code": "SANDBOX_COMMAND_FAILED"}
        return super().call_verified(session, function, args)


class _ScanSandbox(_CodeSandbox):
    """The final check reports this process scan (security review)."""

    scan: dict = {"process_scan": "ok", "leftover_processes": 1}

    def call_verified(self, session, function, args):
        result = super().call_verified(session, function, args)
        if function == "verify_hashes" and isinstance(result.get("value"), dict):
            value = {
                k: v
                for k, v in result["value"].items()
                if k not in ("process_scan", "leftover_processes")
            }
            return {**result, "value": {**value, **self.scan}}
        return result


_ESCAPE_THEN_EMIT = (
    "import os, time\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.setsid()\n"
    "    time.sleep(30)\n"
    "    os._exit(0)\n"
    "open('child.pid', 'w').write(str(pid))\n" + _EMIT
)


def _with_sandbox(env: _Env, cls: type) -> _Env:
    env = _detailed_env(env)
    env.sandbox = cls(
        read_object=env.sandbox._read_object, root_dir=env.sandbox.root_dir
    )
    return env
