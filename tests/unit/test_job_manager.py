"""
JobManager のユニットテスト（DatabaseService モック版）
"""

import json
import time
from unittest.mock import Mock

from src.managers.job_manager import JobManager, JobStatus


def _make_manager() -> tuple[JobManager, Mock]:
    """DatabaseServiceをモックしたJobManagerを作成"""
    mock_db = Mock()
    mock_db.get_job.return_value = None
    manager = JobManager(database_service=mock_db)
    return manager, mock_db


class TestJobManagerSubmit:
    def test_submit_returns_job_id(self):
        jm, mock_db = _make_manager()
        job_id = jm.submit(lambda: "result")
        assert isinstance(job_id, str)
        assert len(job_id) > 0
        mock_db.save_job.assert_called_once()

    def test_submit_saves_pending_status(self):
        jm, mock_db = _make_manager()
        jm.submit(lambda: "result")
        call_kwargs = mock_db.save_job.call_args[1]
        assert call_kwargs["status"] == "pending"


class TestJobManagerGet:
    def test_get_completed_job(self):
        jm, mock_db = _make_manager()
        mock_db.get_job.return_value = {
            "id": {"S": "job-1"},
            "status": {"S": "completed"},
            "created_at": {"S": "2026-05-05T10:00:00"},
            "updated_at": {"S": "2026-05-05T10:01:00"},
            "result": {"S": json.dumps({"key": "value"})},
        }
        job = jm.get("job-1")
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.result == {"key": "value"}
        assert job.error is None

    def test_get_failed_job(self):
        jm, mock_db = _make_manager()
        mock_db.get_job.return_value = {
            "id": {"S": "job-2"},
            "status": {"S": "failed"},
            "created_at": {"S": "2026-05-05T10:00:00"},
            "updated_at": {"S": "2026-05-05T10:01:00"},
            "error": {"S": "something went wrong"},
        }
        job = jm.get("job-2")
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error == "something went wrong"
        assert job.result is None

    def test_get_nonexistent_job(self):
        jm, mock_db = _make_manager()
        assert jm.get("nonexistent") is None


class TestJobManagerRun:
    def test_completed_job_calls_update_completed(self):
        jm, mock_db = _make_manager()
        jm.submit(lambda: {"data": "ok"})
        time.sleep(0.2)

        mock_db.update_job_status.assert_called_once()
        assert mock_db.update_job_status.call_args[0][1] == "running"
        mock_db.update_job_completed.assert_called_once()
        result_json = mock_db.update_job_completed.call_args[0][1]
        assert json.loads(result_json) == {"data": "ok"}

    def test_failed_job_calls_update_failed(self):
        def failing():
            raise ValueError("test error")

        jm, mock_db = _make_manager()
        jm.submit(failing)
        time.sleep(0.2)

        mock_db.update_job_status.assert_called_once()
        mock_db.update_job_failed.assert_called_once()
        assert "test error" in mock_db.update_job_failed.call_args[0][1]
