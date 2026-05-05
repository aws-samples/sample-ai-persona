"""
REST API エンドポイント（/api/...）のテスト
"""

from unittest.mock import Mock, patch
from datetime import datetime

from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.models.insight import Insight
from src.managers.job_manager import Job, JobStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_persona(pid="p1", name="テスト太郎"):
    return Persona.create_new(
        name=name, age=30, occupation="エンジニア",
        background="テスト用", values=["効率"], pain_points=["時間"], goals=["成長"],
    )


def _make_discussion():
    d = Discussion.create_new(topic="テスト議論", participants=["p1", "p2"])
    m = Message.create_new(persona_id="p1", persona_name="太郎", content="こんにちは")
    m2 = Message.create_new(persona_id="p2", persona_name="花子", content="はい")
    i = Insight.create_new(
        category="テスト", description="テストインサイト",
        supporting_messages=["msg1"], confidence_score=0.8,
    )
    from dataclasses import replace
    d = replace(d, messages=[m, m2], insights=[i])
    return d


# ---------------------------------------------------------------------------
# generate_personas (async job)
# ---------------------------------------------------------------------------

class TestGeneratePersonas:
    @patch("web.routers.api.get_job_manager")
    def test_returns_202_with_job_id(self, mock_get_jm, client):
        mock_jm = Mock()
        mock_jm.submit.return_value = "job-123"
        mock_get_jm.return_value = mock_jm

        response = client.post("/api/personas/generate", json={
            "data_type": "interview",
            "file_contents": ["テストインタビュー内容"],
            "count": 1,
        })

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "job-123"
        assert data["status"] == "pending"


# ---------------------------------------------------------------------------
# run_discussion (async job)
# ---------------------------------------------------------------------------

class TestRunDiscussion:
    @patch("web.routers.api.get_job_manager")
    @patch("web.routers.api.get_persona_manager")
    def test_returns_202_with_job_id(self, mock_get_pm, mock_get_jm, client):
        mock_pm = Mock()
        mock_pm.get_persona.return_value = _make_persona()
        mock_get_pm.return_value = mock_pm

        mock_jm = Mock()
        mock_jm.submit.return_value = "job-456"
        mock_get_jm.return_value = mock_jm

        response = client.post("/api/discussions", json={
            "persona_ids": ["p1", "p2"],
            "topic": "テスト議論",
        })

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "job-456"

    @patch("web.routers.api.get_persona_manager")
    def test_persona_not_found(self, mock_get_pm, client):
        mock_pm = Mock()
        mock_pm.get_persona.return_value = None
        mock_get_pm.return_value = mock_pm

        response = client.post("/api/discussions", json={
            "persona_ids": ["missing1", "missing2"],
            "topic": "テスト",
        })

        assert response.status_code == 404

    def test_invalid_mode(self, client):
        """persona_idsが1つだけの場合はバリデーションエラー"""
        response = client.post("/api/discussions", json={
            "persona_ids": ["p1"],
            "topic": "テスト",
        })
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# get_discussion
# ---------------------------------------------------------------------------

class TestGetDiscussion:
    @patch("web.routers.api.get_discussion_manager")
    def test_returns_discussion_detail(self, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.get_discussion.return_value = _make_discussion()
        mock_get_dm.return_value = mock_dm

        response = client.get("/api/discussions/disc-123")

        assert response.status_code == 200
        data = response.json()
        assert data["topic"] == "テスト議論"
        assert len(data["messages"]) == 2
        assert len(data["insights"]) == 1

    @patch("web.routers.api.get_discussion_manager")
    def test_not_found(self, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.get_discussion.return_value = None
        mock_get_dm.return_value = mock_dm

        response = client.get("/api/discussions/nonexistent")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# generate_insights
# ---------------------------------------------------------------------------

class TestGenerateInsights:
    @patch("web.routers.api.get_discussion_manager")
    def test_returns_insights(self, mock_get_dm, client):
        discussion = _make_discussion()
        insight = Insight.create_new(
            category="新カテゴリ", description="新インサイト",
            supporting_messages=["msg"], confidence_score=0.9,
        )
        mock_dm = Mock()
        mock_dm.get_discussion.return_value = discussion
        mock_dm.generate_insights.return_value = [insight]
        mock_get_dm.return_value = mock_dm

        response = client.post("/api/discussions/disc-123/insights", json={})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["category"] == "新カテゴリ"

    @patch("web.routers.api.get_discussion_manager")
    def test_discussion_not_found(self, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.get_discussion.return_value = None
        mock_get_dm.return_value = mock_dm

        response = client.post("/api/discussions/missing/insights", json={})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# run_interview
# ---------------------------------------------------------------------------

class TestRunInterview:
    @patch("web.routers.api.get_interview_manager")
    @patch("web.routers.api.get_persona_manager")
    def test_returns_messages(self, mock_get_pm, mock_get_im, client):
        persona = _make_persona()
        mock_pm = Mock()
        mock_pm.get_persona.return_value = persona
        mock_get_pm.return_value = mock_pm

        session = Mock()
        session.id = "sess-1"
        response_msg = Message.create_new(
            persona_id="p1", persona_name="テスト太郎", content="回答です"
        )
        mock_im = Mock()
        mock_im.start_interview_session.return_value = session
        mock_im.send_user_message.return_value = [response_msg]
        mock_get_im.return_value = mock_im

        response = client.post("/api/interviews", json={
            "persona_ids": ["p1"],
            "question": "テスト質問",
        })

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["content"] == "回答です"
        mock_im.end_interview_session.assert_called_once_with("sess-1")


# ---------------------------------------------------------------------------
# job status
# ---------------------------------------------------------------------------

class TestGetJob:
    @patch("web.routers.api.get_job_manager")
    def test_completed_job(self, mock_get_jm, client):
        job = Job(
            id="job-1", status=JobStatus.COMPLETED,
            created_at=datetime.now(), result={"data": "ok"},
        )
        mock_jm = Mock()
        mock_jm.get.return_value = job
        mock_get_jm.return_value = mock_jm

        response = client.get("/api/jobs/job-1")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["result"] == {"data": "ok"}

    @patch("web.routers.api.get_job_manager")
    def test_not_found(self, mock_get_jm, client):
        mock_jm = Mock()
        mock_jm.get.return_value = None
        mock_get_jm.return_value = mock_jm

        response = client.get("/api/jobs/nonexistent")
        assert response.status_code == 404
