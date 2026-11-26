"""Tests for the REST API router (api.py).

Covers the /api/personas, /api/discussions and /api/health endpoints.
"""

from unittest.mock import Mock, patch

import pytest


class TestHealthEndpoint:
    """Health check endpoint tests."""

    def test_health_check_returns_ok(self, client):
        """The health check responds normally."""
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestPersonasAPIEndpoint:
    """Persona list API endpoint tests."""

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_empty(self, mock_get_manager, client):
        """Returns an empty list when no personas exist."""
        mock_manager = Mock()
        mock_manager.list_accessible_personas.return_value = []
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 200
        assert response.json() == []

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_with_data(self, mock_get_manager, client, sample_persona):
        """Returns the correct data when personas exist."""
        mock_manager = Mock()
        mock_manager.list_accessible_personas.return_value = [sample_persona]
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "田中花子"
        assert data[0]["age"] == 35
        assert data[0]["occupation"] == "マーケティング担当"
        assert "values" in data[0]
        assert "pain_points" in data[0]
        assert "goals" in data[0]

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_with_search(
        self, mock_get_manager, client, sample_persona, sample_persona_2
    ):
        """Passes the search term to the Manager with the user (filtering is the Manager's job)."""
        mock_manager = Mock()
        mock_manager.list_accessible_personas.return_value = [sample_persona]
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas?search=田中")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "田中花子"
        principal, search = mock_manager.list_accessible_personas.call_args.args
        assert search == "田中" and principal.sub

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_search_by_occupation(
        self, mock_get_manager, client, sample_persona, sample_persona_2
    ):
        """Passes None when there is no search term."""
        mock_manager = Mock()
        mock_manager.list_accessible_personas.return_value = [
            sample_persona,
            sample_persona_2,
        ]
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 200
        assert len(response.json()) == 2
        _principal, search = mock_manager.list_accessible_personas.call_args.args
        assert search is None

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_error_handling(self, mock_get_manager, client):
        """Returns 500 on error."""
        mock_manager = Mock()
        mock_manager.list_accessible_personas.side_effect = Exception("Database error")
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 500
        assert "ペルソナ一覧の取得に失敗しました" in response.json()["detail"]

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_keeps_the_lookup_outage_code(self, mock_get_manager, client):
        """An unreachable lookup stays 503 lookup_unavailable (common handler)."""
        from src.models.errors import LookupUnavailableError

        mock_manager = Mock()
        mock_manager.list_accessible_personas.side_effect = LookupUnavailableError(
            "index unreachable"
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 503
        assert response.json()["error_code"] == "lookup_unavailable"


class TestPersonaDetailAPIEndpoint:
    """Persona detail API endpoint tests."""

    @patch("web.routers.api.get_persona_manager")
    def test_get_persona_success(self, mock_get_manager, client, sample_persona):
        """The persona detail is fetched normally."""
        from src.managers.persona_manager import PersonaDetail

        mock_manager = Mock()
        mock_manager.get_persona_detail.return_value = PersonaDetail(
            sample_persona, manageable=True
        )
        mock_get_manager.return_value = mock_manager

        response = client.get(f"/api/personas/{sample_persona.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_persona.id
        assert data["name"] == "田中花子"
        assert data["age"] == 35

    @patch("web.routers.api.get_persona_manager")
    def test_get_persona_not_found(self, mock_get_manager, client):
        """Returns 404 for an unreadable or nonexistent persona ID (same response for both)."""
        from src.models.errors import CodedError, ErrorCode

        mock_manager = Mock()
        mock_manager.get_persona_detail.side_effect = CodedError(
            "not readable", code=ErrorCode.PERSONA_NOT_FOUND
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas/non-existent-id")

        assert response.status_code == 404
        assert "ペルソナが見つかりません" in response.json()["detail"]
        assert response.json()["error_code"] == "persona_not_found"

    @patch("web.routers.api.get_persona_manager")
    def test_get_persona_error_handling(self, mock_get_manager, client):
        """Returns 500 on error."""
        mock_manager = Mock()
        mock_manager.get_persona_detail.side_effect = Exception("Database error")
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas/test-id")

        assert response.status_code == 500
        assert "ペルソナの取得に失敗しました" in response.json()["detail"]


class TestDiscussionsAPIErrors:
    """Unexpected failures of the discussion API (the checks are in tests/api/test_api_endpoints.py)"""

    @patch("web.routers.api.get_discussion_manager")
    def test_list_discussions_error_handling(self, mock_get_manager, client):
        """An unexpected failure returns 500 without exposing the exception text."""
        mock_get_manager.return_value.list_accessible_discussions.side_effect = (
            Exception("Database error at arn:aws:dynamodb:secret")
        )

        response = client.get("/api/discussions")

        assert response.status_code == 500
        assert "議論一覧の取得に失敗しました" in response.json()["detail"]
        assert "secret" not in response.text

    @patch("web.routers.api.get_discussion_manager")
    def test_get_discussion_error(self, mock_get_manager, client):
        """Returns 500 on error."""
        mock_get_manager.return_value.get_discussion.side_effect = Exception("DB error")

        response = client.get("/api/discussions/test-id")

        assert response.status_code == 500


class TestJobEndpoint:
    """Job status API tests."""

    @patch("web.routers.api.get_job_manager")
    def test_get_job_not_found(self, mock_get_jm, client):
        """Returns 404 for a nonexistent job ID or one owned by someone else."""
        from src.managers.job_manager import JobNotFoundError

        mock_jm = Mock()
        mock_jm.get.side_effect = JobNotFoundError("no such job")
        mock_get_jm.return_value = mock_jm

        response = client.get("/api/jobs/non-existent")

        assert response.status_code == 404
        assert "ジョブが見つかりません" in response.json()["detail"]

    @patch("web.routers.api.get_job_manager")
    def test_get_job_completed(self, mock_get_jm, client):
        """Fetching a completed job."""
        from src.models.job import Job, JobStatus

        job = Job(
            id="job-1",
            status=JobStatus.COMPLETED,
            created_at=Mock(),
            result={"data": "test"},
        )
        mock_jm = Mock()
        mock_jm.get.return_value = job
        mock_get_jm.return_value = mock_jm

        response = client.get("/api/jobs/job-1")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-1"
        assert data["status"] == "completed"
        assert data["result"] == {"data": "test"}
        assert data["error"] is None

    @patch("web.routers.api.get_job_manager")
    def test_generation_job_keeps_result_a_list_and_adds_the_failures(
        self, mock_get_jm, client
    ):
        """``result`` stays the persona list; the failures are added."""
        from src.models.job import Job, JobStatus

        job = Job(
            id="job-3",
            status=JobStatus.COMPLETED,
            created_at=Mock(),
            job_type="persona_generation",
            result={
                "personas": [{"id": "new-1", "name": "田中"}],
                "failed_count": 1,
                "failed_names": ["鈴木"],
            },
        )
        mock_jm = Mock()
        mock_jm.get.return_value = job
        mock_get_jm.return_value = mock_jm

        data = client.get("/api/jobs/job-3").json()

        assert data["result"] == [{"id": "new-1", "name": "田中"}]
        assert data["failed_count"] == 1
        assert data["failed_names"] == ["鈴木"]
        assert data["error"] is None

    @patch("web.routers.api.get_job_manager")
    def test_an_older_generation_job_is_returned_as_stored(self, mock_get_jm, client):
        from src.models.job import Job, JobStatus

        job = Job(
            id="job-4",
            status=JobStatus.COMPLETED,
            created_at=Mock(),
            job_type="persona_generation",
            result=[{"id": "p-1"}],
        )
        mock_jm = Mock()
        mock_jm.get.return_value = job
        mock_get_jm.return_value = mock_jm

        data = client.get("/api/jobs/job-4").json()

        assert data["result"] == [{"id": "p-1"}]
        assert data["failed_count"] is None
        assert data["failed_names"] is None

    @patch("web.routers.api.get_job_manager")
    def test_get_job_failed(self, mock_get_jm, client):
        """Fetching a failed job."""
        from src.models.job import Job, JobStatus

        job = Job(
            id="job-2",
            status=JobStatus.FAILED,
            created_at=Mock(),
            error_code="unknown",
        )
        mock_jm = Mock()
        mock_jm.get.return_value = job
        mock_get_jm.return_value = mock_jm

        response = client.get("/api/jobs/job-2")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        # Failures return a code; the message is the catalog text, not the exception text
        assert data["error_code"] == "unknown"
        assert data["error"]


class TestGeneratePersonasEndpoint:
    """Persona generation API tests."""

    @patch("web.routers.api.get_job_manager")
    def test_generate_personas_error(self, mock_get_jm, client):
        """Error when submitting a persona generation job."""
        mock_jm = Mock()
        mock_jm.submit.side_effect = Exception("job error")
        mock_get_jm.return_value = mock_jm

        response = client.post(
            "/api/personas/generate",
            json={"data_type": "interview", "file_contents": ["data"], "count": 1},
        )

        assert response.status_code == 500


class TestGenerateJobSavesOwnCandidatesU5:
    """The API generation job also goes through claim, save and cleanup."""

    def _run(self, gen, pm):
        from tests.conftest import MEMBER_A
        from web.routers import api

        with (
            patch("web.routers.api.get_persona_generation_manager", return_value=gen),
            patch("web.routers.api.get_persona_manager", return_value=pm),
        ):
            return api._run_generate_personas(
                MEMBER_A.principal(),
                [(b"data", "input_0.txt")],
                "interview",
                2,
                None,
                None,
            )

    def test_saves_the_generated_candidates_under_new_ids(
        self, sample_persona, sample_persona_2
    ):
        from src.managers.persona_generation_manager import GenerationResult
        from src.models.persona_candidate import CandidateSaveResult
        from tests.conftest import MEMBER_A

        gen = Mock()
        gen.generate.return_value = GenerationResult(
            [sample_persona, sample_persona_2], []
        )
        gen.claim_candidates.return_value = "the-claim"
        pm = Mock()
        result = CandidateSaveResult(
            saved_ids=(sample_persona.id, sample_persona_2.id),
            saved_persona_ids=("new-1", "new-2"),
        )
        pm.save_generated_personas.return_value = result

        body = self._run(gen, pm)

        principal = gen.generate.call_args.args[0]
        assert principal.sub == MEMBER_A.sub
        assert gen.claim_candidates.call_args.args == (
            principal,
            [sample_persona.id, sample_persona_2.id],
        )
        assert pm.save_generated_personas.call_args.args == (principal, "the-claim")
        gen.finish_candidates.assert_called_once_with("the-claim", result)
        # The returned IDs are those of the saved personas, not the candidates
        assert [p["id"] for p in body["personas"]] == ["new-1", "new-2"]
        assert body["personas"][0]["name"] == sample_persona.name
        assert (body["failed_count"], body["failed_names"]) == (0, [])

    def test_unsaved_candidates_are_counted_and_named(
        self, sample_persona, sample_persona_2
    ):
        """A partial bulk save reports what was not saved."""
        from src.managers.persona_generation_manager import GenerationResult
        from src.models.persona_candidate import CandidateSaveResult

        gen = Mock()
        gen.generate.return_value = GenerationResult(
            [sample_persona, sample_persona_2], []
        )
        gen.claim_candidates.return_value = "the-claim"
        pm = Mock()
        pm.save_generated_personas.return_value = CandidateSaveResult(
            saved_ids=(sample_persona.id,),
            failed_ids=(sample_persona_2.id,),
            saved_persona_ids=("new-1",),
            failed_names=(sample_persona_2.name,),
        )

        payload = self._run(gen, pm)

        assert payload["failed_count"] == 1
        assert payload["failed_names"] == [sample_persona_2.name]
        assert [p["id"] for p in payload["personas"]] == ["new-1"]

    def test_finishes_even_when_the_save_fails(self, sample_persona):
        from src.managers.persona_generation_manager import GenerationResult

        gen = Mock()
        gen.generate.return_value = GenerationResult([sample_persona], [])
        gen.claim_candidates.return_value = "the-claim"
        pm = Mock()
        pm.save_generated_personas.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError):
            self._run(gen, pm)

        gen.finish_candidates.assert_called_once_with("the-claim", None)
