"""
JobManager のユニットテスト
"""

import time

from src.managers.job_manager import JobManager, JobStatus


class TestJobManager:
    """JobManager のテスト"""

    def test_submit_returns_job_id(self):
        jm = JobManager()
        job_id = jm.submit(lambda: "result")
        assert isinstance(job_id, str)
        assert len(job_id) > 0

    def test_completed_job(self):
        jm = JobManager()
        job_id = jm.submit(lambda: {"key": "value"})
        # ジョブ完了を待つ
        for _ in range(50):
            job = jm.get(job_id)
            if job and job.status == JobStatus.COMPLETED:
                break
            time.sleep(0.05)
        job = jm.get(job_id)
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.result == {"key": "value"}
        assert job.error is None

    def test_failed_job(self):
        def failing():
            raise ValueError("test error")

        jm = JobManager()
        job_id = jm.submit(failing)
        for _ in range(50):
            job = jm.get(job_id)
            if job and job.status == JobStatus.FAILED:
                break
            time.sleep(0.05)
        job = jm.get(job_id)
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error == "処理中にエラーが発生しました"
        assert job.result is None

    def test_get_nonexistent_job(self):
        jm = JobManager()
        assert jm.get("nonexistent") is None

    def test_job_with_args(self):
        jm = JobManager()
        job_id = jm.submit(lambda a, b: a + b, 3, 4)
        for _ in range(50):
            job = jm.get(job_id)
            if job and job.status == JobStatus.COMPLETED:
                break
            time.sleep(0.05)
        job = jm.get(job_id)
        assert job.result == 7
