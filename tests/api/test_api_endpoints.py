"""
REST API エンドポイント（/api/...）のテスト

U6: 議論・インタビューの API は利用者を Manager に渡し、判定は本物の Manager・
Component（tests/api/u6_world.py）で確かめる。約束 C12（判定の順序）、BR2.1、
BR2.4、BR2.5、BR2.8、BR3.1、BR8.3、NFR1.43、NFR1.46、NFR3.12。
"""

from contextlib import ExitStack
from datetime import datetime
from typing import Any
from unittest.mock import Mock, patch

import pytest

from src.managers.job_manager import Job, JobStatus
from src.models.errors import ErrorCode
from tests.api.u6_world import ME, PEER, STRANGER, ApiWorld


class _InlineJobs:
    """A JobManager that runs the job at once and keeps the result."""

    def __init__(self) -> None:
        self.results: dict[str, Any] = {}
        self.errors: dict[str, Exception] = {}

    def submit(self, principal, job_type, func, *args, **kwargs) -> str:
        job_id = f"job-{len(self.results) + len(self.errors) + 1}"
        try:
            self.results[job_id] = func(principal, *args, **kwargs)
        except Exception as e:  # the job's failure is kept, not raised
            self.errors[job_id] = e
        return job_id


@pytest.fixture
def aw(client):
    ME.login(client)
    world = ApiWorld()
    jobs = _InlineJobs()
    world.jobs = jobs  # type: ignore[attr-defined]
    with ExitStack() as stack:
        world.patch_routers(stack)
        stack.enter_context(patch("web.routers.api.get_job_manager", return_value=jobs))
        yield world


# ---------------------------------------------------------------------------
# generate_personas (async job)
# ---------------------------------------------------------------------------


class TestGeneratePersonas:
    @patch("web.routers.api.get_job_manager")
    def test_returns_202_with_job_id(self, mock_get_jm, client):
        mock_get_jm.return_value.submit.return_value = "job-123"

        response = client.post(
            "/api/personas/generate",
            json={
                "data_type": "interview",
                "file_contents": ["テストインタビュー内容"],
                "count": 1,
            },
        )

        assert response.status_code == 202
        assert response.json() == {
            "job_id": "job-123",
            "status": "pending",
            "result": None,
            "error": None,
            "error_code": None,
            "failed_count": None,
            "failed_names": None,
        }


# ---------------------------------------------------------------------------
# /api/discussions（一覧・詳細、BR2.1、BR2.4、NFR1.43）
# ---------------------------------------------------------------------------


class TestListDiscussions:
    def test_mine_and_shared_only(self, aw, client):
        mine = aw.discussion(topic="自分の議論について")
        shared = aw.discussion(PEER, topic="共有された議論", shared_with=("sales",))
        aw.discussion(STRANGER, topic="無関係な議論について")

        data = client.get("/api/discussions").json()

        assert {d["id"] for d in data} == {mine.id, shared.id}
        assert set(data[0]) == {"id", "topic", "mode", "created_at"}

    def test_empty(self, aw, client):
        assert client.get("/api/discussions").json() == []


class TestGetDiscussion:
    def test_reader_gets_messages_and_insights(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        response = client.get(f"/api/discussions/{discussion.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["topic"] == discussion.topic
        assert len(data["messages"]) == 2
        assert data["participants"] == ["p1", "p2"]

    @pytest.mark.parametrize("missing", [False, True])
    def test_unrelated_and_missing_are_the_same_404(self, aw, client, missing):
        target = "nonexistent" if missing else aw.discussion(STRANGER).id
        response = client.get(f"/api/discussions/{target}")
        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.DISCUSSION_NOT_FOUND.value


# ---------------------------------------------------------------------------
# POST /api/discussions（BR2.8、C12 の順）
# ---------------------------------------------------------------------------


class TestRunDiscussion:
    def test_runs_as_the_caller_and_saves(self, aw, client):
        response = client.post(
            "/api/discussions",
            json={
                "persona_ids": ["p1", "p2"],
                "topic": "新商品のマーケティングについて",
            },
        )
        assert response.status_code == 202
        (result,) = aw.jobs.results.values()
        assert aw.world.store.bodies[result["id"]]["owner_sub"] == ME.sub
        assert len(result["messages"]) == 2

    def test_unreadable_persona_is_404_before_the_job(self, aw, client):
        response = client.post(
            "/api/discussions",
            json={
                "persona_ids": ["p1", "p3"],
                "topic": "新商品のマーケティングについて",
            },
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.PERSONA_NOT_FOUND.value
        assert aw.jobs.results == {}
        aw.ai.facilitate_discussion.assert_not_called()

    def test_agent_mode(self, aw, client):
        response = client.post(
            "/api/discussions",
            json={
                "persona_ids": ["p1", "p2"],
                "topic": "新商品のマーケティングについて",
                "mode": "agent",
                "rounds": 1,
            },
        )
        assert response.status_code == 202
        (result,) = aw.jobs.results.values()
        assert result["mode"] == "agent"

    def test_one_persona_is_a_validation_error(self, client):
        response = client.post(
            "/api/discussions", json={"persona_ids": ["p1"], "topic": "テスト"}
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/discussions/{id}/insights（BR2.5）
# ---------------------------------------------------------------------------


class TestGenerateInsights:
    def test_owner_regenerates_and_the_job_saves_them(self, aw, client):
        discussion = aw.discussion()
        response = client.post(f"/api/discussions/{discussion.id}/insights", json={})
        assert response.status_code == 202
        (result,) = aw.jobs.results.values()
        assert result[0]["description"] == "十分に長いインサイトの説明です"
        assert len(aw.world.store.bodies[discussion.id]["insights"]) == 1

    def test_shared_reader_is_403(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        response = client.post(f"/api/discussions/{discussion.id}/insights", json={})
        assert response.status_code == 403
        assert aw.jobs.results == {}

    def test_unrelated_is_404(self, aw, client):
        discussion = aw.discussion(STRANGER)
        response = client.post(f"/api/discussions/{discussion.id}/insights", json={})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/interviews（BR3.1）
# ---------------------------------------------------------------------------


class TestRunInterview:
    @pytest.fixture
    def interviews(self, aw, monkeypatch):
        moto = pytest.importorskip("moto")
        import boto3

        from src.managers.interview_manager import InterviewManager
        from src.services.stores.interview_session_store import InterviewSessionStore
        from tests.api.test_interview_router import MESSAGES, REGION, SESSIONS, _tables

        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        with moto.mock_aws():
            ddb = boto3.client("dynamodb", region_name=REGION)
            _tables(ddb)
            store = InterviewSessionStore(
                region=REGION,
                sessions_table=SESSIONS,
                messages_table=MESSAGES,
                client=ddb,
            )
            w = aw.world
            manager = InterviewManager(
                agent_service=aw.agent_service,
                dataset_store=Mock(),
                dataset_analysis_service=Mock(),
                binding_store=Mock(list_for_persona=Mock(return_value=[])),
                session_store=store,
                discussion_records=w.records,
                participants=w.participants,
                file_attachments=w.files,
                resource_lookup=w.lookup,
            )
            with patch("web.routers.api.get_interview_manager", return_value=manager):
                yield store

    def test_one_turn_and_the_session_is_ended(self, aw, client, interviews):
        response = client.post(
            "/api/interviews", json={"persona_ids": ["p1"], "question": "質問です"}
        )
        assert response.status_code == 200
        data = response.json()
        assert [m["persona_id"] for m in data] == ["p1"]
        sessions = interviews.client.scan(TableName="InterviewSessionsApi")["Items"]
        assert sessions == []

    def test_unreadable_persona_is_404(self, aw, client, interviews):
        response = client.post(
            "/api/interviews", json={"persona_ids": ["p3"], "question": "質問です"}
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.PERSONA_NOT_FOUND.value


# ---------------------------------------------------------------------------
# job status（BR8.3）
# ---------------------------------------------------------------------------


class TestGetJob:
    @patch("web.routers.api.get_job_manager")
    def test_completed_job(self, mock_get_jm, client):
        mock_get_jm.return_value.get.return_value = Job(
            id="job-1",
            status=JobStatus.COMPLETED,
            created_at=datetime.now(),
            result={"data": "ok"},
        )

        response = client.get("/api/jobs/job-1")

        assert response.status_code == 200
        assert response.json()["result"] == {"data": "ok"}

    @patch("web.routers.api.get_job_manager")
    def test_failed_job_has_the_wording_of_its_code(self, mock_get_jm, client):
        mock_get_jm.return_value.get.return_value = Job(
            id="job-2",
            status=JobStatus.FAILED,
            created_at=datetime.now(),
            error_code=ErrorCode.DISCUSSION_TOO_LARGE.value,
        )

        data = client.get("/api/jobs/job-2").json()

        assert data["status"] == "failed"
        assert data["error"]
        assert data["error_code"] == ErrorCode.DISCUSSION_TOO_LARGE.value

    @patch("web.routers.api.get_job_manager")
    def test_not_found(self, mock_get_jm, client):
        from src.managers.job_manager import JobNotFoundError

        mock_get_jm.return_value.get.side_effect = JobNotFoundError("no such job")

        assert client.get("/api/jobs/nonexistent").status_code == 404
