"""AdvancedGenerationStore (``AdvancedGenerations``, contract C5) with moto.

BR7.5 / BR6.4 (lock, generation and in-use marks in one transaction),
BR7.1 / NFR5.18 (transitions conditioned on the current state), BR7.2 /
NFR5.2 (events numbered in order and re-read with ``BETWEEN``), NFR5.1
(result parts and their size limits), BR7.3 (heartbeat) and W5-8 (finishing
releases the lock and the marks and writes finished_at / expires_at).
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.advanced_generation import (  # noqa: E402
    AdvancedGeneration,
    EventType,
    GenerationStatus,
    ResultPartName,
)
from src.models.errors import ErrorCode  # noqa: E402
from src.models.source_file import SourceFile  # noqa: E402
from src.services.stores.advanced_generation_store import (  # noqa: E402
    OWNER_INDEX,
    AdvancedGenerationStore,
    GenerationLockHeld,
    GenerationResultTooLarge,
    GenerationSourceUnavailable,
)

REGION = "ap-northeast-1"
TABLE = "AdvancedGenerationsTest"
SOURCES = "SourceFilesTest"
NOW = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
LOCK = NOW + timedelta(minutes=75)
EXPIRES = int(NOW.timestamp()) + 48 * 3600


@pytest.fixture
def client(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        c.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
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
        c.create_table(
            TableName=SOURCES,
            KeySchema=[{"AttributeName": "source_file_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "source_file_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield c


@pytest.fixture
def store(client) -> AdvancedGenerationStore:
    return AdvancedGenerationStore(
        region=REGION,
        table_name=TABLE,
        source_files_table_name=SOURCES,
        client=client,
    )


def _put_source(client, owner: str = "sub-a", status: str = "ready", **extra) -> str:
    sf = SourceFile.create_new(
        owner_sub=owner, filename="a.csv", size_bytes=10, part_count=1, now=NOW
    )
    item: dict[str, Any] = {
        "source_file_id": {"S": sf.source_file_id},
        "owner_sub": {"S": owner},
        "status": {"S": status},
    }
    for name, value in extra.items():
        item[name] = {"S": value}
    client.put_item(TableName=SOURCES, Item=item)
    return sf.source_file_id


def _source_item(client, source_file_id: str) -> dict[str, Any]:
    return client.get_item(
        TableName=SOURCES, Key={"source_file_id": {"S": source_file_id}}
    )["Item"]


def _generation(source_id: str, owner: str = "sub-a") -> AdvancedGeneration:
    return AdvancedGeneration.create_new(
        owner_sub=owner,
        source_file_ids=[source_id],
        purpose="",
        persona_count=3,
        now=NOW,
        ttl_seconds=48 * 3600,
    )


def _start(store, client, owner: str = "sub-a") -> AdvancedGeneration:
    gen = _generation(_put_source(client, owner), owner)
    store.create_with_lock(gen, lock_until=LOCK, in_use_until=LOCK, now=NOW)
    return gen


def _count_items(client) -> int:
    return int(client.scan(TableName=TABLE, Select="COUNT")["Count"])


class TestCreateWithLock:
    def test_writes_lock_generation_and_in_use_mark(self, store, client):
        gen = _start(store, client)

        assert store.get(gen.generation_id) == gen
        lock = store.get_lock("sub-a")
        assert lock is not None and lock.generation_id == gen.generation_id
        assert lock.lock_until == LOCK
        mark = _source_item(client, gen.source_file_ids[0])
        assert mark["in_use_by"]["S"] == gen.generation_id

    def test_a_second_generation_of_the_same_owner_writes_nothing(self, store, client):
        _start(store, client)
        before = _count_items(client)
        source_id = _put_source(client)

        with pytest.raises(GenerationLockHeld) as exc:
            store.create_with_lock(
                _generation(source_id), lock_until=LOCK, in_use_until=LOCK, now=NOW
            )

        assert exc.value.code is ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING
        assert _count_items(client) == before
        assert "in_use_by" not in _source_item(client, source_id)

    def test_a_lock_past_lock_until_can_be_taken(self, store, client):
        _start(store, client)
        later = LOCK + timedelta(seconds=1)
        gen = _generation(_put_source(client))

        store.create_with_lock(
            gen, lock_until=later + timedelta(minutes=75), in_use_until=later, now=later
        )

        lock = store.get_lock("sub-a")
        assert lock is not None and lock.generation_id == gen.generation_id

    @pytest.mark.parametrize(
        "source",
        [
            {"owner": "sub-b"},
            {"status": "inspecting"},
            {"in_use_by": "gen-x", "in_use_until": LOCK.isoformat()},
        ],
    )
    def test_a_source_that_is_not_the_owners_free_ready_table_writes_nothing(
        self, store, client, source
    ):
        source_id = _put_source(client, **source)

        with pytest.raises(GenerationSourceUnavailable) as exc:
            store.create_with_lock(
                _generation(source_id), lock_until=LOCK, in_use_until=LOCK, now=NOW
            )

        assert exc.value.code is ErrorCode.GENERATION_SOURCE_NOT_FOUND
        assert _count_items(client) == 0
        assert store.get_lock("sub-a") is None

    def test_a_missing_source_writes_nothing(self, store, client):
        with pytest.raises(GenerationSourceUnavailable):
            store.create_with_lock(
                _generation("missing"), lock_until=LOCK, in_use_until=LOCK, now=NOW
            )
        assert _count_items(client) == 0


class TestTransitions:
    def test_transition_is_conditioned_on_the_current_state(self, store, client):
        gen = _start(store, client)

        assert store.transition(
            gen.generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.PLANNING,
            updates={"part_counts": {"prepare_reports": 1}},
        )
        assert not store.transition(
            gen.generation_id, GenerationStatus.PREPARING, GenerationStatus.FAILED
        )

        current = store.get(gen.generation_id)
        assert current is not None
        assert current.status is GenerationStatus.PLANNING
        assert current.part_counts == {"prepare_reports": 1}

    def test_transition_of_a_missing_generation_is_dropped(self, store):
        assert not store.transition(
            "missing", GenerationStatus.PREPARING, GenerationStatus.PLANNING
        )

    def test_heartbeat_only_while_running(self, store, client):
        gen = _start(store, client)
        beat = NOW + timedelta(seconds=30)

        assert store.heartbeat(gen.generation_id, now=beat)
        current = store.get(gen.generation_id)
        assert current is not None and current.heartbeat_at == beat

        store.transition(
            gen.generation_id, GenerationStatus.PREPARING, GenerationStatus.FAILED
        )
        assert not store.heartbeat(gen.generation_id, now=beat)


class TestEvents:
    def test_events_are_numbered_from_1(self, store, client):
        gen = _start(store, client)

        events = [
            store.append_event(
                gen.generation_id,
                EventType.STAGE,
                summary=f"s{i}",
                now=NOW,
                expires_at=EXPIRES,
            )
            for i in range(3)
        ]

        assert [e.sequence for e in events] == [1, 2, 3]

    def test_read_events_after_the_last_seen_sequence(self, store, client):
        gen = _start(store, client)
        for i in range(12):
            store.append_event(
                gen.generation_id,
                EventType.PROGRESS,
                summary=str(i),
                now=NOW,
                expires_at=EXPIRES,
            )
        store.put_result_parts(
            gen.generation_id,
            {ResultPartName.SEGMENTS: [{"id": "seg-1"}]},
            expires_at=EXPIRES,
            max_part_bytes=100 * 1024,
            max_total_bytes=2 * 1024 * 1024,
        )

        everything = store.read_events(gen.generation_id)
        after_ten = store.read_events(gen.generation_id, after_sequence=10)

        assert [e.sequence for e in everything] == list(range(1, 13))
        # Lexical order must not put 10 before 2, nor read the GENERATION / PART items.
        assert [e.sequence for e in after_ten] == [11, 12]
        assert store.read_events(gen.generation_id, after_sequence=12) == []

    def test_events_of_another_generation_are_not_read(self, store, client):
        gen = _start(store, client)
        store.append_event(
            gen.generation_id,
            EventType.STAGE,
            summary=None,
            now=NOW,
            expires_at=EXPIRES,
        )

        assert store.read_events("other") == []


class TestResultParts:
    def test_parts_are_split_and_read_back_in_order(self, store, client):
        gen = _start(store, client)
        items = [{"id": f"ev-{i}", "claim": "x" * 400} for i in range(30)]

        counts = store.put_result_parts(
            gen.generation_id,
            {ResultPartName.EVIDENCE: items, ResultPartName.SEGMENTS: [{"id": "s"}]},
            expires_at=EXPIRES,
            max_part_bytes=4 * 1024,
            max_total_bytes=2 * 1024 * 1024,
        )

        assert counts["evidence"] > 1
        assert counts["segments"] == 1
        assert store.read_result_parts(gen.generation_id, ResultPartName.EVIDENCE) == (
            items
        )
        assert (
            store.read_result_parts(gen.generation_id, ResultPartName.PREPARE_REPORTS)
            == []
        )

    def test_one_item_larger_than_a_part_is_refused(self, store, client):
        gen = _start(store, client)

        with pytest.raises(GenerationResultTooLarge) as exc:
            store.put_result_parts(
                gen.generation_id,
                {ResultPartName.EVIDENCE: [{"claim": "x" * 5000}]},
                expires_at=EXPIRES,
                max_part_bytes=4 * 1024,
                max_total_bytes=2 * 1024 * 1024,
            )
        assert exc.value.code is ErrorCode.GENERATION_RESULT_TOO_LARGE

    def test_the_total_limit_is_checked_before_anything_is_written(self, store, client):
        gen = _start(store, client)
        before = _count_items(client)

        with pytest.raises(GenerationResultTooLarge):
            store.put_result_parts(
                gen.generation_id,
                {
                    ResultPartName.SEGMENTS: [{"x": "a" * 3000}],
                    ResultPartName.EVIDENCE: [{"x": "b" * 3000}],
                },
                expires_at=EXPIRES,
                max_part_bytes=4 * 1024,
                max_total_bytes=5 * 1024,
            )
        assert _count_items(client) == before


class TestFinish:
    def test_finish_releases_the_lock_and_marks_and_writes_the_end(self, store, client):
        gen = _start(store, client)
        store.transition(
            gen.generation_id, GenerationStatus.PREPARING, GenerationStatus.FAILED
        )
        end = NOW + timedelta(minutes=5)
        expires = int(end.timestamp()) + 24 * 3600

        outcome = store.finish(
            gen.generation_id,
            "sub-a",
            gen.source_file_ids,
            now=end,
            expires_at=expires,
        )

        assert outcome.lock_released and outcome.marks_released
        assert outcome.finished_written
        assert store.get_lock("sub-a") is None
        mark = _source_item(client, gen.source_file_ids[0])
        assert "in_use_by" not in mark and "in_use_until" not in mark
        assert mark["last_used_at"]["S"] == end.isoformat(timespec="microseconds")
        finished = store.get(gen.generation_id)
        assert finished is not None
        assert finished.finished_at == end and finished.expires_at == expires

    def test_finish_does_not_touch_a_lock_of_another_generation(self, store, client):
        first = _start(store, client)
        later = LOCK + timedelta(seconds=1)
        second = _generation(_put_source(client))
        store.create_with_lock(
            second, lock_until=later + timedelta(hours=1), in_use_until=later, now=later
        )

        outcome = store.finish(
            first.generation_id,
            "sub-a",
            first.source_file_ids,
            now=later,
            expires_at=EXPIRES,
        )

        assert not outcome.lock_released
        lock = store.get_lock("sub-a")
        assert lock is not None and lock.generation_id == second.generation_id
