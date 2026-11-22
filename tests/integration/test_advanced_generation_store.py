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
    ControlAction,
    ControlRequest,
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

    # --- U2: 使った日と期限の延長（BR13.3、NFR5.28） ---

    def test_the_start_writes_the_last_use_and_the_new_expiry(self, store, client):
        source_id = _put_source(client)
        source_expires = NOW + timedelta(days=7)

        store.create_with_lock(
            _generation(source_id),
            lock_until=LOCK,
            in_use_until=LOCK,
            now=NOW,
            source_expires_at=source_expires,
        )

        mark = _source_item(client, source_id)
        assert mark["last_used_at"]["S"] == NOW.isoformat(timespec="microseconds")
        assert mark["expires_at"] == {"N": str(int(source_expires.timestamp()))}

    @pytest.mark.parametrize("status", ["deleting", "error"])
    def test_a_file_that_is_not_ready_cannot_start(self, store, client, status):
        source_id = _put_source(client, status=status)

        with pytest.raises(GenerationSourceUnavailable):
            store.create_with_lock(
                _generation(source_id),
                lock_until=LOCK,
                in_use_until=LOCK,
                now=NOW,
                source_expires_at=NOW + timedelta(days=7),
            )
        assert "in_use_by" not in _source_item(client, source_id)

    def test_an_expired_file_cannot_start(self, store, client):
        source_id = _put_source(client)
        client.update_item(
            TableName=SOURCES,
            Key={"source_file_id": {"S": source_id}},
            UpdateExpression="SET expires_at = :e",
            ExpressionAttributeValues={":e": {"N": str(int(NOW.timestamp()))}},
        )

        with pytest.raises(GenerationSourceUnavailable):
            store.create_with_lock(
                _generation(source_id),
                lock_until=LOCK,
                in_use_until=LOCK,
                now=NOW,
                source_expires_at=NOW + timedelta(days=7),
            )
        assert _count_items(client) == 0

    def test_a_file_within_its_expiry_can_start(self, store, client):
        source_id = _put_source(client)
        client.update_item(
            TableName=SOURCES,
            Key={"source_file_id": {"S": source_id}},
            UpdateExpression="SET expires_at = :e",
            ExpressionAttributeValues={
                ":e": {"N": str(int((NOW + timedelta(seconds=5)).timestamp()))}
            },
        )

        store.create_with_lock(
            _generation(source_id),
            lock_until=LOCK,
            in_use_until=LOCK,
            now=NOW,
            source_expires_at=NOW + timedelta(days=7),
        )

        assert _source_item(client, source_id)["in_use_by"]["S"]


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

    def test_unless_requested_loses_to_a_pending_cancel(self, store, client):
        # U5 BR31.2 (review fix): ``done`` is never written over a cancel
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING, GenerationStatus.RUNNING)
        store.put_control_request(
            gen.generation_id,
            ControlRequest(action=ControlAction.CANCEL, requested_at=NOW),
            allowed_statuses=[GenerationStatus.RUNNING],
            require_empty=False,
        )

        assert not store.transition(
            gen.generation_id,
            GenerationStatus.RUNNING,
            GenerationStatus.DONE,
            unless_requested=True,
        )

        current = store.get(gen.generation_id)
        assert current is not None and current.status is GenerationStatus.RUNNING
        assert current.control_request is not None

    def test_unless_requested_moves_without_a_request(self, store, client):
        gen = _start(store, client)

        assert store.transition(
            gen.generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.PLANNING,
            unless_requested=True,
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
        # U2 BR13.3: the end only removes the mark; the last use is the start.
        assert mark["last_used_at"]["S"] == NOW.isoformat(timespec="microseconds")
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

    def test_release_holds_leaves_the_end_as_it_is(self, store, client):
        # U5 review fix: a lock left by an ended generation is freed at a start
        gen = _start(store, client)
        store.transition(
            gen.generation_id, GenerationStatus.PREPARING, GenerationStatus.FAILED
        )

        released = store.release_holds(gen.generation_id, "sub-a", gen.source_file_ids)

        assert released == (True, True)
        assert store.get_lock("sub-a") is None
        mark = _source_item(client, gen.source_file_ids[0])
        assert "in_use_by" not in mark
        current = store.get(gen.generation_id)
        assert current is not None and current.finished_at is None

    def test_release_holds_does_not_touch_another_generations(self, store, client):
        first = _start(store, client)
        later = LOCK + timedelta(seconds=1)
        second = _generation(first.source_file_ids[0])
        store.create_with_lock(
            second, lock_until=later + timedelta(hours=1), in_use_until=later, now=later
        )

        released = store.release_holds(
            first.generation_id, "sub-a", first.source_file_ids
        )

        assert released == (False, False)
        lock = store.get_lock("sub-a")
        assert lock is not None and lock.generation_id == second.generation_id
        mark = _source_item(client, first.source_file_ids[0])
        assert mark["in_use_by"]["S"] == second.generation_id


# --- U5: control requests, the reader's end and the plan wait ---------------------

RUNNING_STATES = [s for s in GenerationStatus if not s.is_terminal]


def _to(store, gen, *path: GenerationStatus) -> None:
    previous = GenerationStatus.PREPARING
    for status in path:
        assert store.transition(gen.generation_id, previous, status)
        previous = status


def _request(action: ControlAction, minutes: int = 1, **kw) -> ControlRequest:
    return ControlRequest(
        action=action, requested_at=NOW + timedelta(minutes=minutes), **kw
    )


class TestControlRequests:
    def test_a_cancel_is_written_while_the_generation_runs(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING, GenerationStatus.RUNNING)

        assert store.put_control_request(
            gen.generation_id,
            _request(ControlAction.CANCEL),
            allowed_statuses=RUNNING_STATES,
            require_empty=False,
        )
        current = store.get(gen.generation_id)
        assert current is not None and current.control_request is not None
        assert current.control_request.action is ControlAction.CANCEL

    def test_a_cancel_of_an_ended_generation_is_refused(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.FAILED)

        assert not store.put_control_request(
            gen.generation_id,
            _request(ControlAction.CANCEL),
            allowed_statuses=RUNNING_STATES,
            require_empty=False,
        )
        current = store.get(gen.generation_id)
        assert current is not None and current.control_request is None

    def test_an_approve_needs_the_wait_and_no_pending_request(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING)
        awaiting = [GenerationStatus.AWAITING_PLAN_APPROVAL]
        approve = _request(ControlAction.APPROVE)

        # not waiting yet
        assert not store.put_control_request(
            gen.generation_id, approve, allowed_statuses=awaiting, require_empty=True
        )
        _to_awaiting(store, gen)
        assert store.put_control_request(
            gen.generation_id, approve, allowed_statuses=awaiting, require_empty=True
        )
        # a second one while the first is pending
        assert not store.put_control_request(
            gen.generation_id,
            _request(ControlAction.REPLAN, 2, revised_purpose=""),
            allowed_statuses=awaiting,
            require_empty=True,
        )

    def test_a_cancel_overwrites_a_pending_approve(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING)
        _to_awaiting(store, gen)
        awaiting = [GenerationStatus.AWAITING_PLAN_APPROVAL]
        store.put_control_request(
            gen.generation_id,
            _request(ControlAction.APPROVE),
            allowed_statuses=awaiting,
            require_empty=True,
        )

        assert store.put_control_request(
            gen.generation_id,
            _request(ControlAction.CANCEL, 2),
            allowed_statuses=RUNNING_STATES,
            require_empty=False,
        )
        current = store.get(gen.generation_id)
        assert current is not None and current.control_request is not None
        assert current.control_request.action is ControlAction.CANCEL

    def test_take_removes_the_request_and_moves_on(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING)
        _to_awaiting(store, gen)
        approve = _request(ControlAction.APPROVE)
        store.put_control_request(
            gen.generation_id,
            approve,
            allowed_statuses=[GenerationStatus.AWAITING_PLAN_APPROVAL],
            require_empty=True,
        )

        assert store.take_control_request(
            gen.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.RUNNING,
            approve,
            updates={"waited_seconds": 120},
            removes=("awaiting_since",),
        )
        current = store.get(gen.generation_id)
        assert current is not None
        assert current.status is GenerationStatus.RUNNING
        assert current.control_request is None and current.awaiting_since is None
        assert current.waited_seconds == 120

    def test_take_of_a_request_replaced_by_a_cancel_is_lost(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING)
        _to_awaiting(store, gen)
        approve = _request(ControlAction.APPROVE)
        store.put_control_request(
            gen.generation_id,
            approve,
            allowed_statuses=[GenerationStatus.AWAITING_PLAN_APPROVAL],
            require_empty=True,
        )
        store.put_control_request(
            gen.generation_id,
            _request(ControlAction.CANCEL, 2),
            allowed_statuses=RUNNING_STATES,
            require_empty=False,
        )

        assert not store.take_control_request(
            gen.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.RUNNING,
            approve,
        )
        current = store.get(gen.generation_id)
        assert current is not None
        assert current.status is GenerationStatus.AWAITING_PLAN_APPROVAL

    def test_a_transition_may_remove_attributes(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING)
        _to_awaiting(store, gen)

        assert store.transition(
            gen.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.FAILED,
            updates={"error_code": "generation_timeout"},
            removes=("awaiting_since", "control_request"),
        )
        current = store.get(gen.generation_id)
        assert current is not None and current.awaiting_since is None


class TestEndIfUnchanged:
    def test_ends_while_the_heartbeat_is_the_one_read(self, store, client):
        gen = _start(store, client)
        beat = NOW + timedelta(seconds=30)
        store.heartbeat(gen.generation_id, now=beat)

        assert store.end_if_unchanged(
            gen.generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.FAILED,
            field="heartbeat_at",
            value=beat,
            updates={"error_code": "generation_stalled"},
        )
        current = store.get(gen.generation_id)
        assert current is not None and current.status is GenerationStatus.FAILED
        assert current.error_code == "generation_stalled"

    def test_a_newer_heartbeat_keeps_the_generation(self, store, client):
        gen = _start(store, client)
        beat = NOW + timedelta(seconds=30)
        store.heartbeat(gen.generation_id, now=beat)
        store.heartbeat(gen.generation_id, now=beat + timedelta(seconds=30))

        assert not store.end_if_unchanged(
            gen.generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.FAILED,
            field="heartbeat_at",
            value=beat,
        )

    def test_no_heartbeat_yet_is_matched_as_missing(self, store, client):
        gen = _start(store, client)

        assert store.end_if_unchanged(
            gen.generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.CANCELLED,
            field="heartbeat_at",
            value=None,
        )

    def test_the_reader_and_the_thread_race_and_only_one_wins(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING, GenerationStatus.RUNNING)

        reader = store.end_if_unchanged(
            gen.generation_id,
            GenerationStatus.RUNNING,
            GenerationStatus.FAILED,
            field="heartbeat_at",
            value=None,
        )
        thread = store.transition(
            gen.generation_id, GenerationStatus.RUNNING, GenerationStatus.SYNTHESIZING
        )

        assert [reader, thread] == [True, False]

    def test_the_thread_first_makes_the_reader_lose(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING)
        since = _to_awaiting(store, gen)

        thread = store.transition(
            gen.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.PLANNING,
            removes=("awaiting_since",),
        )
        reader = store.end_if_unchanged(
            gen.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.FAILED,
            field="awaiting_since",
            value=since,
        )

        assert [thread, reader] == [True, False]


def _to_awaiting(store, gen) -> datetime:
    since = NOW + timedelta(minutes=10)
    assert store.enter_awaiting(
        gen.generation_id,
        gen.owner_sub,
        gen.source_file_ids,
        now=since,
        until=LOCK + timedelta(minutes=15),
    )
    return since


class TestEnterAwaiting:
    def test_moves_to_the_wait_and_extends_the_lock_and_the_marks(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING)
        until = LOCK + timedelta(minutes=15)

        since = _to_awaiting(store, gen)

        current = store.get(gen.generation_id)
        assert current is not None
        assert current.status is GenerationStatus.AWAITING_PLAN_APPROVAL
        assert current.awaiting_since == since
        lock = store.get_lock("sub-a")
        assert lock is not None and lock.lock_until == until
        mark = _source_item(client, gen.source_file_ids[0])
        assert mark["in_use_until"]["S"] == until.isoformat(timespec="microseconds")

    def test_writes_the_updates_with_the_move(self, store, client):
        gen = _start(store, client)
        _to(store, gen, GenerationStatus.PLANNING)

        assert store.enter_awaiting(
            gen.generation_id,
            gen.owner_sub,
            gen.source_file_ids,
            now=NOW,
            until=LOCK,
            updates={"replan_count": 1},
        )
        current = store.get(gen.generation_id)
        assert current is not None and current.replan_count == 1

    def test_not_planning_writes_nothing(self, store, client):
        gen = _start(store, client)

        assert not store.enter_awaiting(
            gen.generation_id,
            gen.owner_sub,
            gen.source_file_ids,
            now=NOW,
            until=LOCK + timedelta(hours=1),
        )
        lock = store.get_lock("sub-a")
        assert lock is not None and lock.lock_until == LOCK
        current = store.get(gen.generation_id)
        assert current is not None and current.status is GenerationStatus.PREPARING

    def test_a_lock_of_another_generation_writes_nothing(self, store, client):
        first = _start(store, client)
        _to(store, first, GenerationStatus.PLANNING)
        later = LOCK + timedelta(seconds=1)
        second = _generation(_put_source(client))
        store.create_with_lock(
            second, lock_until=later + timedelta(hours=1), in_use_until=later, now=later
        )

        assert not store.enter_awaiting(
            first.generation_id,
            first.owner_sub,
            first.source_file_ids,
            now=later,
            until=later + timedelta(hours=2),
        )
        lock = store.get_lock("sub-a")
        assert lock is not None and lock.lock_until == later + timedelta(hours=1)
        current = store.get(first.generation_id)
        assert current is not None and current.status is GenerationStatus.PLANNING
