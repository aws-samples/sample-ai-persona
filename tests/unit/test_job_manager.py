"""JobManager の単体テスト（約束 C14、BR2.12）。

投入時に投入者（owner_sub）を記録し、処理の関数に投入者の principal を渡す。結果は
本人にだけ返し、他人・投入者なし・存在しないは同じ JOB_NOT_FOUND（NFR1.5）。Jobs に
届かなければ 503 で結果を返さない（NFR9.22）。照合の結果はキャッシュしない。
DatabaseService（書き込み）と AuthzLookupService（読み取り）はモックを注入する。
"""

import json
import threading
from datetime import datetime
from unittest.mock import Mock

import pytest

from src.managers.job_manager import JobManager
from src.models.authz import Principal, Role
from src.models.errors import (
    AccessDeniedError,
    CodedError,
    ErrorCode,
    LookupUnavailableError,
)
from src.models.job import Job, JobStatus
from src.services.authz_lookup_service import AuthzLookupError
from tests.error_helpers import raises_code

OWNER = Principal(sub="user-a", roles=frozenset({Role.MEMBER}))
OTHER = Principal(sub="user-b", roles=frozenset({Role.MEMBER}))
NO_ROLE = Principal(sub="user-c")


def _make_manager() -> tuple[JobManager, Mock, Mock, threading.Event]:
    """Mocks for both services and an Event set when a job finishes."""
    mock_db = Mock()
    mock_lookup = Mock()
    mock_lookup.get_job.return_value = None
    done = threading.Event()

    def _on_finished(*args: object) -> None:
        done.set()

    mock_db.update_job_completed.side_effect = _on_finished
    mock_db.update_job_failed.side_effect = _on_finished
    manager = JobManager(database_service=mock_db, lookup_service=mock_lookup)
    return manager, mock_db, mock_lookup, done


def _job(owner_sub: str | None = "user-a", **overrides: object) -> Job:
    values: dict = {
        "id": "job-1",
        "status": JobStatus.COMPLETED,
        "created_at": datetime(2026, 10, 12, 10, 0),
        "updated_at": datetime(2026, 10, 12, 10, 1),
        "result": {"key": "value"},
        "owner_sub": owner_sub,
    }
    values.update(overrides)
    return Job(**values)


class TestSubmit:
    def test_records_the_submitter_and_the_kind(self):
        jm, mock_db, _, _ = _make_manager()
        job_id = jm.submit(OWNER, "discussion", lambda principal: "result")

        assert isinstance(job_id, str) and job_id
        kwargs = mock_db.save_job.call_args.kwargs
        assert kwargs["job_id"] == job_id
        assert kwargs["status"] == "pending"
        assert kwargs["owner_sub"] == "user-a"
        assert kwargs["job_type"] == "discussion"

    def test_passes_the_submitter_to_the_work(self):
        jm, mock_db, _, done = _make_manager()
        seen: list[object] = []

        def work(principal: Principal, value: int, *, scale: int) -> dict:
            seen.append(principal)
            return {"value": value * scale}

        jm.submit(OWNER, "discussion", work, 2, scale=3)
        assert done.wait(timeout=5)

        assert seen == [OWNER]
        assert json.loads(mock_db.update_job_completed.call_args[0][1]) == {"value": 6}

    def test_needs_the_ai_permission(self):
        jm, mock_db, _, _ = _make_manager()
        with raises_code(AccessDeniedError, ErrorCode.ACCESS_DENIED):
            jm.submit(NO_ROLE, "discussion", lambda principal: None)
        mock_db.save_job.assert_not_called()


class TestGet:
    def test_owner_gets_the_result(self):
        jm, _, mock_lookup, _ = _make_manager()
        mock_lookup.get_job.return_value = _job()

        job = jm.get(OWNER, "job-1")

        assert job.result == {"key": "value"}
        mock_lookup.get_job.assert_called_once_with("job-1")

    @pytest.mark.parametrize(
        "stored",
        [_job(owner_sub="user-a"), _job(owner_sub=None), None],
        ids=["someone-else", "no-submitter", "missing"],
    )
    def test_not_the_owner_is_job_not_found(self, stored):
        jm, _, mock_lookup, _ = _make_manager()
        mock_lookup.get_job.return_value = stored
        with raises_code(CodedError, ErrorCode.JOB_NOT_FOUND):
            jm.get(OTHER, "job-1")

    def test_all_not_found_cases_look_the_same(self):
        errors = []
        for stored in (_job(owner_sub="user-a"), _job(owner_sub=None), None):
            jm, _, mock_lookup, _ = _make_manager()
            mock_lookup.get_job.return_value = stored
            with pytest.raises(CodedError) as info:
                jm.get(OTHER, "job-1")
            errors.append(info.value)
        assert len({type(e) for e in errors}) == 1
        assert len({str(e) for e in errors}) == 1

    def test_unreachable_jobs_table_is_503(self):
        jm, _, mock_lookup, _ = _make_manager()
        mock_lookup.get_job.side_effect = AuthzLookupError(
            "jobs table down", reason="unreachable"
        )
        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            jm.get(OWNER, "job-1")

    def test_every_request_reads_again(self):
        jm, _, mock_lookup, _ = _make_manager()
        mock_lookup.get_job.return_value = _job()
        jm.get(OWNER, "job-1")
        mock_lookup.get_job.return_value = _job(owner_sub="user-z")
        with raises_code(CodedError, ErrorCode.JOB_NOT_FOUND):
            jm.get(OWNER, "job-1")
        assert mock_lookup.get_job.call_count == 2


class TestAnotherTask:
    def test_a_job_is_readable_from_another_manager_instance(self):
        """投入した台と別の台（別の JobManager）からも本人は結果を取れる（NFR9.16）。

        JobManager は台のメモリに結果を持たず、Jobs テーブルだけを読む。共有の
        テーブルを辞書で置き換えて確かめる。
        """
        table: dict[str, Job] = {}
        done = threading.Event()

        db = Mock()

        def save_job(*, job_id, status, expires_at, owner_sub, job_type):
            table[job_id] = _job(
                owner_sub=owner_sub, id=job_id, status=JobStatus(status)
            )

        def completed(job_id, result_json):
            table[job_id] = _job(
                owner_sub=table[job_id].owner_sub,
                id=job_id,
                result=json.loads(result_json),
            )
            done.set()

        db.save_job.side_effect = save_job
        db.update_job_completed.side_effect = completed
        lookup = Mock()
        lookup.get_job.side_effect = table.get

        submitting = JobManager(database_service=db, lookup_service=lookup)
        reading = JobManager(database_service=Mock(), lookup_service=lookup)
        job_id = submitting.submit(OWNER, "discussion", lambda principal: {"n": 1})
        assert done.wait(timeout=5)

        assert reading.get(OWNER, job_id).result == {"n": 1}
        with raises_code(CodedError, ErrorCode.JOB_NOT_FOUND):
            reading.get(OTHER, job_id)


class TestRun:
    def test_completed_job_calls_update_completed(self):
        jm, mock_db, _, done = _make_manager()
        jm.submit(OWNER, "discussion", lambda principal: {"data": "ok"})
        assert done.wait(timeout=5)

        mock_db.update_job_status.assert_called_once()
        assert mock_db.update_job_status.call_args[0][1] == "running"
        mock_db.update_job_completed.assert_called_once()
        result_json = mock_db.update_job_completed.call_args[0][1]
        assert json.loads(result_json) == {"data": "ok"}

    def test_failed_job_calls_update_failed(self):
        def failing(principal: Principal) -> None:
            raise ValueError("test error")

        jm, mock_db, _, done = _make_manager()
        jm.submit(OWNER, "discussion", failing)
        assert done.wait(timeout=5)

        mock_db.update_job_status.assert_called_once()
        mock_db.update_job_failed.assert_called_once()
