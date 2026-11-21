"""SourceFileStore (``SourceFiles``, contract C4) with moto.

US2.1, US3.1, BR2.4, BR3.3, NFR5.19 (completion takes effect once; a stuck
inspection can be retried after 15 minutes), BR2.1 (the owner's files through
the ``owner_sub-created_at`` index) and BR6.4 (the in-use mark).
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.source_file import (  # noqa: E402
    SourceColumn,
    SourceFile,
    SourceFileStatus,
    SourceMetadata,
)
from src.services.stores.source_file_store import (  # noqa: E402
    OWNER_INDEX,
    SourceFileStore,
    SourceFileStoreError,
    SourceFileUnavailable,
)

REGION = "ap-northeast-1"
TABLE = "SourceFilesTest"
NOW = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
STALE = timedelta(minutes=15)
PENDING = timedelta(hours=24)


@pytest.fixture
def client(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        c.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "source_file_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "source_file_id", "AttributeType": "S"},
                {"AttributeName": "owner_sub", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": OWNER_INDEX,
                    "KeySchema": [
                        {"AttributeName": "owner_sub", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield c


@pytest.fixture
def store(client) -> SourceFileStore:
    return SourceFileStore(region=REGION, table_name=TABLE, client=client)


def _new(owner: str = "sub-a", now: datetime = NOW, **kwargs: Any) -> SourceFile:
    return SourceFile.create_new(
        owner_sub=owner,
        filename="sales.csv",
        size_bytes=1000,
        part_count=1,
        now=now,
        multipart_upload_id="mpu-1",
        **kwargs,
    )


def _metadata() -> SourceMetadata:
    return SourceMetadata.create(
        format="csv",
        encoding="utf-8",
        delimiter=",",
        columns=[SourceColumn("age", "integer")],
        sample_rows=[["31"]],
        estimated_rows=10,
        inspected_bytes=1000,
        inspected_at=NOW,
    )


def _begin(store: SourceFileStore, sf: SourceFile, now: datetime = NOW) -> SourceFile:
    return store.begin_inspection(
        sf.source_file_id,
        sf.owner_sub,
        now=now,
        stale_before=now - STALE,
        created_after=now - PENDING,
    )


class TestCreateAndRead:
    def test_put_new_and_get(self, store):
        sf = _new()
        store.put_new(sf)

        assert store.get(sf.source_file_id) == sf

    def test_put_new_refuses_an_existing_id(self, store):
        sf = _new()
        store.put_new(sf)

        with pytest.raises(SourceFileStoreError):
            store.put_new(sf)

    def test_get_for_owner_hides_someone_elses_file(self, store):
        sf = _new()
        store.put_new(sf)

        assert store.get_for_owner(sf.source_file_id, "sub-a") == sf
        assert store.get_for_owner(sf.source_file_id, "sub-b") is None
        assert store.get_for_owner("missing", "sub-a") is None

    def test_list_for_owner_uses_the_owner_index(self, store):
        mine = [_new(now=NOW + timedelta(minutes=i)) for i in range(3)]
        for sf in mine:
            store.put_new(sf)
        store.put_new(_new(owner="sub-b"))

        listed = store.list_for_owner("sub-a")

        assert [s.source_file_id for s in listed] == [
            s.source_file_id for s in reversed(mine)
        ]


class TestInspection:
    def test_completion_takes_effect_once(self, store):
        sf = _new()
        store.put_new(sf)

        inspecting = _begin(store, sf)

        assert inspecting.status is SourceFileStatus.INSPECTING
        with pytest.raises(SourceFileUnavailable):
            _begin(store, sf, now=NOW + timedelta(minutes=1))

    def test_a_stuck_inspection_can_be_retried_after_15_minutes(self, store):
        sf = _new()
        store.put_new(sf)
        _begin(store, sf)

        with pytest.raises(SourceFileUnavailable):
            _begin(store, sf, now=NOW + timedelta(minutes=14))
        again = _begin(store, sf, now=NOW + timedelta(minutes=16))

        assert again.status is SourceFileStatus.INSPECTING

    def test_someone_elses_or_an_old_upload_cannot_complete(self, store):
        sf = _new()
        store.put_new(sf)

        with pytest.raises(SourceFileUnavailable):
            store.begin_inspection(
                sf.source_file_id,
                "sub-b",
                now=NOW,
                stale_before=NOW - STALE,
                created_after=NOW - PENDING,
            )
        with pytest.raises(SourceFileUnavailable):
            _begin(store, sf, now=NOW + timedelta(hours=25))
        with pytest.raises(SourceFileUnavailable):
            store.begin_inspection(
                "missing",
                "sub-a",
                now=NOW,
                stale_before=NOW - STALE,
                created_after=NOW - PENDING,
            )

    def test_mark_ready_writes_metadata_and_checksum(self, store):
        sf = _new()
        store.put_new(sf)
        _begin(store, sf)

        ready = store.mark_ready(sf.source_file_id, _metadata(), "crc==", now=NOW)

        assert ready.status is SourceFileStatus.READY
        assert ready.metadata == _metadata()
        assert ready.checksum == "crc=="
        assert ready.multipart_upload_id is None
        assert store.get(sf.source_file_id) == ready

    def test_mark_ready_needs_inspecting(self, store):
        sf = _new()
        store.put_new(sf)

        with pytest.raises(SourceFileUnavailable):
            store.mark_ready(sf.source_file_id, _metadata(), "crc==", now=NOW)

    def test_mark_error_keeps_only_the_code(self, store):
        sf = _new()
        store.put_new(sf)
        _begin(store, sf)

        assert store.mark_error(sf.source_file_id, "upload_source_invalid", now=NOW)
        failed = store.get(sf.source_file_id)
        assert failed is not None
        assert failed.status is SourceFileStatus.ERROR
        assert failed.error_code == "upload_source_invalid"
        # A finished file does not go back to error.
        assert not store.mark_error(sf.source_file_id, "x", now=NOW)


class TestUploadingOperations:
    def test_url_refresh_is_counted_up_to_the_limit(self, store):
        sf = _new()
        store.put_new(sf)

        for n in range(1, 3):
            refreshed = store.record_url_refresh(
                sf.source_file_id, "sub-a", max_refreshes=2, now=NOW
            )
            assert refreshed.url_refresh_count == n
        with pytest.raises(SourceFileUnavailable):
            store.record_url_refresh(
                sf.source_file_id, "sub-a", max_refreshes=2, now=NOW
            )

    def test_url_refresh_needs_the_owners_uploading_file(self, store):
        sf = _new()
        store.put_new(sf)

        with pytest.raises(SourceFileUnavailable):
            store.record_url_refresh(
                sf.source_file_id, "sub-b", max_refreshes=24, now=NOW
            )
        _begin(store, sf)
        with pytest.raises(SourceFileUnavailable):
            store.record_url_refresh(
                sf.source_file_id, "sub-a", max_refreshes=24, now=NOW
            )

    def test_delete_uploading_only_removes_the_owners_uploading_file(self, store):
        sf = _new()
        store.put_new(sf)

        assert not store.delete_uploading(sf.source_file_id, "sub-b")
        assert store.delete_uploading(sf.source_file_id, "sub-a")
        assert store.get(sf.source_file_id) is None

        other = _new()
        store.put_new(other)
        _begin(store, other)
        assert not store.delete_uploading(other.source_file_id, "sub-a")


class TestInUseMark:
    def _in_use(self, store, client) -> SourceFile:
        sf = _new()
        store.put_new(sf)
        client.update_item(
            TableName=TABLE,
            Key={"source_file_id": {"S": sf.source_file_id}},
            UpdateExpression="SET in_use_by = :g, in_use_until = :u",
            ExpressionAttributeValues={
                ":g": {"S": "gen-1"},
                ":u": {"S": (NOW + timedelta(hours=1)).isoformat()},
            },
        )
        return sf

    def test_clear_in_use_needs_the_same_generation(self, store, client):
        sf = self._in_use(store, client)

        assert not store.clear_in_use(sf.source_file_id, "gen-2", now=NOW)
        assert store.clear_in_use(sf.source_file_id, "gen-1", now=NOW)

        cleared = store.get(sf.source_file_id)
        assert cleared is not None
        assert cleared.in_use_by is None and cleared.in_use_until is None
        assert cleared.last_used_at == NOW
