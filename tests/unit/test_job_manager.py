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

from src.managers.job_manager import JobManager, JobNotFoundError
from src.models.authz import Principal, Role
from src.models.errors import (
    AccessDeniedError,
    CodedError,
    ErrorCode,
    LookupUnavailableError,
)
from src.models.job import Job, JobStatus
from src.services.authz_lookup_service import AuthzLookupError
from src.services.job_store import JobStore
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture

OWNER = Principal(sub="user-a", roles=frozenset({Role.MEMBER}))
OTHER = Principal(sub="user-b", roles=frozenset({Role.MEMBER}))
NO_ROLE = Principal(sub="user-c")


def _make_manager() -> tuple[JobManager, Mock, Mock, threading.Event]:
    """Mocks for both services and an Event set when a job finishes."""
    mock_db = Mock(spec=JobStore)
    mock_lookup = Mock()
    mock_lookup.get_job.return_value = None
    done = threading.Event()

    def _on_finished(*args: object) -> None:
        done.set()

    mock_db.update_completed.side_effect = _on_finished
    mock_db.update_failed.side_effect = _on_finished
    manager = JobManager(job_store=mock_db, lookup_service=mock_lookup)
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
        assert mock_db.create.call_args.args == (job_id,)
        kwargs = mock_db.create.call_args.kwargs
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
        assert json.loads(mock_db.update_completed.call_args[0][1]) == {"value": 6}

    def test_needs_the_ai_permission(self):
        jm, mock_db, _, _ = _make_manager()
        with raises_code(AccessDeniedError, ErrorCode.ACCESS_DENIED):
            jm.submit(NO_ROLE, "discussion", lambda principal: None)
        mock_db.create.assert_not_called()


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


class TestDenialLog:
    """投入者の照合で断ったら、U2 の ``authz.denied`` を 1 行書く（NFR8.6、BR2.13）。

    Build and Test の Loop-back 2。理由は応答が NOT_FOUND のため ``not_found``。
    ジョブは ``ResourceType`` に無いので ``resource_type`` は書かない（log_event は
    None の項目を書かない）。存在しないジョブも、他人のものと同じ 1 行にする。
    """

    @pytest.mark.parametrize(
        "stored",
        [_job(owner_sub="user-a"), _job(owner_sub=None), None],
        ids=["someone-else", "no-submitter", "missing"],
    )
    def test_refused_job_logs_one_denial(self, capsys, stored):
        jm, _, mock_lookup, _ = _make_manager()
        mock_lookup.get_job.return_value = stored
        logs = LogCapture(capsys)

        with raises_code(JobNotFoundError, ErrorCode.JOB_NOT_FOUND):
            jm.get(OTHER, "job-secret-id")

        (line,) = logs.events("authz.denied")
        assert line["reason"] == "not_found"
        assert line["sub"] == OTHER.sub
        assert line.get("resource_type") is None
        # ジョブの ID はログに出さない（NFR8.6）
        assert all("job-secret-id" not in json.dumps(r) for r in logs.records())

    def test_the_owner_logs_no_denial(self, capsys):
        jm, _, mock_lookup, _ = _make_manager()
        mock_lookup.get_job.return_value = _job()
        logs = LogCapture(capsys)

        jm.get(OWNER, "job-1")

        assert logs.events("authz.denied") == []

    def test_unreachable_jobs_table_logs_no_denial(self, capsys):
        """503 は拒否ではないので ``authz.denied`` を書かない。"""
        jm, _, mock_lookup, _ = _make_manager()
        mock_lookup.get_job.side_effect = AuthzLookupError(
            "jobs table down", reason="unreachable"
        )
        logs = LogCapture(capsys)

        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            jm.get(OTHER, "job-1")

        assert logs.events("authz.denied") == []


class TestAnotherTask:
    def test_a_job_is_readable_from_another_manager_instance(self):
        """投入した台と別の台（別の JobManager）からも本人は結果を取れる（NFR9.16）。

        JobManager は台のメモリに結果を持たず、Jobs テーブルだけを読む。共有の
        テーブルを辞書で置き換えて確かめる。
        """
        table: dict[str, Job] = {}
        done = threading.Event()

        db = Mock(spec=JobStore)

        def create(job_id, *, status, expires_at, owner_sub, job_type):
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

        db.create.side_effect = create
        db.update_completed.side_effect = completed
        lookup = Mock()
        lookup.get_job.side_effect = table.get

        submitting = JobManager(job_store=db, lookup_service=lookup)
        reading = JobManager(job_store=Mock(spec=JobStore), lookup_service=lookup)
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

        mock_db.update_status.assert_called_once()
        assert mock_db.update_status.call_args[0][1] == "running"
        mock_db.update_completed.assert_called_once()
        result_json = mock_db.update_completed.call_args[0][1]
        assert json.loads(result_json) == {"data": "ok"}

    def test_failed_job_calls_update_failed(self):
        def failing(principal: Principal) -> None:
            raise ValueError("test error")

        jm, mock_db, _, done = _make_manager()
        jm.submit(OWNER, "discussion", failing)
        assert done.wait(timeout=5)

        mock_db.update_status.assert_called_once()
        mock_db.update_failed.assert_called_once()
        # U6 BR8.3: 例外文ではなくエラーコードを保存する（コードのない例外は UNKNOWN）
        assert mock_db.update_failed.call_args[0][1] == ErrorCode.UNKNOWN.value

    def test_failed_job_saves_the_coded_errors_code(self):
        def failing(principal: Principal) -> None:
            raise CodedError(
                "s3://bucket/secret-key unreadable",
                code=ErrorCode.DISCUSSION_TOO_LARGE,
            )

        jm, mock_db, _, done = _make_manager()
        jm.submit(OWNER, "discussion", failing)
        assert done.wait(timeout=5)

        (_job_id, saved) = mock_db.update_failed.call_args[0]
        assert saved == ErrorCode.DISCUSSION_TOO_LARGE.value
        assert "secret" not in saved
