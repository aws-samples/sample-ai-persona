"""Job モデル（約束 C14）の単体テスト。v1.0.0 で投入者（owner_sub）を持つ。"""

from datetime import datetime

from src.models.job import Job, JobStatus

_CREATED = datetime(2026, 10, 12, 10, 0)
_UPDATED = datetime(2026, 10, 12, 10, 1)


def _job(**overrides: object) -> Job:
    values: dict = {
        "id": "job-1",
        "status": JobStatus.PENDING,
        "created_at": _CREATED,
        "updated_at": _UPDATED,
    }
    values.update(overrides)
    return Job(**values)


class TestOwner:
    def test_job_records_its_submitter(self):
        job = _job(owner_sub="user-1", job_type="persona_generation")
        assert job.owner_sub == "user-1"
        assert job.job_type == "persona_generation"

    def test_owner_defaults_to_none_for_old_jobs(self):
        assert _job().owner_sub is None


class TestToDict:
    def test_round_trip(self):
        job = _job(
            status=JobStatus.COMPLETED,
            owner_sub="user-1",
            job_type="discussion",
            result={"key": "value"},
        )
        assert Job.from_dict(job.to_dict()) == job

    def test_none_fields_are_left_out(self):
        data = _job().to_dict()
        for name in ("owner_sub", "job_type", "result", "error"):
            assert name not in data

    def test_values_are_plain(self):
        data = _job(owner_sub="user-1").to_dict()
        assert data["status"] == "pending"
        assert data["created_at"] == _CREATED.isoformat()
        assert data["owner_sub"] == "user-1"


class TestFromDict:
    def test_missing_owner_becomes_none(self):
        job = Job.from_dict(
            {
                "id": "job-1",
                "status": "running",
                "created_at": _CREATED.isoformat(),
                "updated_at": _UPDATED.isoformat(),
            }
        )
        assert job.owner_sub is None
        assert job.status is JobStatus.RUNNING

    def test_empty_owner_becomes_none(self):
        job = Job.from_dict(
            {
                "id": "job-1",
                "status": "pending",
                "created_at": _CREATED.isoformat(),
                "updated_at": _UPDATED.isoformat(),
                "owner_sub": "",
            }
        )
        assert job.owner_sub is None
