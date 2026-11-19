"""SourceUploadManager: start, part URLs, completion and cancel (W2, W3).

US2.1, US3.1, BR2.1〜BR2.8, BR3.3, NFR1.3, NFR1.5, NFR5.19, NFR5.20. S3 and
DynamoDB are moto; the stores and components are the real ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import boto3
import pytest

moto = pytest.importorskip("moto")

from src.config import config  # noqa: E402
from src.managers.source_upload_manager import (  # noqa: E402
    SourceUploadError,
    SourceUploadManager,
)
from src.models.authz import Principal, Role  # noqa: E402
from src.models.errors import AccessDeniedError, ErrorCode  # noqa: E402
from src.models.source_file import SourceEncoding, SourceFileStatus  # noqa: E402
from src.services.s3_service import S3Service  # noqa: E402
from src.services.stores.app_settings_store import AppSettingsStore  # noqa: E402
from src.services.stores.source_file_store import SourceFileStore  # noqa: E402
from tests.advanced_helpers import (  # noqa: E402
    BUCKET,
    REGION,
    SOURCE_FILES_TABLE,
    create_source_files_table,
)
from tests.error_helpers import raises_code  # noqa: E402
from tests.log_helpers import LogCapture  # noqa: E402

pytestmark = pytest.mark.unit

ADMIN = Principal(sub="admin-1", roles=frozenset({Role.ADMIN}))
OTHER = Principal(sub="admin-2", roles=frozenset({Role.ADMIN}))
MEMBER = Principal(sub="member-1", roles=frozenset({Role.MEMBER}))
NOW = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
CSV = "id,age,area\n1,30,東京\n2,41,大阪\n3,25,札幌\n".encode("utf-8")
PART = 5 * 1024 * 1024


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", True)
    monkeypatch.setattr(config, "CODE_INTERPRETER_ID", "ci-1")
    monkeypatch.setattr(config, "ADVANCED_UPLOAD_PART_SIZE_BYTES", PART)
    monkeypatch.setattr(config, "ADVANCED_UPLOAD_MAX_BYTES", 1_000_000_000)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        dynamo = boto3.client("dynamodb", region_name=REGION)
        create_source_files_table(dynamo)
        s3_client = boto3.client("s3", region_name=REGION)
        s3_client.create_bucket(Bucket=BUCKET)
        settings = Mock(spec=AppSettingsStore)
        settings.get.return_value = None
        clock = _Clock(NOW)
        store = SourceFileStore(
            region=REGION, table_name=SOURCE_FILES_TABLE, client=dynamo
        )
        manager = SourceUploadManager(
            settings_store=settings,
            source_file_store=store,
            s3_service=S3Service(BUCKET, REGION),
            clock=clock,
        )
        yield manager, store, s3_client, clock


def _start(manager: SourceUploadManager, size: int = len(CSV), name: str = "a.csv"):
    return manager.start_upload(
        ADMIN, filename=name, size_bytes=size, content_type="text/csv"
    )


def _send_parts(
    s3_client: Any, store: SourceFileStore, source_file_id: str, data: bytes
):
    found = store.get(source_file_id)
    assert found is not None
    parts = []
    for number, start in enumerate(range(0, len(data), PART), start=1):
        response = s3_client.upload_part(
            Bucket=BUCKET,
            Key=found.s3_key,
            UploadId=found.multipart_upload_id,
            PartNumber=number,
            Body=data[start : start + PART],
        )
        parts.append((number, response["ETag"]))
    return parts


def _multipart_uploads(s3_client: Any) -> list[Any]:
    return s3_client.list_multipart_uploads(Bucket=BUCKET).get("Uploads", [])


class TestStart:
    def test_issues_every_part_url_and_records_the_upload(self, env, caplog) -> None:
        manager, store, s3_client, _ = env

        session = _start(manager, size=2 * PART + 1)

        assert session.part_count == 3
        assert [p.part_number for p in session.part_urls] == [1, 2, 3]
        assert session.urls_expire_at == NOW + timedelta(
            seconds=config.ADVANCED_UPLOAD_URL_EXPIRATION_SECONDS
        )
        found = store.get(session.source_file_id)
        assert found is not None and found.status is SourceFileStatus.UPLOADING
        assert found.s3_key == f"sources/{ADMIN.sub}/{session.source_file_id}"
        assert len(_multipart_uploads(s3_client)) == 1
        # The URLs themselves are never logged.
        assert all(p.url not in caplog.text for p in session.part_urls)

    def test_exact_limit_is_accepted(self, env, monkeypatch) -> None:
        manager, *_ = env
        monkeypatch.setattr(config, "ADVANCED_UPLOAD_MAX_BYTES", PART)

        assert _start(manager, size=PART).part_count == 1

    def test_over_the_limit_issues_nothing(self, env, monkeypatch) -> None:
        manager, _, s3_client, _ = env
        monkeypatch.setattr(config, "ADVANCED_UPLOAD_MAX_BYTES", PART)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_TOO_LARGE, max_bytes=PART):
            _start(manager, size=PART + 1)
        assert _multipart_uploads(s3_client) == []

    @pytest.mark.parametrize("name", ["book.xlsx", "notes.txt", "noext"])
    def test_not_csv_or_tsv_is_invalid(self, env, name: str) -> None:
        manager, _, s3_client, _ = env

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID):
            _start(manager, name=name)
        assert _multipart_uploads(s3_client) == []

    def test_unusual_content_type_is_invalid(self, env) -> None:
        manager, *_ = env

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID):
            manager.start_upload(
                ADMIN, filename="a.csv", size_bytes=10, content_type="image/png"
            )

    def test_the_eleventh_file_is_refused(self, env, monkeypatch) -> None:
        manager, *_ = env
        monkeypatch.setattr(config, "ADVANCED_UPLOAD_MAX_FILES", 2)
        _start(manager)
        _start(manager)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_TOO_MANY_FILES):
            _start(manager)

    def test_unavailable_user_is_refused(self, env) -> None:
        manager, _, s3_client, _ = env

        with raises_code(SourceUploadError, ErrorCode.ADVANCED_GENERATION_UNAVAILABLE):
            manager.start_upload(
                MEMBER, filename="a.csv", size_bytes=10, content_type="text/csv"
            )
        assert _multipart_uploads(s3_client) == []

    def test_no_ai_execute_is_forbidden(self, env) -> None:
        manager, *_ = env

        with pytest.raises(AccessDeniedError):
            manager.start_upload(
                Principal(sub="x"), filename="a.csv", size_bytes=1, content_type=""
            )


class TestRefresh:
    def test_returns_new_urls_for_the_numbers(self, env) -> None:
        manager, store, _, clock = env
        session = _start(manager, size=2 * PART)
        clock.now = NOW + timedelta(minutes=55)

        refreshed = manager.refresh_part_urls(ADMIN, session.source_file_id, [2])

        assert [p.part_number for p in refreshed.part_urls] == [2]
        assert refreshed.urls_expire_at == clock.now + timedelta(
            seconds=config.ADVANCED_UPLOAD_URL_EXPIRATION_SECONDS
        )
        found = store.get(session.source_file_id)
        assert found is not None and found.url_refresh_count == 1

    def test_someone_elses_upload_is_not_found(self, env) -> None:
        manager, *_ = env
        session = _start(manager)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.refresh_part_urls(OTHER, session.source_file_id, [1])

    @pytest.mark.parametrize("numbers", [[0], [2], [], [1, 1]])
    def test_numbers_out_of_range_are_invalid(self, env, numbers) -> None:
        manager, *_ = env
        session = _start(manager)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID):
            manager.refresh_part_urls(ADMIN, session.source_file_id, numbers)

    def test_refreshes_stop_at_the_limit(self, env, monkeypatch) -> None:
        manager, *_ = env
        monkeypatch.setattr(config, "ADVANCED_UPLOAD_URL_REFRESH_MAX", 2)
        session = _start(manager)
        manager.refresh_part_urls(ADMIN, session.source_file_id, [1])
        manager.refresh_part_urls(ADMIN, session.source_file_id, [1])

        with raises_code(
            SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID, max_refreshes=2
        ):
            manager.refresh_part_urls(ADMIN, session.source_file_id, [1])


class TestComplete:
    def test_completes_inspects_and_becomes_ready(self, env, capsys) -> None:
        manager, store, s3_client, _ = env
        logs = LogCapture(capsys)
        session = _start(manager)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)

        ready = manager.complete_upload(ADMIN, session.source_file_id, parts)

        assert ready.status is SourceFileStatus.READY
        assert ready.metadata is not None
        assert [c.name for c in ready.metadata.columns] == ["id", "age", "area"]
        assert ready.metadata.encoding is SourceEncoding.UTF8
        assert ready.multipart_upload_id is None
        (event,) = logs.events("advanced_generation.upload_completed")
        assert set(event) <= {
            "event",
            "ts",
            "source_file_id",
            "size_bytes",
            "status",
            "error_code",
            "encoding",
            "estimated_rows",
        }
        assert event["status"] == "ready" and event["estimated_rows"] == 3

    def test_completes_only_once(self, env) -> None:
        manager, store, s3_client, _ = env
        session = _start(manager)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)
        manager.complete_upload(ADMIN, session.source_file_id, parts)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.complete_upload(ADMIN, session.source_file_id, parts)

    def test_someone_elses_upload_is_not_found(self, env) -> None:
        manager, store, s3_client, _ = env
        session = _start(manager)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.complete_upload(OTHER, session.source_file_id, parts)

    def test_size_mismatch_aborts_and_marks_error(self, env, capsys) -> None:
        manager, store, s3_client, _ = env
        logs = LogCapture(capsys)
        session = _start(manager, size=len(CSV) + 10)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID):
            manager.complete_upload(ADMIN, session.source_file_id, parts)

        found = store.get(session.source_file_id)
        assert found is not None and found.status is SourceFileStatus.ERROR
        assert _multipart_uploads(s3_client) == []
        (event,) = logs.events("advanced_generation.upload_completed")
        assert event["status"] == "error"
        assert event["error_code"] == ErrorCode.UPLOAD_SOURCE_INVALID.value

    def test_parts_listed_by_the_browser_must_match(self, env) -> None:
        manager, store, s3_client, _ = env
        session = _start(manager)
        _send_parts(s3_client, store, session.source_file_id, CSV)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID):
            manager.complete_upload(ADMIN, session.source_file_id, [(2, '"x"')])

    def test_not_a_table_marks_error(self, env) -> None:
        manager, store, s3_client, _ = env
        data = b"\x89PNG\r\n\x1a\n\x00\x00binary"
        session = _start(manager, size=len(data))
        parts = _send_parts(s3_client, store, session.source_file_id, data)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID):
            manager.complete_upload(ADMIN, session.source_file_id, parts)
        found = store.get(session.source_file_id)
        assert found is not None and found.status is SourceFileStatus.ERROR
        assert found.error_code == ErrorCode.UPLOAD_SOURCE_INVALID.value

    def test_reads_only_the_head(self, env, monkeypatch) -> None:
        manager, store, s3_client, _ = env
        monkeypatch.setattr(config, "ADVANCED_INSPECT_BYTES", 20)
        session = _start(manager)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)

        ready = manager.complete_upload(ADMIN, session.source_file_id, parts)

        assert ready.metadata is not None
        assert ready.metadata.inspected_bytes == 20


class TestCancelAndRead:
    def test_cancel_drops_the_parts_and_the_record(self, env) -> None:
        manager, store, s3_client, _ = env
        session = _start(manager)

        manager.cancel_upload(ADMIN, session.source_file_id)

        assert store.get(session.source_file_id) is None
        assert _multipart_uploads(s3_client) == []

    def test_cancel_of_someone_elses_upload_is_not_found(self, env) -> None:
        manager, store, *_ = env
        session = _start(manager)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.cancel_upload(OTHER, session.source_file_id)
        assert store.get(session.source_file_id) is not None

    def test_get_upload_is_owner_only(self, env) -> None:
        manager, *_ = env
        session = _start(manager)

        assert manager.get_upload(ADMIN, session.source_file_id).source_file_id == (
            session.source_file_id
        )
        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.get_upload(OTHER, session.source_file_id)
