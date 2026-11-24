"""AdvancedPersonaGenerationManager: start, run and reads of a generation (W4〜W6).

US5.1, US9.1, US10.1, BR5.5, BR5.8, BR6.1, BR6.2, BR7.1〜BR7.5, NFR2.2,
NFR2.3, NFR5.6, NFR5.14, NFR5.15, NFR6.1〜NFR6.5.

DynamoDB and S3 are moto with the real stores; the sandbox is the in-process
version running ``sandbox/persona_analysis.py`` (the session's download from
the presigned URL is replaced by a read of the same key); the agents are a
fake that calls the real tools and answers with the shapes AgentService returns.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest.mock import Mock

import boto3
import pytest

moto = pytest.importorskip("moto")

from src.config import config  # noqa: E402
from src.managers.advanced_generation.analysis_policy import AnalysisPolicy  # noqa: E402
from src.managers.advanced_generation.manager import (  # noqa: E402
    AdvancedGenerationError,
    AdvancedPersonaGenerationManager,
)
from src.managers.components.analysis_record_archive import record_key  # noqa: E402
from src.managers.shared.source_inspector import inspect_table_head  # noqa: E402
from src.models.advanced_generation import (  # noqa: E402
    GenerationStatus,
    ResultPartName,
)
from src.models.authz import Principal, Role  # noqa: E402
from src.models.errors import ErrorCode  # noqa: E402
from src.models.source_file import SourceFile, SourceFileStatus, SourceFormat  # noqa: E402
from src.services.agent_service import AgentServiceError  # noqa: E402
from src.services.s3_service import S3Service, S3ServiceError  # noqa: E402
from src.services.dataset_analysis.analysis_tool_service import (  # noqa: E402
    AnalysisToolService,
)
from src.services.sandbox_service import InProcessSandbox, SandboxSession  # noqa: E402
from src.services.service_factory import service_factory  # noqa: E402
from src.services.stores.advanced_generation_store import (  # noqa: E402
    AdvancedGenerationStore,
    AdvancedGenerationStoreError,
)
from src.services.stores.app_settings_store import AppSettingsStore  # noqa: E402
from src.services.stores.persona_candidate_store import (  # noqa: E402
    PersonaCandidateStore,
)
from src.services.stores.source_file_store import SourceFileStore  # noqa: E402
from tests.advanced_helpers import (  # noqa: E402
    BUCKET,
    CANDIDATES_TABLE,
    GENERATIONS_TABLE,
    REGION,
    SOURCE_FILES_TABLE,
    create_candidates_table,
    create_generations_table,
    create_source_files_table,
)
from tests.error_helpers import raises_code  # noqa: E402
from tests.log_helpers import LogCapture, assert_not_leaked  # noqa: E402

pytestmark = pytest.mark.unit

LIBRARY = Path(__file__).resolve().parents[2] / "sandbox" / "persona_analysis.py"
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


#: Labels the fake extraction and synthesis read evidence from (U4).
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
        # U4: what was started and called, and fake document preparations
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

    #: U5: sessions a reader stopped by their ID (BR32.4, NFR5.48)
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
        #: The ``may_retry`` each extraction call was given (BR26.2)
        self.may_retry: list[Any] = []
        #: The ``purpose`` each extraction call was given (BR26.1)
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

    # --- U4: documents ---------------------------------------------------------

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

    #: U6 addendum: the planning and analysis requests, in order
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

    #: U5: called inside the analysis call (the model is "running")
    on_analysis: Any = None
    #: U5: called inside each extraction call
    on_extraction: Any = None

    #: U6: replaces the analysis (tools, holder, source_id) -> output
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


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", True)
    monkeypatch.setattr(config, "CODE_INTERPRETER_ID", "ci-1")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        dynamo = boto3.client("dynamodb", region_name=REGION)
        create_source_files_table(dynamo)
        create_generations_table(dynamo)
        create_candidates_table(dynamo)
        s3_client = boto3.client("s3", region_name=REGION)
        s3_client.create_bucket(Bucket=BUCKET)
        yield _Env(dynamo, s3_client, tmp_path)


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
            # U6: the same Service, polling a running run_code quickly
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
        """A ready document (U4) inspected like an upload (or not, ``inspect``)."""
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


class TestStart:
    def test_writes_lock_generation_and_mark(self, env: _Env) -> None:
        file = env.ready_file()

        generation_id = _start(env.manager(), file)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.PREPARING
        lock = env.generations.get_lock(ADMIN.sub)
        assert lock is not None and lock.generation_id == generation_id
        marked = env.files.get(file.source_file_id)
        assert marked is not None and marked.in_use_by == generation_id

    def test_unavailable_user_is_refused(self, env: _Env) -> None:
        file = env.ready_file(owner=MEMBER.sub)

        with raises_code(
            AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_UNAVAILABLE
        ):
            _start(env.manager(), file, principal=MEMBER)

    @pytest.mark.parametrize("count", [0, 11])
    def test_persona_count_outside_1_to_10(self, env: _Env, count: int) -> None:
        file = env.ready_file()

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_PERSONA_COUNT_INVALID
        ):
            _start(env.manager(), file, count=count)

    def test_four_tables_are_refused(self, env: _Env) -> None:
        # U6 BR37.1 replaced U4's one table: three tables at most
        files = [env.ready_file() for _ in range(4)]

        with raises_code(
            AdvancedGenerationError,
            ErrorCode.UPLOAD_SOURCE_INVALID,
            reason="tables",
            max=3,
        ):
            env.manager().start_generation(
                ADMIN,
                source_file_ids=[f.source_file_id for f in files],
                purpose="",
                persona_count=2,
            )

    def test_purpose_over_the_limit_is_refused(self, env: _Env) -> None:
        file = env.ready_file()

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_PURPOSE_TOO_LONG
        ):
            env.manager().start_generation(
                ADMIN,
                source_file_ids=[file.source_file_id],
                purpose="あ" * 2001,
                persona_count=2,
            )

    def test_someone_elses_file_is_not_found(self, env: _Env) -> None:
        file = env.ready_file(owner=OTHER.sub)

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_SOURCE_NOT_FOUND
        ):
            _start(env.manager(), file)

    def test_no_free_slot_is_busy_and_writes_nothing(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=1)
        _start(manager, env.ready_file())
        other = env.ready_file(owner=OTHER.sub)

        with raises_code(AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_BUSY):
            _start(manager, other, principal=OTHER)
        assert env.generations.get_lock(OTHER.sub) is None

    def test_held_lock_is_already_running_and_returns_the_slot(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=2)
        _start(manager, env.ready_file())

        with raises_code(
            AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING
        ):
            _start(manager, env.ready_file())
        # The refused start gave its slot back: another user can still start.
        _start(manager, env.ready_file(owner=OTHER.sub), principal=OTHER)


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

        params = AdvancedPersonaGenerationManager._u6_params(self._state(profile))

        assert params["rows_are_not_people"] is True

    def test_a_table_of_people_records_nothing(self) -> None:
        profile = {
            "columns": [{"name": "顧客ID", "type": "VARCHAR", "all_distinct": True}]
        }

        params = AdvancedPersonaGenerationManager._u6_params(self._state(profile))

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
        # NFR1.6 / NFR5.7: the 20-minute preparation limit, told apart from the 60
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
        # NFR1.8: the interval comes from config, not from the manager
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
        # entities.md「1 つの item は 64KB まで」: done で書く本体の大きさを先に測る
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


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("end step failed")


class TestSlotIsAlwaysReturned:
    """The free slot goes back last and once, whatever an end step raises (§2)."""

    def _slot_is_free_exactly_once(self, manager, env: _Env, caplog) -> None:
        _start(manager, env.ready_file(owner=OTHER.sub), principal=OTHER)
        with raises_code(AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_BUSY):
            _start(manager, env.ready_file())
        assert "released twice" not in caplog.text

    def test_run_returns_the_slot_when_the_end_log_raises(
        self, env: _Env, caplog, monkeypatch
    ) -> None:
        manager = env.manager(max_concurrent=1)
        generation_id = _start(manager, env.ready_file())
        monkeypatch.setattr(manager, "_log_finished", _boom)

        manager.run(generation_id)  # never raises (a pool thread)

        self._slot_is_free_exactly_once(manager, env, caplog)

    def test_run_returns_the_slot_when_the_store_end_raises(
        self, env: _Env, caplog, monkeypatch
    ) -> None:
        manager = env.manager(max_concurrent=1)
        generation_id = _start(manager, env.ready_file())
        monkeypatch.setattr(env.generations, "finish", _boom)
        monkeypatch.setattr(manager, "_log_finished", _boom)

        manager.run(generation_id)

        self._slot_is_free_exactly_once(manager, env, caplog)

    def test_abort_start_returns_the_slot_when_the_transition_raises(
        self, env: _Env, caplog, monkeypatch
    ) -> None:
        manager = env.manager(max_concurrent=1)
        generation_id = _start(manager, env.ready_file())
        monkeypatch.setattr(env.generations, "transition", _boom)

        manager.abort_start(generation_id)

        # The lock and the mark still come off (each end step stands alone).
        assert env.generations.get_lock(ADMIN.sub) is None
        self._slot_is_free_exactly_once(manager, env, caplog)

    def test_abort_start_returns_the_slot_when_the_read_raises(
        self, env: _Env, caplog
    ) -> None:
        manager = env.manager(max_concurrent=1)
        generation_id = _start(manager, env.ready_file())
        env.generations.get = _boom  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError):
                manager.abort_start(generation_id)
        finally:
            del env.generations.get  # back to the store's own method

        self._slot_is_free_exactly_once(manager, env, caplog)


class TestReads:
    def _done(self, env: _Env) -> tuple[AdvancedPersonaGenerationManager, str]:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        manager.run(generation_id)
        return manager, generation_id

    def test_events_after_a_sequence(self, env: _Env) -> None:
        manager, generation_id = self._done(env)

        progress = manager.stream_events(ADMIN, generation_id, after_sequence=2)

        assert progress.is_finished
        assert [e.sequence for e in progress.events][0] == 3

    def test_elapsed_of_a_running_generation_counts_from_its_start(
        self, env: _Env
    ) -> None:
        # AC9.1.1: 経過時間は生成の created_at から数える
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        env.clock.advance(minutes=2, seconds=14)

        clock = manager.elapsed_clock(ADMIN, generation_id)

        assert clock.seconds == 134
        assert clock.running is True

    def test_elapsed_of_a_finished_generation_stops_at_its_end(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        env.sandbox.on_profile = lambda: env.clock.advance(minutes=3)
        manager.run(generation_id)
        env.clock.advance(minutes=10)

        clock = manager.elapsed_clock(ADMIN, generation_id)

        assert clock.seconds == 180
        assert clock.running is False

    def test_elapsed_of_someone_elses_generation_is_not_found(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.elapsed_clock(OTHER, generation_id)

    def test_someone_elses_generation_is_not_found(self, env: _Env) -> None:
        manager, generation_id = self._done(env)

        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.stream_events(OTHER, generation_id)
        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.get_result(OTHER, generation_id)

    def test_past_24_hours_is_expired(self, env: _Env) -> None:
        manager, generation_id = self._done(env)
        env.clock.advance(hours=25)

        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_EXPIRED):
            manager.stream_events(ADMIN, generation_id)
        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_EXPIRED):
            manager.get_record(ADMIN, generation_id)

    def test_result_and_record(self, env: _Env) -> None:
        manager, generation_id = self._done(env)

        result = manager.get_result(ADMIN, generation_id)
        record = manager.get_record(ADMIN, generation_id)

        assert len(result.candidates) == 2
        assert sum(s.size for s in result.segments) == 300
        assert result.prepare_reports[0].rows_read == 300
        assert record.generation_id == generation_id
        assert record.seed == 42
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", b["sha256"]) for b in record.code_blocks
        )

    def test_running_generation_is_found_through_the_owner_lock(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        assert manager.running_generation_id(ADMIN) == generation_id
        assert manager.running_generation_id(OTHER) is None

    def test_no_running_generation_after_the_end(self, env: _Env) -> None:
        manager, _ = self._done(env)

        assert manager.running_generation_id(ADMIN) is None

    def test_unreadable_lock_means_no_running_generation(self, env: _Env) -> None:
        failing = Mock(spec=AdvancedGenerationStore)
        failing.get_lock.side_effect = AdvancedGenerationError(
            "down", code=ErrorCode.GENERATION_OPERATION_FAILED
        )
        manager = env.manager(generation_store=failing)

        assert manager.running_generation_id(ADMIN) is None

    def test_is_available_uses_the_policy_only(self, env: _Env) -> None:
        manager = AdvancedPersonaGenerationManager(settings_store=env.settings)

        assert manager.is_available(ADMIN) is True
        assert manager.is_available(MEMBER) is False


# --- U2: 使い回しと期限（BR13.3、BR13.4、BR13.5、BR14.3、NFR5.30、NFR6.9） -----------

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


class TestReuseAndExpiry:
    def test_the_start_stamps_the_last_use_and_moves_the_expiry(
        self, env: _Env
    ) -> None:
        file = env.ready_file()
        env.clock.advance(days=2)

        _start(env.manager(), file)

        found = env.files.get(file.source_file_id)
        assert found is not None
        assert found.last_used_at == env.clock.now
        assert found.expires_at == env.clock.now + timedelta(days=DAYS)

    def test_an_expired_file_cannot_start(self, env: _Env) -> None:
        file = env.ready_file()  # an U1-style record: no expires_at
        env.clock.advance(days=DAYS)

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_SOURCE_NOT_FOUND
        ):
            _start(env.manager(), file)
        found = env.files.get(file.source_file_id)
        assert found is not None and found.in_use_by is None

    def test_the_end_does_not_stamp_the_last_use(self, env: _Env) -> None:
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)
        env.clock.advance(minutes=5)

        manager.run(generation_id)

        found = env.files.get(file.source_file_id)
        assert found is not None and found.last_used_at == START


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


class TestPrepareFailureContext:
    """BR16.2, BR17.1, NFR3.20: counts and line numbers reach error_context."""

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
    """BR18.1, BR18.2: fewer rows than persona count x 10 stop before planning."""

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


class TestPrepareNotice:
    """BR17.2, NFR1.13: a notice every 60 seconds while the rows stand still."""

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
    """NFR6.11, NFR1.12: counts and time of the preparation, no values."""

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


# ---------------------------------------------------------------------------
# U4: documents (BR23〜BR28)
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


@pytest.fixture
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunks of 300 characters with 50 of overlap, handed over 120 at a time."""
    monkeypatch.setattr(config, "ADVANCED_DOCUMENT_CHUNK_CHARS", 300)
    monkeypatch.setattr(config, "ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS", 50)
    monkeypatch.setattr(config, "ADVANCED_DOCUMENT_TRANSFER_CHARS", 120)


@pytest.fixture
def extraction(monkeypatch: pytest.MonkeyPatch, small_chunks: None) -> None:
    """Every document goes to the evidence extraction (BR25.2)."""
    monkeypatch.setattr(config, "PERSONA_SOURCE_MAX_CHARS", 10)


class TestDocumentPreparation:
    """Step 17: BR24.4〜BR24.6, BR25.1, BR25.4, NFR1.19, NFR1.22, NFR3.22, NFR3.24."""

    def test_every_document_is_refreshed_brought_in_and_prepared(
        self, env: _Env, monkeypatch
    ) -> None:
        refreshed: list[str] = []
        original = env.s3.refresh_object

        def spy(key: str) -> Any:
            refreshed.append(key)
            return original(key)

        monkeypatch.setattr(env.s3, "refresh_object", spy)
        a = env.ready_document(_interview(2), filename="a.md")
        b = env.ready_document(_interview(3), filename="b.txt")
        manager = env.manager()
        generation_id = _start_with(manager, a, b)

        manager.run(generation_id)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        assert refreshed == [a.s3_key, b.s3_key]
        assert len(env.sandbox.download_urls) == 2
        prepared = [
            args for fn, args in env.sandbox.started if fn == "prepare_document"
        ]
        assert [p["source_index"] for p in prepared] == [0, 1]
        assert [p["format"] for p in prepared] == ["md", "txt"]
        assert prepared[0]["chunk_chars"] == config.ADVANCED_DOCUMENT_CHUNK_CHARS
        assert (
            prepared[0]["overlap_chars"] == config.ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS
        )
        messages = _progress(env, generation_id)
        assert any("1 件目の文書" in m and "分割 1 個" in m for m in messages)
        assert any("2 件目の文書" in m for m in messages)
        _ended_cleanly_all(env, a, b)

    def test_pages_and_pages_without_text_are_told(self, env: _Env) -> None:
        from tests.document_fixtures import pdf_bytes

        pages = [
            "Value: family time matters most to me every single day.",
            "",
            "Pain: waiting at the counter for thirty minutes is too long.",
        ]
        doc = env.ready_document(pdf_bytes(pages), filename="a.pdf")
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        messages = _progress(env, generation_id)
        assert any("3 ページ" in m for m in messages)
        assert any("文字がないため除外" in m and "2" in m for m in messages)

    def test_the_table_is_prepared_before_the_documents(self, env: _Env) -> None:
        table = env.ready_file()
        doc = env.ready_document(_interview(2))
        manager = env.manager()
        generation_id = _start_with(manager, table, doc)

        manager.run(generation_id)

        assert [fn for fn, _ in env.sandbox.started] == ["prepare", "prepare_document"]
        assert env.generations.get(generation_id).status is GenerationStatus.DONE

    def test_a_document_without_text_stops_before_any_model(self, env: _Env) -> None:
        from tests.document_fixtures import pdf_bytes

        table = env.ready_file()
        doc = env.ready_document(pdf_bytes(["", ""]), filename="blank.pdf")
        manager = env.manager()
        generation_id = _start_with(manager, table, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.DOCUMENT_NO_TEXT.value
        assert failed.error_context == {"source_index": 1}
        assert env.agents.calls == []
        _ended_cleanly_all(env, table, doc)

    def test_an_encrypted_document_stops(self, env: _Env) -> None:
        from tests.document_fixtures import pdf_bytes

        data = pdf_bytes(["secret"], encrypted=True)
        # passed the upload check (say, the trailer was outside the tail read)
        doc = env.ready_document(data, filename="locked.pdf", inspect=False)
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.DOCUMENT_ENCRYPTED.value
        assert failed.error_context == {"source_index": 0}
        _ended_cleanly_all(env, doc)

    def test_over_the_total_limit_stops_before_any_model(
        self, env: _Env, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_DOCUMENT_MAX_TOTAL_CHARS", 100)
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_SOURCE_TOO_LARGE.value
        assert failed.error_context["max_chars"] == 100
        assert failed.error_context["total_chars"] > 100
        assert env.agents.calls == []

    def test_only_integers_of_a_failure_are_kept(self, env: _Env) -> None:
        env.sandbox.fake_document_result = {
            "ok": False,
            "error": {
                "code": "DOCUMENT_NO_TEXT",
                "context": {"source_index": "本文の一部", "detail": "secret"},
            },
        }
        doc = env.ready_document(_interview(1))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.DOCUMENT_NO_TEXT.value
        assert not failed.error_context

    def test_the_twenty_minutes_count_the_table_and_the_documents(
        self, env: _Env, monkeypatch
    ) -> None:
        table = env.ready_file()
        doc = env.ready_document(_interview(1))
        env.sandbox.on_profile = lambda: env.clock.advance(minutes=15)
        polls = []
        original = env.sandbox.poll_command

        def held(session, command_id):
            if env.sandbox.functions.get(command_id) == "prepare_document":
                polls.append(command_id)
                return {"done": False, "status": "running", "progress": None}
            return original(session, command_id)

        monkeypatch.setattr(env.sandbox, "poll_command", held)
        manager = env.manager(sleep=lambda s: env.clock.advance(minutes=2))
        generation_id = _start_with(manager, table, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert failed.error_context["limit"] == "prepare_minutes"
        # about 5 minutes were left after the table, not a fresh 20
        assert len(polls) <= 4


class TestDocumentMode:
    """Step 19: BR25.2, BR25.3, BR26.4, AC6.1.1, NFR4.3."""

    def _chars(self, env: _Env, text: str) -> int:
        doc = env.ready_document(text)
        manager = env.manager()
        generation_id = _start_with(manager, doc)
        manager.run(generation_id)
        return int(
            env.generations.read_result_parts(
                generation_id, ResultPartName.DOCUMENT_REPORTS
            )[0]["chars"]
        )

    def test_exactly_the_limit_uses_the_full_text(
        self, env: _Env, monkeypatch, small_chunks
    ) -> None:
        text = _interview(3)
        chars = self._chars(env, text)
        monkeypatch.setattr(config, "PERSONA_SOURCE_MAX_CHARS", chars)
        env.agents.calls.clear()
        env.agents.documents_seen.clear()
        env.agents.data_seen.clear()
        doc = env.ready_document(text)
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.document_mode.value == "full_text"
        assert "extraction" not in env.agents.calls
        # the planning may be asked twice (the guide is filter for documents)
        planning_docs, synthesis_docs = (
            env.agents.documents_seen[0],
            env.agents.documents_seen[-1],
        )
        assert len(planning_docs) == 1 and planning_docs == synthesis_docs
        # the chunks are joined without the overlap: each line once
        assert planning_docs[0].count("価値: ") == 3
        assert len(planning_docs[0]) == chars
        assert any("全文を使います" in m for m in _progress(env, generation_id))

    def test_one_character_over_extracts_evidence(
        self, env: _Env, monkeypatch, small_chunks
    ) -> None:
        text = _interview(3)
        chars = self._chars(env, text)
        monkeypatch.setattr(config, "PERSONA_SOURCE_MAX_CHARS", chars - 1)
        env.agents.calls.clear()
        env.agents.documents_seen.clear()
        env.agents.data_seen.clear()
        doc = env.ready_document(text)
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.document_mode.value == "extraction"
        assert "extraction" in env.agents.calls
        # BR26.3: planning and synthesis get the evidence list, not the text
        assert env.agents.documents_seen
        assert all(docs == [] for docs in env.agents.documents_seen)
        assert "document_evidence" in env.agents.data_seen[0]
        assert any("根拠を取り出します" in m for m in _progress(env, generation_id))

    def test_a_cut_transfer_fails_the_generation(self, env: _Env, small_chunks) -> None:
        env.sandbox.truncate_chunks = True
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_OPERATION_FAILED.value


class TestEvidenceExtraction:
    """Step 20: BR26.1〜BR26.3, BR26.5, NFR1.16〜NFR1.18, NFR2.7, NFR7.3."""

    def _run(self, env: _Env, *docs: SourceFile, count: int = 2) -> str:
        manager = env.manager()
        generation_id = _start_with(manager, *docs, count=count)
        manager.run(generation_id)
        return generation_id

    def _chunks(self, env: _Env, generation_id: str) -> int:
        return sum(
            r["chunks"]
            for r in env.generations.read_result_parts(
                generation_id, ResultPartName.DOCUMENT_REPORTS
            )
        )

    def test_one_call_per_chunk_and_never_more_than_the_limit_at_once(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 2)
        doc = env.ready_document(_interview(8))

        generation_id = self._run(env, doc)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        chunks = self._chunks(env, generation_id)
        assert chunks >= 4
        assert env.agents.calls.count("extraction") == chunks
        assert env.agents.max_running <= 2

    @pytest.mark.parametrize("purpose", ["30 代の不満を優先", ""])
    def test_every_chunk_call_gets_the_users_purpose(
        self, env: _Env, extraction, purpose: str
    ) -> None:
        """The prompt asks to favour the instruction, so it must be sent (BR26.1)."""
        doc = env.ready_document(_interview(8))
        manager = env.manager()
        generation_id = _start_with(manager, doc, purpose=purpose)

        manager.run(generation_id)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        assert env.agents.extraction_purposes
        assert set(env.agents.extraction_purposes) == {purpose}

    def test_progress_says_n_of_m_up_to_the_end(self, env: _Env, extraction) -> None:
        doc = env.ready_document(_interview(8))

        generation_id = self._run(env, doc)

        chunks = self._chunks(env, generation_id)
        counts = [m for m in _progress(env, generation_id) if "根拠の抽出" in m]
        assert counts[-1].endswith(f"{chunks} / {chunks}")
        assert len(counts) <= 11

    def test_items_and_quotes_are_cut_to_the_limits(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_PER_CHUNK_MAX", 3)
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_QUOTE_MAX_CHARS", 12)
        doc = env.ready_document(_interview(6))

        generation_id = self._run(env, doc, count=1)

        evidence = env.generations.read_result_parts(
            generation_id, ResultPartName.EVIDENCE
        )
        by_chunk: dict[str, int] = {}
        for item in evidence:
            assert len(item["quote"]) <= 12
            chunk = item["id"].rsplit("-", 1)[0]
            by_chunk[chunk] = by_chunk.get(chunk, 0) + 1
        assert by_chunk and max(by_chunk.values()) <= 3

    def test_ids_are_renumbered_and_the_source_comes_from_the_chunk(
        self, env: _Env, extraction
    ) -> None:
        table_free = env.ready_document(_interview(2), filename="a.md")
        other = env.ready_document(_interview(2, prefix="B"), filename="b.md")

        generation_id = self._run(env, table_free, other)

        evidence = env.generations.read_result_parts(
            generation_id, ResultPartName.EVIDENCE
        )
        assert evidence
        for item in evidence:
            assert re.fullmatch(r"ev-d[01]-\d+-\d+", item["id"])
            expected = table_free if item["id"].startswith("ev-d0") else other
            assert item["source_id"] == expected.source_file_id
            assert item["location"]
        assert len({e["id"] for e in evidence}) == len(evidence)

    def test_a_failure_stops_the_rest_and_fails_the_generation(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 1)
        env.agents.extraction_mode = "fail_at:3"
        doc = env.ready_document(_interview(8))

        generation_id = self._run(env, doc)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_SANDBOX_UNAVAILABLE.value
        assert failed.error_context == {"cause": "model"}
        assert env.agents.calls.count("extraction") == 3
        assert "planning" not in env.agents.calls
        _ended_cleanly_all(env, doc)

    def test_a_running_call_may_retry_until_another_one_fails(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 1)
        doc = env.ready_document(_interview(4))

        self._run(env, doc)

        assert env.agents.may_retry and all(callable(f) for f in env.agents.may_retry)
        # The run ended well: nothing stopped the retries of later calls
        assert all(f() for f in env.agents.may_retry)

    def test_after_a_failure_no_call_retries_or_starts(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 2)
        env.agents.extraction_mode = "fail_at:1"
        doc = env.ready_document(_interview(8))

        generation_id = self._run(env, doc)

        assert _failed(env, generation_id).error_code == (
            ErrorCode.GENERATION_SANDBOX_UNAVAILABLE.value
        )
        # Only the two calls already running when the first one failed
        assert env.agents.calls.count("extraction") <= 2
        assert not any(f() for f in env.agents.may_retry)

    def test_the_time_left_is_checked_before_each_call(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(
            config, "ADVANCED_GENERATION_MODEL_MIN_REMAINING_SECONDS", 3601
        )
        doc = env.ready_document(_interview(4))

        generation_id = self._run(env, doc)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert "extraction" not in env.agents.calls

    def test_an_answer_out_of_shape_gives_no_evidence(
        self, env: _Env, extraction
    ) -> None:
        env.agents.extraction_mode = "none"
        doc = env.ready_document(_interview(3))

        generation_id = self._run(env, doc)

        # nothing to rest the personas on: the verification fails (BR27.2)
        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_VERIFICATION_FAILED.value


class TestQuoteVerification:
    """Step 21: BR27.1, BR27.3, NFR1.21, NFR5.38, AC6.2.2."""

    def test_extracted_quotes_not_in_the_text_are_dropped_and_counted(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_VERIFY_BATCH", 2)
        env.agents.extraction_mode = "fabricate"
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.status is GenerationStatus.DONE
        stats = generation.evidence_stats
        assert stats["discarded_unverified"] == 1
        evidence = env.generations.read_result_parts(
            generation_id, ResultPartName.EVIDENCE
        )
        assert stats["extracted"] == len(evidence) + 1
        assert all("作り話" not in e["quote"] for e in evidence)
        batches = [a["quotes"] for fn, a in env.sandbox.called if fn == "verify_quotes"]
        assert batches and max(len(b) for b in batches) <= 2

    def test_full_text_quotes_are_checked_after_the_synthesis(self, env: _Env) -> None:
        env.agents.full_text_mode = "fabricate"
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.document_mode.value == "full_text"
        assert generation.evidence_stats["discarded_unverified"] == 1
        evidence = env.generations.read_result_parts(
            generation_id, ResultPartName.EVIDENCE
        )
        assert all(re.fullmatch(r"ev-d0-0-\d+", e["id"]) for e in evidence)
        assert all("作り話" not in e["quote"] for e in evidence)


class TestDocumentVerificationAndResult:
    """Steps 22〜23: BR27.2, BR28.1, BR28.2, AC6.1.3, AC7.1.1, AC7.1.2, NFR4.4."""

    def test_a_persona_without_goal_evidence_fails_the_verification(
        self, env: _Env
    ) -> None:
        env.agents.full_text_mode = "no_goal"
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_VERIFICATION_FAILED.value

    def test_unknown_evidence_ids_are_dropped_from_the_personas(
        self, env: _Env
    ) -> None:
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.status is GenerationStatus.DONE
        known = {
            e["id"]
            for e in env.generations.read_result_parts(
                generation_id, ResultPartName.EVIDENCE
            )
        }
        for candidate_id in generation.candidate_ids or ():
            context = env.candidates.get(candidate_id).generation_context
            assert context["segment_id"] is None
            assert context["evidence_ids"]
            assert set(context["evidence_ids"]) <= known

    def test_extraction_assigns_evidence_to_segments_before_the_synthesis(
        self, env: _Env, extraction
    ) -> None:
        table = env.ready_file()
        doc = env.ready_document(_interview(4))
        manager = env.manager()
        generation_id = _start_with(manager, table, doc)
        env.agents.assignments = {"ev-d1-0-0": "seg-0", "ev-d1-0-1": "seg-nope"}

        manager.run(generation_id)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        calls = env.agents.calls
        assert calls.index("assignment") < calls.index("synthesis")
        evidence = {
            e["id"]: e
            for e in env.generations.read_result_parts(
                generation_id, ResultPartName.EVIDENCE
            )
        }
        assert evidence["ev-d1-0-0"]["segment_id"] == "seg-0"
        assert evidence["ev-d1-0-0"]["assignment_reason"] == "30代の発言"
        assert "segment_id" not in evidence["ev-d1-0-1"]  # unknown segment
        assert "ev-unknown" not in evidence
        # BR28.2: the evidence without a segment goes to every persona
        synthesis = env.agents.data_seen[-1]
        given = {e["id"] for e in json.loads(synthesis["evidence"])}
        assert {"ev-d1-0-0", "ev-d1-0-1"} <= given

    def test_full_text_synthesis_returns_the_segments_itself(self, env: _Env) -> None:
        table = env.ready_file()
        doc = env.ready_document(_interview(2))
        manager = env.manager()
        generation_id = _start_with(manager, table, doc)

        manager.run(generation_id)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        assert "assignment" not in env.agents.calls
        evidence = [
            e
            for e in env.generations.read_result_parts(
                generation_id, ResultPartName.EVIDENCE
            )
            if e.get("quote")
        ]
        assert any(e.get("segment_id") == "seg-0" for e in evidence)
        assert any("segment_id" not in e for e in evidence)

    def test_the_result_lists_the_documents_used(self, env: _Env) -> None:
        doc = env.ready_document(_interview(2), filename="面談A.md")
        manager = env.manager()
        generation_id = _start_with(manager, doc)
        manager.run(generation_id)

        result = manager.get_result(ADMIN, generation_id)

        (report,) = result.document_reports
        assert report.source_id == doc.source_file_id
        assert result.source_names[doc.source_file_id] == "面談A.md"
        assert any(e.quote for e in result.evidence)

    def test_the_failure_names_the_users_file(self, env: _Env) -> None:
        from tests.document_fixtures import pdf_bytes

        doc = env.ready_document(pdf_bytes([""]), filename="白紙.pdf")
        manager = env.manager()
        generation_id = _start_with(manager, doc)
        manager.run(generation_id)

        progress = manager.stream_events(ADMIN, generation_id)

        assert progress.failure_context == {"source_index": 0, "filename": "白紙.pdf"}


class TestDocumentLogs:
    """Step 24: NFR6.13〜NFR6.15, NFR7.3 (counts, time and tokens only)."""

    def test_preparation_mode_and_extraction_are_logged_without_text(
        self, env: _Env, capsys, extraction
    ) -> None:
        logs = LogCapture(capsys)
        doc = env.ready_document(_interview(4), filename="秘密の面談.md")
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        (prepared,) = logs.events("advanced_generation.document_prepared")
        assert prepared["document_index"] == 0 and prepared["format"] == "md"
        assert prepared["chars"] > 0 and prepared["chunks"] >= 2
        assert prepared["no_text_pages"] == 0
        assert isinstance(prepared["elapsed_ms"], int)
        (mode,) = logs.events("advanced_generation.document_mode")
        assert mode["mode"] == "extraction" and mode["chunks"] == prepared["chunks"]
        (extracted,) = logs.events("advanced_generation.evidence_extracted")
        assert extracted["chunks"] == prepared["chunks"]
        assert extracted["evidence_input_tokens"] == 100 * prepared["chunks"]
        assert extracted["evidence_output_tokens"] == 10 * prepared["chunks"]
        assert extracted["discarded_unverified"] == 0
        assert_not_leaked("秘密", logs.records())
        assert_not_leaked("家族と過ごす", logs.records())
        assert env.generations.get(generation_id).status is GenerationStatus.DONE


# =============================================================================
# U5: the generation's controls (BR29〜BR33)
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


class TestPlanConfirmation:
    """Step 10: BR29.1〜BR29.7, NFR1.24, NFR5.41, NFR5.42, AC8.1.1〜8.1.4."""

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
        # BR29.6, NFR5.43: created_at + 75 minutes + waited (0) + 15 minutes
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
        # BR29.6: every card extends from the deadline, never shrinking it
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


class _FlakyStore(AdvancedGenerationStore):
    """The real store whose watch reads or heartbeats fail on demand (NFR5.51)."""

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


class TestCancel:
    """Step 12: BR31.1〜BR31.5, NFR1.23, NFR5.46, NFR5.51, AC9.3.1, AC9.3.2."""

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
        env.clock.advance(minutes=5, seconds=1)  # BR31.3: no heartbeat for 5 min

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


def _left_alone(env: _Env, generation_id: str, *, session_id: str = "s-lost") -> None:
    """A started generation whose task went away (no heartbeat, a session)."""
    env.generations.transition(
        generation_id,
        GenerationStatus.PREPARING,
        GenerationStatus.PREPARING,
        updates={"session_id": session_id},
    )


class TestReaderJudgement:
    """Step 14: BR30.1, BR32.1〜BR32.5, NFR5.44〜NFR5.47, AC9.2.1, AC9.4.1."""

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
        _start(manager, file)  # BR32.5: the slot went back


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


class TestControlLogs:
    """Step 17: NFR3.29, NFR3.30, NFR6.16〜NFR6.18."""

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


class _UnreachableSandbox(_Sandbox):
    """Code Interpreter that cannot be reached (BR33.2)."""

    def start_session(self, generation_id: str, lifetime_seconds: int):
        from src.services.sandbox_service import SandboxUnavailableError

        raise SandboxUnavailableError(
            "StartCodeInterpreterSession failed: ConnectTimeoutError at endpoint-x",
            context={"cause": "code_interpreter"},
        )


class TestSandboxUnreachable:
    """Step 16: BR33.1, BR33.2, NFR5.49, AC9.5.1, AC9.5.2."""

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


# --- U5 review fixes ----------------------------------------------------------


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
        # the same file starts again at once (no 409 after 失敗しました)
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


#: The watch never reads: only the check right before the results can see it.
_NO_WATCH = {"control_poll_seconds": 3600}


class TestNothingKeptAfterALateEnd:
    """Review fix 2: BR31.2 holds after the last checkpoint too."""

    def _stall(self, env: _Env, manager, generation_id: str) -> None:
        env.clock.advance(minutes=6)  # no heartbeat: a reader ends it (BR32.1)
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


class TestDataReading:
    """U6 addendum: the plan card shows how the tables read, and the user may fix it."""

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


# ---------------------------------------------------------------------------
# U6: the AI detailed analysis and several tables (BR35〜BR38)
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


class TestDetailedAnalysisChoice:
    """BR35.2, NFR5.58: decided at the start and kept on the generation."""

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


class _NoParquetHashSandbox(_CodeSandbox):
    """The preparation reports no ``parquet_sha256`` (review-01 R-01)."""

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
    """The session holds no library hash to compare with (review-01 R-01)."""

    @property
    def library_sha256(self) -> str:
        return ""


class _FailingCheckSandbox(_CodeSandbox):
    """The isolated check itself fails for one function (review-01 R-03)."""

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


class TestDetailedAnalysisFailsClosed:
    """review-01 R-01 / R-03: no hash to compare and a failed check."""

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
        # review-01 R-04: the analysis kept to source_1, so only the plan is kept
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


class TestMultiStepPlan:
    """U6 addendum: the plan as steps, the candidates and the playbooks read."""

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
