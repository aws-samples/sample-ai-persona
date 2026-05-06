"""
JobManager のユニットテスト（DynamoDB モック版）
"""

import json
import time
from unittest.mock import MagicMock, patch

from src.managers.job_manager import JobManager, JobStatus


def _make_manager() -> tuple[JobManager, MagicMock]:
    """DynamoDB clientをモックしたJobManagerを作成"""
    with patch("src.managers.job_manager.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        manager = JobManager()
    # 実際のテストではclientを差し替え
    manager._client = mock_client
    return manager, mock_client


class TestJobManagerSubmit:
    def test_submit_returns_job_id(self):
        jm, mock_client = _make_manager()
        job_id = jm.submit(lambda: "result")
        assert isinstance(job_id, str)
        assert len(job_id) > 0
        mock_client.put_item.assert_called_once()

    def test_submit_writes_pending_status(self):
        jm, mock_client = _make_manager()
        jm.submit(lambda: "result")
        call_args = mock_client.put_item.call_args
        item = call_args[1]["Item"]
        assert item["status"]["S"] == "pending"


class TestJobManagerGet:
    def test_get_completed_job(self):
        jm, mock_client = _make_manager()
        mock_client.get_item.return_value = {
            "Item": {
                "id": {"S": "job-1"},
                "status": {"S": "completed"},
                "created_at": {"S": "2026-05-05T10:00:00"},
                "updated_at": {"S": "2026-05-05T10:01:00"},
                "result": {"S": json.dumps({"key": "value"})},
            }
        }
        job = jm.get("job-1")
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.result == {"key": "value"}
        assert job.error is None

    def test_get_failed_job(self):
        jm, mock_client = _make_manager()
        mock_client.get_item.return_value = {
            "Item": {
                "id": {"S": "job-2"},
                "status": {"S": "failed"},
                "created_at": {"S": "2026-05-05T10:00:00"},
                "updated_at": {"S": "2026-05-05T10:01:00"},
                "error": {"S": "something went wrong"},
            }
        }
        job = jm.get("job-2")
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error == "something went wrong"
        assert job.result is None

    def test_get_nonexistent_job(self):
        jm, mock_client = _make_manager()
        mock_client.get_item.return_value = {}
        assert jm.get("nonexistent") is None


class TestJobManagerRun:
    def test_completed_job_updates_dynamodb(self):
        jm, mock_client = _make_manager()
        # submit 後にスレッドが完了するのを待つ
        jm.submit(lambda: {"data": "ok"})
        time.sleep(0.2)

        # update_item が2回呼ばれる（running → completed）
        calls = mock_client.update_item.call_args_list
        assert len(calls) == 2
        # 1回目: running
        assert ":s" in calls[0][1]["ExpressionAttributeValues"]
        assert calls[0][1]["ExpressionAttributeValues"][":s"]["S"] == "running"
        # 2回目: completed + result
        assert calls[1][1]["ExpressionAttributeValues"][":s"]["S"] == "completed"
        assert ":r" in calls[1][1]["ExpressionAttributeValues"]

    def test_failed_job_updates_dynamodb(self):
        def failing():
            raise ValueError("test error")

        jm, mock_client = _make_manager()
        jm.submit(failing)
        time.sleep(0.2)

        calls = mock_client.update_item.call_args_list
        assert len(calls) == 2
        assert calls[1][1]["ExpressionAttributeValues"][":s"]["S"] == "failed"
        assert "test error" in calls[1][1]["ExpressionAttributeValues"][":e"]["S"]
