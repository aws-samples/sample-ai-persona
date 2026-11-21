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

        assert not store.clear_in_use(sf.source_file_id, "gen-2")
        assert store.clear_in_use(sf.source_file_id, "gen-1")

        cleared = store.get(sf.source_file_id)
        assert cleared is not None
        assert cleared.in_use_by is None and cleared.in_use_until is None
        # U2 BR13.3: the last use is written at the start, not at the end.
        assert cleared.last_used_at is None


# --- U2: 番号・期限・削除（BR11.3、BR13.2、BR13.6、BR14.1、BR14.2、NFR5.27、NFR5.28） ---

EXPIRES = NOW + timedelta(days=7)


def _raw(client, source_file_id: str) -> dict[str, Any]:
    return client.get_item(
        TableName=TABLE, Key={"source_file_id": {"S": source_file_id}}
    )["Item"]


def _with_state(store, client, status: SourceFileStatus, **attrs: Any) -> SourceFile:
    sf = _new(expires_at=EXPIRES)
    store.put_new(sf)
    names = {"#status": "status"}
    values: dict[str, Any] = {":s": {"S": status.value}}
    sets = ["#status = :s"]
    for i, (name, value) in enumerate(attrs.items()):
        sets.append(f"{name} = :v{i}")
        values[f":v{i}"] = value
    client.update_item(
        TableName=TABLE,
        Key={"source_file_id": {"S": sf.source_file_id}},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return sf


def _begin_delete(store, sf: SourceFile, owner: str = "sub-a", now: datetime = NOW):
    return store.begin_delete(
        sf.source_file_id, owner, now=now, stale_before=now - STALE
    )


class TestNumberAndExpiry:
    def test_put_new_writes_the_number_and_the_expiry_as_epoch_seconds(
        self, store, client
    ):
        sf = _new(display_seq=2, expires_at=EXPIRES)
        store.put_new(sf)

        item = _raw(client, sf.source_file_id)
        assert item["display_seq"] == {"N": "2"}
        assert item["expires_at"] == {"N": str(int(EXPIRES.timestamp()))}
        assert store.get(sf.source_file_id) == sf

    def test_max_display_seq_counts_the_owners_live_files_of_the_name(self, store):
        assert store.max_display_seq("sub-a", "sales.csv", now=NOW) == 0
        store.put_new(_new(display_seq=1, expires_at=EXPIRES))
        store.put_new(_new(display_seq=3, expires_at=EXPIRES))
        store.put_new(_new(display_seq=9, expires_at=NOW - timedelta(seconds=1)))
        store.put_new(_new(owner="sub-b", display_seq=7, expires_at=EXPIRES))

        assert store.max_display_seq("sub-a", "sales.csv", now=NOW) == 3
        assert store.max_display_seq("sub-a", "other.csv", now=NOW) == 0

    def test_max_display_seq_counts_an_u1_record_as_1(self, store):
        store.put_new(_new())  # no number and no expiry (U1)

        assert store.max_display_seq("sub-a", "sales.csv", now=NOW) == 1

    def test_fill_expires_at_only_when_missing(self, store, client):
        sf = _new()
        store.put_new(sf)

        assert store.fill_expires_at(sf.source_file_id, "sub-a", EXPIRES)
        assert not store.fill_expires_at(
            sf.source_file_id, "sub-a", EXPIRES + timedelta(days=1)
        )
        assert not store.fill_expires_at(sf.source_file_id, "sub-b", EXPIRES)
        found = store.get(sf.source_file_id)
        assert found is not None and found.expires_at == EXPIRES

    def test_set_checksum_if_empty(self, store, client):
        sf = _new()
        store.put_new(sf)

        assert store.set_checksum_if_empty(sf.source_file_id, "crc==")
        assert not store.set_checksum_if_empty(sf.source_file_id, "other==")
        found = store.get(sf.source_file_id)
        assert found is not None and found.checksum == "crc=="

    def test_an_empty_checksum_counts_as_empty(self, store, client):
        sf = _with_state(store, client, SourceFileStatus.READY, checksum={"S": ""})

        assert store.set_checksum_if_empty(sf.source_file_id, "crc==")


class TestDelete:
    @pytest.mark.parametrize(
        "status",
        [SourceFileStatus.READY, SourceFileStatus.ERROR, SourceFileStatus.DELETING],
    )
    def test_ready_error_and_deleting_files_can_be_deleted(self, store, client, status):
        sf = _with_state(store, client, status)

        deleting = _begin_delete(store, sf)

        assert deleting.status is SourceFileStatus.DELETING
        assert store.delete_record(sf.source_file_id, "sub-a")
        assert store.get(sf.source_file_id) is None

    def test_a_stuck_inspection_can_be_deleted_but_not_a_fresh_one(self, store, client):
        fresh = _with_state(
            store,
            client,
            SourceFileStatus.INSPECTING,
            updated_at={
                "S": (NOW - timedelta(minutes=1)).isoformat(timespec="microseconds")
            },
        )
        stuck = _with_state(
            store,
            client,
            SourceFileStatus.INSPECTING,
            updated_at={
                "S": (NOW - timedelta(minutes=16)).isoformat(timespec="microseconds")
            },
        )

        with pytest.raises(SourceFileUnavailable):
            _begin_delete(store, fresh)
        assert _begin_delete(store, stuck).status is SourceFileStatus.DELETING

    def test_in_use_someone_elses_expired_and_uploading_cannot_be_deleted(
        self, store, client
    ):
        in_use = _with_state(
            store,
            client,
            SourceFileStatus.READY,
            in_use_by={"S": "gen-1"},
            in_use_until={
                "S": (NOW + timedelta(hours=1)).isoformat(timespec="microseconds")
            },
        )
        expired = _with_state(
            store,
            client,
            SourceFileStatus.READY,
            expires_at={"N": str(int((NOW - timedelta(seconds=1)).timestamp()))},
        )
        uploading = _new(expires_at=EXPIRES)
        store.put_new(uploading)
        mine = _with_state(store, client, SourceFileStatus.READY)

        for sf in (in_use, expired, uploading):
            with pytest.raises(SourceFileUnavailable):
                _begin_delete(store, sf)
        with pytest.raises(SourceFileUnavailable):
            _begin_delete(store, mine, owner="sub-b")
        with pytest.raises(SourceFileUnavailable):
            store.begin_delete("missing", "sub-a", now=NOW, stale_before=NOW - STALE)

    def test_a_lapsed_mark_does_not_block_the_delete(self, store, client):
        lapsed = _with_state(
            store,
            client,
            SourceFileStatus.READY,
            in_use_by={"S": "gen-1"},
            in_use_until={
                "S": (NOW - timedelta(seconds=1)).isoformat(timespec="microseconds")
            },
        )

        assert _begin_delete(store, lapsed).status is SourceFileStatus.DELETING

    def test_an_u1_record_without_expiry_passes_the_condition(self, store, client):
        sf = _new()
        store.put_new(sf)
        client.update_item(
            TableName=TABLE,
            Key={"source_file_id": {"S": sf.source_file_id}},
            UpdateExpression="SET #status = :s",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":s": {"S": "ready"}},
        )

        assert _begin_delete(store, sf).status is SourceFileStatus.DELETING

    def test_delete_record_needs_the_owners_deleting_file(self, store, client):
        ready = _with_state(store, client, SourceFileStatus.READY)
        deleting = _with_state(store, client, SourceFileStatus.DELETING)

        assert not store.delete_record(ready.source_file_id, "sub-a")
        assert not store.delete_record(deleting.source_file_id, "sub-b")
        assert store.get(ready.source_file_id) is not None
        assert store.delete_record(deleting.source_file_id, "sub-a")
