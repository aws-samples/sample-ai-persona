"""``GET /api/jobs/{job_id}`` の認可（US4.4 AC4.4.3、BR2.12、NFR1.5、NFR9.22）。

本物の JobManager に Service のモックを注入し、member A が投入したジョブを member B が
開くと 404（存在しない ID と同じ本文）、A 本人には結果を返すことを確かめる。
"""

from datetime import datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.managers.job_manager import JobManager
from src.models.errors import ErrorCode
from src.models.job import Job, JobStatus
from src.services.authz_lookup_service import AuthzLookupError
from web.routers import api


def _job(owner_sub: str | None) -> Job:
    return Job(
        id="job-1",
        status=JobStatus.COMPLETED,
        created_at=datetime(2026, 10, 12, 10, 0),
        updated_at=datetime(2026, 10, 12, 10, 1),
        result={"secret": "member A の結果"},
        owner_sub=owner_sub,
    )


@pytest.fixture
def lookup(monkeypatch) -> Mock:
    service = Mock()
    service.get_job.return_value = None
    monkeypatch.setattr(
        api,
        "_job_manager",
        JobManager(database_service=Mock(), lookup_service=service),
    )
    return service


@pytest.fixture
def browser(test_app):
    with TestClient(test_app, raise_server_exceptions=False) as c:
        yield c


class TestJobOwner:
    def test_the_submitter_gets_the_result(self, browser, lookup, member_a):
        lookup.get_job.return_value = _job(member_a.sub)
        member_a.login(browser)

        response = browser.get("/api/jobs/job-1")

        assert response.status_code == 200
        assert response.json()["result"] == {"secret": "member A の結果"}

    def test_someone_else_gets_404(self, browser, lookup, member_a, member_b):
        lookup.get_job.return_value = _job(member_a.sub)
        member_b.login(browser)

        response = browser.get("/api/jobs/job-1")

        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.JOB_NOT_FOUND.value
        assert "member A の結果" not in response.text

    def test_someone_elses_job_looks_like_a_missing_one(
        self, browser, lookup, member_a, member_b
    ):
        member_b.login(browser)
        lookup.get_job.return_value = _job(member_a.sub)
        others = browser.get("/api/jobs/job-1")
        lookup.get_job.return_value = None
        missing = browser.get("/api/jobs/job-1")

        assert others.status_code == missing.status_code == 404
        assert others.json() == missing.json()

    def test_a_job_without_submitter_is_returned_to_nobody(
        self, browser, lookup, admin_user
    ):
        lookup.get_job.return_value = _job(None)
        admin_user.login(browser)
        assert browser.get("/api/jobs/job-1").status_code == 404

    def test_unreachable_jobs_table_is_503(self, browser, lookup, member_a):
        lookup.get_job.side_effect = AuthzLookupError("down", reason="unreachable")
        member_a.login(browser)

        response = browser.get("/api/jobs/job-1")

        assert response.status_code == 503
        assert response.json()["error_code"] == ErrorCode.LOOKUP_UNAVAILABLE.value
