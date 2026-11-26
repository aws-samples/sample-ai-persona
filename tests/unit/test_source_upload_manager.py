"""SourceUploadManager: start, part URLs, completion and cancel.

S3 and DynamoDB are moto; the stores and components are the real ones.
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

    def test_every_part_url_signs_the_part_length(self, env) -> None:
        from urllib.parse import parse_qs, urlsplit

        manager, _, _, _ = env

        session = _start(manager, size=2 * PART + 1)

        for part in session.part_urls:
            signed = parse_qs(urlsplit(part.url).query)["X-Amz-SignedHeaders"]
            assert signed == ["content-length;host"]

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

        # ".txt" is now a document, so it left this list

    @pytest.mark.parametrize("name", ["book.xlsx", "image.png", "noext"])
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
            "kind",  # table or document
        }
        assert event["status"] == "ready" and event["estimated_rows"] == 3

    def test_completes_only_once(self, env, monkeypatch) -> None:
        manager, store, s3_client, _ = env
        session = _start(manager)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)
        first = manager.complete_upload(ADMIN, session.source_file_id, parts)
        inspect = Mock(side_effect=AssertionError("inspected twice"))
        monkeypatch.setattr(
            "src.managers.source_upload_manager.inspect_table_head", inspect
        )

        # The second tab gets the same row, without a second inspection.
        again = manager.complete_upload(ADMIN, session.source_file_id, parts)

        assert again == first
        inspect.assert_not_called()

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

        with raises_code(SourceUploadError, ErrorCode.SOURCE_NOT_A_TABLE):
            manager.complete_upload(ADMIN, session.source_file_id, parts)
        found = store.get(session.source_file_id)
        assert found is not None and found.status is SourceFileStatus.ERROR
        assert found.error_code == ErrorCode.SOURCE_NOT_A_TABLE.value

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


# --- numbers, expiry, double completion, listing, deletion ---------------------

DAYS = config.ADVANCED_SOURCE_RETENTION_DAYS


def _ready(manager, store, s3_client, name: str = "a.csv", data: bytes = CSV):
    session = _start(manager, size=len(data), name=name)
    parts = _send_parts(s3_client, store, session.source_file_id, data)
    return manager.complete_upload(ADMIN, session.source_file_id, parts)


def _set(store: SourceFileStore, source_file_id: str, **values: Any) -> None:
    names = {f"#a{i}": k for i, k in enumerate(values)}
    sets = ", ".join(f"#a{i} = :v{i}" for i in range(len(values)))
    store.client.update_item(
        TableName=SOURCE_FILES_TABLE,
        Key={"source_file_id": {"S": source_file_id}},
        UpdateExpression=f"SET {sets}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(values.values())},
    )


def _iso(value: datetime) -> dict[str, str]:
    return {"S": value.isoformat(timespec="microseconds")}


class TestNumberAndExpiry:
    def test_start_gives_the_number_and_the_expiry(self, env) -> None:
        manager, store, *_ = env

        first = _start(manager, name="sales.csv")
        second = _start(manager, name="sales.csv")
        other = _start(manager, name="other.csv")

        found = [store.get(s.source_file_id) for s in (first, second, other)]
        assert [f.display_seq for f in found if f] == [1, 2, 1]
        assert all(f and f.expires_at == NOW + timedelta(days=DAYS) for f in found)
        assert found[1] is not None and found[1].display_name == "sales.csv (2)"

    def test_a_deleted_file_does_not_renumber_the_others(self, env) -> None:
        manager, store, s3_client, _ = env
        first = _ready(manager, store, s3_client, name="s.csv")
        second = _ready(manager, store, s3_client, name="s.csv")

        manager.delete_source(ADMIN, first.source_file_id)

        assert store.get(second.source_file_id).display_seq == 2  # type: ignore[union-attr]
        third = _start(manager, name="s.csv")
        assert store.get(third.source_file_id).display_seq == 3  # type: ignore[union-attr]


class TestDoubleCompletion:
    def test_while_the_first_still_inspects_the_row_is_returned(self, env) -> None:
        manager, store, s3_client, clock = env
        session = _start(manager)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)
        _set(
            store,
            session.source_file_id,
            status={"S": "inspecting"},
            updated_at=_iso(NOW),
        )
        clock.now = NOW + timedelta(minutes=14)

        row = manager.complete_upload(ADMIN, session.source_file_id, parts)

        assert row.status is SourceFileStatus.INSPECTING

    def test_after_15_minutes_the_inspection_runs_again(self, env) -> None:
        manager, store, s3_client, clock = env
        session = _start(manager)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)
        _set(
            store,
            session.source_file_id,
            status={"S": "inspecting"},
            updated_at=_iso(NOW),
        )
        clock.now = NOW + timedelta(minutes=16)

        assert (
            manager.complete_upload(ADMIN, session.source_file_id, parts).status
            is SourceFileStatus.READY
        )

    def test_an_error_row_is_returned_as_it_is(self, env) -> None:
        manager, store, s3_client, _ = env
        data = b"a,a\n1,2\n"
        session = _start(manager, size=len(data))
        parts = _send_parts(s3_client, store, session.source_file_id, data)
        with raises_code(SourceUploadError, ErrorCode.SOURCE_DUPLICATE_COLUMN_NAME):
            manager.complete_upload(ADMIN, session.source_file_id, parts)

        row = manager.complete_upload(ADMIN, session.source_file_id, parts)

        assert row.status is SourceFileStatus.ERROR
        assert row.error_code == ErrorCode.SOURCE_DUPLICATE_COLUMN_NAME.value

    def test_someone_elses_second_completion_is_not_found(self, env) -> None:
        manager, store, s3_client, _ = env
        ready = _ready(manager, store, s3_client)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.complete_upload(OTHER, ready.source_file_id, [])


class TestInspectionCodes:
    @pytest.mark.parametrize(
        ("data", "code"),
        [
            (b"a,,c\n1,2,3\n", ErrorCode.SOURCE_EMPTY_COLUMN_NAME),
            (b"a,b\n\xff\xfe\xfd\xfc,\x80\x80\n", ErrorCode.SOURCE_ENCODING_UNKNOWN),
        ],
    )
    def test_the_record_keeps_the_new_code_only(self, env, capsys, data, code) -> None:
        manager, store, s3_client, _ = env
        logs = LogCapture(capsys)
        session = _start(manager, size=len(data))
        parts = _send_parts(s3_client, store, session.source_file_id, data)

        with raises_code(SourceUploadError, code):
            manager.complete_upload(ADMIN, session.source_file_id, parts)

        found = store.get(session.source_file_id)
        assert found is not None and found.error_code == code.value
        (event,) = logs.events("advanced_generation.upload_completed")
        assert event["error_code"] == code.value

    def test_an_error_file_is_not_counted(self, env, monkeypatch) -> None:
        manager, store, s3_client, _ = env
        monkeypatch.setattr(config, "ADVANCED_UPLOAD_MAX_FILES", 1)
        data = b"a,a\n1,2\n"
        session = _start(manager, size=len(data))
        parts = _send_parts(s3_client, store, session.source_file_id, data)
        with raises_code(SourceUploadError, ErrorCode.SOURCE_DUPLICATE_COLUMN_NAME):
            manager.complete_upload(ADMIN, session.source_file_id, parts)

        assert _start(manager).part_count == 1


class TestList:
    def test_lists_the_owners_live_files_newest_first(self, env) -> None:
        manager, store, s3_client, clock = env
        older = _ready(manager, store, s3_client, name="old.csv")
        clock.now = NOW + timedelta(minutes=1)
        newer = _start(manager, name="new.csv")
        clock.now = NOW + timedelta(minutes=2)
        other = SourceUploadManager(
            settings_store=manager._advanced_generation_policy._settings,
            source_file_store=store,
            s3_service=manager._s3,
            clock=clock,
        )
        other.start_upload(OTHER, filename="x.csv", size_bytes=10, content_type="")

        listed = manager.list_uploads(ADMIN)

        assert [f.source_file_id for f in listed] == [
            newer.source_file_id,
            older.source_file_id,
        ]

    def test_expired_files_drop_out_of_the_list(self, env) -> None:
        manager, store, s3_client, clock = env
        ready = _ready(manager, store, s3_client)

        clock.now = NOW + timedelta(days=DAYS)

        assert manager.list_uploads(ADMIN) == []
        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.get_upload(ADMIN, ready.source_file_id)

    def test_the_list_reconciles_old_u1_records(self, env, capsys) -> None:
        manager, store, s3_client, clock = env
        logs = LogCapture(capsys)
        live = _ready(manager, store, s3_client, name="live.csv")
        old = _ready(manager, store, s3_client, name="old.csv")
        for f in (live, old):
            store.client.update_item(
                TableName=SOURCE_FILES_TABLE,
                Key={"source_file_id": {"S": f.source_file_id}},
                UpdateExpression="REMOVE expires_at",
            )
        _set(
            store,
            old.source_file_id,
            created_at=_iso(NOW - timedelta(days=DAYS + 1)),
        )

        listed = manager.list_uploads(ADMIN)

        assert [f.source_file_id for f in listed] == [live.source_file_id]
        assert store.get(old.source_file_id) is None
        filled = store.get(live.source_file_id)
        assert filled is not None and filled.expires_at == NOW + timedelta(days=DAYS)
        (event,) = logs.events("advanced_generation.sources_reconciled")
        assert (event["filled"], event["deleted"]) == (1, 1)

    def test_list_item_says_in_use_and_expiry(self, env) -> None:
        manager, store, s3_client, _ = env
        ready = _ready(manager, store, s3_client)
        _set(
            store,
            ready.source_file_id,
            in_use_by={"S": "gen-1"},
            in_use_until=_iso(NOW + timedelta(hours=1)),
        )

        (found,) = manager.list_uploads(ADMIN)
        item = manager.list_item(found)

        assert item.in_use and not item.selectable
        assert item.expires_on == (NOW + timedelta(days=DAYS)).date()


class TestDelete:
    def _versioned(self, s3_client: Any) -> None:
        s3_client.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )

    def test_deletes_every_version_and_the_record(self, env, capsys) -> None:
        manager, store, s3_client, _ = env
        self._versioned(s3_client)
        logs = LogCapture(capsys)
        ready = _ready(manager, store, s3_client)
        s3_client.put_object(Bucket=BUCKET, Key=ready.s3_key, Body=b"id\n1\n")

        manager.delete_source(ADMIN, ready.source_file_id)

        assert store.get(ready.source_file_id) is None
        versions = s3_client.list_object_versions(Bucket=BUCKET, Prefix=ready.s3_key)
        assert versions.get("Versions", []) == []
        assert versions.get("DeleteMarkers", []) == []
        (event,) = logs.events("advanced_generation.source_deleted")
        assert event["status"] == "deleted" and event["versions"] == 2
        assert set(event) <= {
            "event",
            "ts",
            "source_file_id",
            "status",
            "versions",
            "error_code",
        }
        assert "a.csv" not in capsys.readouterr().out

    def test_an_error_file_can_be_deleted_alone(self, env) -> None:
        manager, store, s3_client, _ = env
        keep = _ready(manager, store, s3_client, name="keep.csv")
        data = b"a,a\n1,2\n"
        session = _start(manager, size=len(data))
        parts = _send_parts(s3_client, store, session.source_file_id, data)
        with raises_code(SourceUploadError, ErrorCode.SOURCE_DUPLICATE_COLUMN_NAME):
            manager.complete_upload(ADMIN, session.source_file_id, parts)

        manager.delete_source(ADMIN, session.source_file_id)

        assert store.get(session.source_file_id) is None
        assert store.get(keep.source_file_id) is not None

    def test_a_file_in_use_is_kept(self, env, capsys) -> None:
        manager, store, s3_client, _ = env
        logs = LogCapture(capsys)
        ready = _ready(manager, store, s3_client)
        _set(
            store,
            ready.source_file_id,
            in_use_by={"S": "gen-1"},
            in_use_until=_iso(NOW + timedelta(hours=1)),
        )

        with raises_code(SourceUploadError, ErrorCode.SOURCE_FILE_IN_USE):
            manager.delete_source(ADMIN, ready.source_file_id)

        found = store.get(ready.source_file_id)
        assert found is not None and found.status is SourceFileStatus.READY
        (event,) = logs.events("advanced_generation.source_deleted")
        assert event["status"] == "in_use"

    def test_an_s3_failure_leaves_deleting_and_a_retry_finishes(
        self, env, monkeypatch, capsys
    ) -> None:
        manager, store, s3_client, _ = env
        logs = LogCapture(capsys)
        ready = _ready(manager, store, s3_client)
        real = manager._s3.s3_client.delete_objects
        monkeypatch.setattr(
            manager._s3.s3_client,
            "delete_objects",
            Mock(return_value={"Errors": [{"Code": "InternalError"}]}),
        )

        with raises_code(SourceUploadError, ErrorCode.S3_OPERATION_FAILED):
            manager.delete_source(ADMIN, ready.source_file_id)
        found = store.get(ready.source_file_id)
        assert found is not None and found.status is SourceFileStatus.DELETING
        assert [f.status for f in manager.list_uploads(ADMIN)] == [
            SourceFileStatus.DELETING
        ]

        monkeypatch.setattr(manager._s3.s3_client, "delete_objects", real)
        manager.delete_source(ADMIN, ready.source_file_id)

        assert store.get(ready.source_file_id) is None
        statuses = [
            e["status"] for e in logs.events("advanced_generation.source_deleted")
        ]
        assert statuses == ["failed", "deleted"]

    def test_someone_elses_file_is_not_found_and_untouched(self, env) -> None:
        manager, store, s3_client, _ = env
        ready = _ready(manager, store, s3_client)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.delete_source(OTHER, ready.source_file_id)
        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.delete_source(ADMIN, "missing")
        assert store.get(ready.source_file_id) is not None

    def test_an_uploading_file_is_cancelled_not_deleted(self, env) -> None:
        manager, store, *_ = env
        session = _start(manager)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.delete_source(ADMIN, session.source_file_id)
        manager.cancel_upload(ADMIN, session.source_file_id)
        assert store.get(session.source_file_id) is None

    def test_a_stuck_inspection_can_be_deleted_after_15_minutes(self, env) -> None:
        manager, store, _, clock = env
        session = _start(manager)
        _set(
            store,
            session.source_file_id,
            status={"S": "inspecting"},
            updated_at=_iso(NOW),
        )
        found = store.get(session.source_file_id)
        assert found is not None

        assert not manager.inspection_stuck(found)
        with raises_code(SourceUploadError, ErrorCode.UPLOAD_NOT_FOUND):
            manager.delete_source(ADMIN, session.source_file_id)

        clock.now = NOW + timedelta(minutes=16)
        assert manager.inspection_stuck(found)
        manager.delete_source(ADMIN, session.source_file_id)
        assert store.get(session.source_file_id) is None


# --- documents -------------------------------------------------------------


def _start_document(
    manager: SourceUploadManager,
    name: str,
    size: int,
    content_type: str = "application/octet-stream",
):
    return manager.start_upload(
        ADMIN, filename=name, size_bytes=size, content_type=content_type
    )


def _upload_document(env: Any, name: str, data: bytes):
    manager, store, s3_client, _ = env
    session = _start_document(manager, name, len(data))
    parts = _send_parts(s3_client, store, session.source_file_id, data)
    return session.source_file_id, parts


class TestDocumentStart:
    @pytest.mark.parametrize(
        ("name", "content_type"),
        [
            ("interview.pdf", "application/pdf"),
            (
                "memo.DOCX",
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document",
            ),
            ("note.txt", "text/plain"),
            ("readme.md", "text/markdown"),
        ],
    )
    def test_documents_start_as_documents(self, env, name, content_type) -> None:
        from src.models.source_file import SourceKind

        manager, store, *_ = env

        session = _start_document(manager, name, 1000, content_type)

        found = store.get(session.source_file_id)
        assert found is not None and found.kind is SourceKind.DOCUMENT

    def test_a_table_still_starts_as_a_table(self, env) -> None:
        from src.models.source_file import SourceKind

        manager, store, *_ = env

        session = _start(manager)

        found = store.get(session.source_file_id)
        assert found is not None and found.kind is SourceKind.TABLE

    def test_exactly_100mb_is_accepted_and_one_byte_more_is_not(self, env) -> None:
        manager, _, s3_client, _ = env

        assert _start_document(manager, "a.pdf", 100_000_000).part_count >= 1
        with raises_code(
            SourceUploadError, ErrorCode.UPLOAD_TOO_LARGE, max_bytes=100_000_000
        ):
            _start_document(manager, "b.pdf", 100_000_001)

    def test_an_old_word_file_is_told_to_save_as_docx(self, env) -> None:
        manager, _, s3_client, _ = env

        with raises_code(
            SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID, reason="legacy_word"
        ):
            _start_document(manager, "old.doc", 1000)
        assert _multipart_uploads(s3_client) == []

    def test_excel_is_a_wrong_format(self, env) -> None:
        manager, *_ = env

        with raises_code(
            SourceUploadError, ErrorCode.UPLOAD_SOURCE_INVALID, reason="format"
        ):
            _start_document(manager, "book.xlsx", 1000)

    def test_tables_and_documents_count_together(self, env, monkeypatch) -> None:
        manager, *_ = env
        monkeypatch.setattr(config, "ADVANCED_UPLOAD_MAX_FILES", 2)
        _start(manager)
        _start_document(manager, "a.pdf", 1000)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_TOO_MANY_FILES):
            _start_document(manager, "b.md", 1000)


class TestDocumentComplete:
    def test_a_pdf_is_inspected_from_its_head_and_tail(self, env, capsys) -> None:
        from src.models.source_file import DocumentMetadata, SourceFormat
        from tests.document_fixtures import pdf_bytes

        manager, store, *_ = env
        logs = LogCapture(capsys)
        source_file_id, parts = _upload_document(env, "面談.pdf", pdf_bytes(["hi"]))

        ready = manager.complete_upload(ADMIN, source_file_id, parts)

        assert ready.status is SourceFileStatus.READY
        assert isinstance(ready.metadata, DocumentMetadata)
        assert ready.metadata.format is SourceFormat.PDF
        stored = store.get(source_file_id)
        assert stored is not None and isinstance(stored.metadata, DocumentMetadata)
        (event,) = logs.events("advanced_generation.upload_completed")
        assert event["status"] == "ready"
        assert "estimated_chars" in event and "面談" not in str(event)

    def test_an_encrypted_pdf_goes_to_error(self, env) -> None:
        from tests.document_fixtures import pdf_bytes

        manager, store, *_ = env
        source_file_id, parts = _upload_document(
            env, "a.pdf", pdf_bytes(["x"], encrypted=True)
        )

        with raises_code(SourceUploadError, ErrorCode.DOCUMENT_ENCRYPTED):
            manager.complete_upload(ADMIN, source_file_id, parts)

        found = store.get(source_file_id)
        assert found is not None and found.status is SourceFileStatus.ERROR
        assert found.error_code == ErrorCode.DOCUMENT_ENCRYPTED.value

    def test_bytes_that_do_not_match_go_to_error(self, env) -> None:
        manager, store, *_ = env
        source_file_id, parts = _upload_document(env, "a.docx", b"plain text")

        with raises_code(SourceUploadError, ErrorCode.SOURCE_NOT_A_DOCUMENT):
            manager.complete_upload(ADMIN, source_file_id, parts)

    def test_a_docx_is_ready(self, env) -> None:
        from tests.document_fixtures import docx_bytes

        manager, *_ = env
        source_file_id, parts = _upload_document(
            env, "a.docx", docx_bytes([(1, "見出し"), "本文"])
        )

        assert manager.complete_upload(ADMIN, source_file_id, parts).status is (
            SourceFileStatus.READY
        )

    def test_text_keeps_its_encoding(self, env) -> None:
        manager, *_ = env
        source_file_id, parts = _upload_document(
            env, "a.txt", "面談の記録\n".encode("cp932")
        )

        ready = manager.complete_upload(ADMIN, source_file_id, parts)

        assert ready.metadata is not None
        assert ready.metadata.encoding is SourceEncoding.CP932

    def test_a_document_over_100mb_on_s3_is_refused(self, env, monkeypatch) -> None:
        # The parts S3 holds are checked against the document limit
        manager, store, s3_client, _ = env
        monkeypatch.setattr(config, "ADVANCED_DOCUMENT_MAX_BYTES", 10)
        data = b"%PDF-1.4 " + b"x" * 20
        session = _start_document(manager, "a.pdf", 5)
        parts = _send_parts(s3_client, store, session.source_file_id, data)

        with raises_code(SourceUploadError, ErrorCode.UPLOAD_TOO_LARGE):
            manager.complete_upload(ADMIN, session.source_file_id, parts)

    def test_a_table_is_inspected_as_before(self, env) -> None:
        manager, store, s3_client, _ = env
        session = _start(manager)
        parts = _send_parts(s3_client, store, session.source_file_id, CSV)

        ready = manager.complete_upload(ADMIN, session.source_file_id, parts)

        assert ready.table_metadata is not None
        assert [c.name for c in ready.table_metadata.columns] == ["id", "age", "area"]
