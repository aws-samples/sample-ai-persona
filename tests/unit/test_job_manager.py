"""
JobManager のユニットテスト（DatabaseService モック版）
"""

import json
import threading
from datetime import datetime
from unittest.mock import Mock

from src.managers.job_manager import JobManager
from src.models.job import Job, JobStatus


def _make_manager() -> tuple[JobManager, Mock, threading.Event]:
    """DatabaseServiceをモックしたJobManagerを作成（完了通知Event付き）"""
    mock_db = Mock()
    mock_db.get_job.return_value = None
    done = threading.Event()

    # update_job_completed / update_job_failed が呼ばれたらEventをセット
    def _on_completed(*args: object) -> None:
        done.set()

    mock_db.update_job_completed.side_effect = _on_completed
    mock_db.update_job_failed.side_effect = _on_completed

    manager = JobManager(database_service=mock_db)
    return manager, mock_db, done


class TestJobManagerSubmit:
    def test_submit_returns_job_id(self):
        jm, mock_db, _ = _make_manager()
        job_id = jm.submit(lambda: "result")
        assert isinstance(job_id, str)
        assert len(job_id) > 0
        mock_db.save_job.assert_called_once()

    def test_submit_saves_pending_status(self):
        jm, mock_db, _ = _make_manager()
        jm.submit(lambda: "result")
        call_kwargs = mock_db.save_job.call_args[1]
        assert call_kwargs["status"] == "pending"


class TestJobManagerGet:
    def test_get_completed_job(self):
        jm, mock_db, _ = _make_manager()
        mock_db.get_job.return_value = Job(
            id="job-1",
            status=JobStatus.COMPLETED,
            created_at=datetime(2026, 5, 5, 10, 0),
            updated_at=datetime(2026, 5, 5, 10, 1),
            result={"key": "value"},
        )
        job = jm.get("job-1")
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.result == {"key": "value"}
        assert job.error is None

    def test_get_failed_job(self):
        jm, mock_db, _ = _make_manager()
        mock_db.get_job.return_value = Job(
            id="job-2",
            status=JobStatus.FAILED,
            created_at=datetime(2026, 5, 5, 10, 0),
            updated_at=datetime(2026, 5, 5, 10, 1),
            error="something went wrong",
        )
        job = jm.get("job-2")
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error == "something went wrong"
        assert job.result is None

    def test_get_nonexistent_job(self):
        jm, mock_db, _ = _make_manager()
        assert jm.get("nonexistent") is None


class TestJobManagerRun:
    def test_completed_job_calls_update_completed(self):
        jm, mock_db, done = _make_manager()
        jm.submit(lambda: {"data": "ok"})
        assert done.wait(timeout=5)

        mock_db.update_job_status.assert_called_once()
        assert mock_db.update_job_status.call_args[0][1] == "running"
        mock_db.update_job_completed.assert_called_once()
        result_json = mock_db.update_job_completed.call_args[0][1]
        assert json.loads(result_json) == {"data": "ok"}

    def test_failed_job_calls_update_failed(self):
        def failing():
            raise ValueError("test error")

        jm, mock_db, done = _make_manager()
        jm.submit(failing)
        assert done.wait(timeout=5)

        mock_db.update_job_status.assert_called_once()
        mock_db.update_job_failed.assert_called_once()
        assert "test error" in mock_db.update_job_failed.call_args[0][1]
