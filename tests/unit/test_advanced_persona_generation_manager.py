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
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest.mock import Mock

import boto3
import pytest

moto = pytest.importorskip("moto")

from src.config import config  # noqa: E402
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
from src.services.s3_service import S3Service  # noqa: E402
from src.services.sandbox_service import InProcessSandbox, SandboxSession  # noqa: E402
from src.services.service_factory import service_factory  # noqa: E402
from src.services.stores.advanced_generation_store import (  # noqa: E402
    AdvancedGenerationStore,
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
        self.cancelled: list[str] = []

    def poll_command(self, session, command_id):
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
        return super().call(session, function, args)

    def stop_session(self, session: SandboxSession) -> None:
        self.stopped.append(session.session_id)
        super().stop_session(session)
        if self.stop_error is not None:
            raise self.stop_error


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

    def create_advanced_planning_agent(self, instructions: str) -> str:
        return "planner"

    def create_advanced_analysis_agent(self, instructions: str, tools: list) -> str:
        self.tools = {t.tool_name: t for t in tools}
        return "analyst"

    def create_advanced_synthesis_agent(self, instructions: str) -> str:
        return "synthesizer"

    def run_advanced_planning(self, agent, prompt, data):
        self.calls.append("planning")
        self.data_seen.append(data)
        if self.fail_planning:
            raise AgentServiceError(
                "advanced planning agent failed (ThrottlingException)",
                code=ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                context={"cause": "model"},
            )
        return dict(self.plan)

    def run_advanced_analysis(self, agent, prompt, data, *, source_id):
        self.calls.append("analysis")
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

    def run_advanced_synthesis(self, agent, prompt, data, output_schema):
        self.calls.append("synthesis")
        self.data_seen.append(data)
        personas = []
        for i, segment in enumerate(self._segments):
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
                    "evidence_ids": list(segment["evidence_ids"]),
                }
            )
        return output_schema(personas=personas)


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
        service = service_factory.create_analysis_tools(session, policy, **kwargs)
        self.holder["service"] = service
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

    def test_two_tables_are_refused(self, env: _Env) -> None:
        a, b = env.ready_file(), env.ready_file()

        with raises_code(AdvancedGenerationError, ErrorCode.UPLOAD_SOURCE_INVALID):
            env.manager().start_generation(
                ADMIN,
                source_file_ids=[a.source_file_id, b.source_file_id],
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
