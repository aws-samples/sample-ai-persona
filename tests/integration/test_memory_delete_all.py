"""MemoryService.delete_all（ペルソナの長期記憶の一括削除）の結合テスト。

performance-design.md §2、BR3.2、BR3.3、NFR2.11、NFR1.42。AgentCore Memory は moto に
ないため、削除専用の boto3 クライアントを botocore の Stubber で差し替える。時計は
差し替え、各 API 呼び出しの後に進める（time.sleep を使わない）。
"""

from typing import Any
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from src.services.memory.memory_service import (
    CALL_BUDGET_SECONDS,
    DELETE_CLIENT_CONFIG,
    MemoryService,
)
from src.services.memory.semantic_strategy import SemanticStrategy
from src.services.memory.summary_strategy import SummaryStrategy

MEMORY_ID = "memory-u5-delete-all"
REGION = "us-east-1"


def _record_id(n: int) -> str:
    return f"mem-{n:040d}"


class FakeClock:
    """Monotonic clock advanced by ``step`` after every API call."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        return self.now

    def advance(self, **_: Any) -> None:
        self.now += self.step


@pytest.fixture
def delete_client(monkeypatch) -> Any:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    return boto3.client(
        "bedrock-agentcore", region_name=REGION, config=DELETE_CLIENT_CONFIG
    )


def _service(delete_client: Any, namespaces: list[list[str]]) -> MemoryService:
    summary = Mock(spec=SummaryStrategy)
    summary.namespaces_for.return_value = namespaces[0]
    service = MemoryService(
        memory_id=MEMORY_ID,
        region=REGION,
        strategy=summary,
        validate_connection=False,
        delete_client=delete_client,
    )
    # never the strategy built from the environment (only the ones given here)
    service._semantic_strategy = None
    if len(namespaces) > 1:
        semantic = Mock(spec=SemanticStrategy)
        semantic.namespaces_for.return_value = namespaces[1]
        service._semantic_strategy = semantic
    return service


def _list(
    stub: Stubber,
    namespace: str,
    ids: list[int],
    token: str | None,
    next_token: str | None = None,
) -> None:
    params: dict[str, Any] = {
        "memoryId": MEMORY_ID,
        "namespace": namespace,
        "maxResults": 100,
    }
    if token:
        params["nextToken"] = token
    response: dict[str, Any] = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": _record_id(i),
                "content": {"text": "x"},
                "memoryStrategyId": "s",
                "namespaces": [namespace],
                "createdAt": "2026-10-14T00:00:00Z",
            }
            for i in ids
        ]
    }
    if next_token:
        response["nextToken"] = next_token
    stub.add_response("list_memory_records", response, params)


def _delete(stub: Stubber, n: int) -> None:
    stub.add_response(
        "delete_memory_record",
        {"memoryRecordId": _record_id(n)},
        {"memoryId": MEMORY_ID, "memoryRecordId": _record_id(n)},
    )


def test_delete_client_has_short_timeouts_and_no_botocore_retry():
    assert DELETE_CLIENT_CONFIG.connect_timeout == 2
    assert DELETE_CLIENT_CONFIG.read_timeout == 5
    # one attempt in total: botocore never retries (delete_all retries itself)
    assert DELETE_CLIENT_CONFIG.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert CALL_BUDGET_SECONDS == 7.0


def test_delete_client_is_built_from_the_given_session():
    session = Mock()
    service = MemoryService(
        memory_id=MEMORY_ID,
        region=REGION,
        strategy=Mock(spec=SummaryStrategy),
        validate_connection=False,
        boto3_session=session,
    )
    service._delete_client()
    session.client.assert_called_once_with(
        "bedrock-agentcore", region_name=REGION, config=DELETE_CLIENT_CONFIG
    )


def test_every_page_of_both_strategies_is_deleted(delete_client):
    service = _service(
        delete_client, [["/s/summary/actors/p1"], ["/s/semantic/actors/p1"]]
    )
    with Stubber(delete_client) as stub:
        _list(stub, "/s/summary/actors/p1", [1, 2], None, next_token="t1")
        _delete(stub, 1)
        _delete(stub, 2)
        _list(stub, "/s/summary/actors/p1", [3], "t1", next_token="t2")
        _delete(stub, 3)
        _list(stub, "/s/summary/actors/p1", [], "t2")
        _list(stub, "/s/semantic/actors/p1", [4], None)
        _delete(stub, 4)

        result = service.delete_all("p1", deadline=30.0, clock=FakeClock())

        stub.assert_no_pending_responses()
    assert (result.deleted, result.remaining, result.timed_out) == (4, 0, False)
    assert result.complete


def test_stops_before_a_call_when_less_than_seven_seconds_are_left(delete_client):
    service = _service(delete_client, [["/ns"]])
    clock = FakeClock(step=12.0)
    delete_client.meta.events.register("after-call.bedrock-agentcore", clock.advance)
    with Stubber(delete_client) as stub:
        _list(stub, "/ns", [1, 2, 3], None)
        # list: 12 s used; delete 1: 24 s used; 6 s left < 7 s -> no more calls
        _delete(stub, 1)

        result = service.delete_all("p1", deadline=30.0, clock=clock)

        stub.assert_no_pending_responses()
    assert result.timed_out
    assert result.deleted == 1
    assert result.remaining == 2  # the only page was read
    assert not result.complete


def test_unread_pages_make_remaining_unknown(delete_client):
    service = _service(delete_client, [["/ns"]])
    clock = FakeClock(step=12.0)
    delete_client.meta.events.register("after-call.bedrock-agentcore", clock.advance)
    with Stubber(delete_client) as stub:
        _list(stub, "/ns", [1], None, next_token="more")
        _delete(stub, 1)

        result = service.delete_all("p1", deadline=30.0, clock=clock)

    assert result.timed_out
    assert result.remaining is None


def test_a_failed_delete_is_retried_once(delete_client):
    service = _service(delete_client, [["/ns"]])
    with Stubber(delete_client) as stub:
        _list(stub, "/ns", [1], None)
        stub.add_client_error("delete_memory_record", "ThrottlingException")
        _delete(stub, 1)

        result = service.delete_all("p1", deadline=30.0, clock=FakeClock())

        stub.assert_no_pending_responses()
    assert result.deleted == 1
    assert result.complete


def test_a_second_failure_is_reported_not_raised(delete_client):
    service = _service(delete_client, [["/ns"]])
    with Stubber(delete_client) as stub:
        _list(stub, "/ns", [1, 2], None)
        stub.add_client_error("delete_memory_record", "ThrottlingException")
        stub.add_client_error("delete_memory_record", "InternalServerException")

        result = service.delete_all("p1", deadline=30.0, clock=FakeClock())

    assert result.deleted == 0
    # the exception class name only (botocore's modeled error class)
    assert result.error_type == "InternalServerException"
    assert not result.timed_out
    assert result.remaining == 2


def test_a_failed_list_is_reported_with_unknown_remaining(delete_client):
    service = _service(delete_client, [["/ns"]])
    with Stubber(delete_client) as stub:
        stub.add_client_error("list_memory_records", "AccessDeniedException")

        result = service.delete_all("p1", deadline=30.0, clock=FakeClock())

    assert result.error_type == "AccessDeniedException"
    assert result.remaining is None


def test_already_deleted_records_count_as_done(delete_client):
    service = _service(delete_client, [["/ns"]])
    with Stubber(delete_client) as stub:
        _list(stub, "/ns", [1], None)
        stub.add_client_error("delete_memory_record", "ResourceNotFoundException")

        result = service.delete_all("p1", deadline=30.0, clock=FakeClock())

    assert result.complete


def test_disabled_service_does_nothing(delete_client):
    service = _service(delete_client, [["/ns"]])
    service._enabled = False
    with Stubber(delete_client) as stub:
        result = service.delete_all("p1", deadline=30.0, clock=FakeClock())
        stub.assert_no_pending_responses()
    assert result.complete
    assert result.deleted == 0


def _get(stub: Stubber, n: int, namespace: str) -> None:
    stub.add_response(
        "get_memory_record",
        {
            "memoryRecord": {
                "memoryRecordId": _record_id(n),
                "content": {"text": "x"},
                "memoryStrategyId": "s",
                "namespaces": [namespace],
                "createdAt": "2026-10-14T00:00:00Z",
            }
        },
        {"memoryId": MEMORY_ID, "memoryRecordId": _record_id(n)},
    )


class TestDeleteOwnedMemory:
    """One record is deleted only when it is under the actor's namespaces (R-01).

    BR4.1, NFR1.23, AC4.2.4: persona A's ID with persona B's record ID must not
    reach B's record.
    """

    def test_own_record_is_deleted(self, delete_client):
        service = _service(delete_client, [["/s/summary/actors/p1"]])
        with Stubber(delete_client) as stub:
            _get(stub, 1, "/s/summary/actors/p1/sessions/d1")
            _delete(stub, 1)

            assert service.delete_owned_memory("p1", _record_id(1)) is True

            stub.assert_no_pending_responses()

    def test_record_of_another_actor_is_not_deleted(self, delete_client):
        service = _service(delete_client, [["/s/summary/actors/p1"]])
        with Stubber(delete_client) as stub:
            _get(stub, 2, "/s/summary/actors/p2/sessions/d1")
            # nothing else is queued: a delete_memory_record call would fail

            assert service.delete_owned_memory("p1", _record_id(2)) is False

            stub.assert_no_pending_responses()

    def test_a_longer_actor_id_with_the_same_prefix_is_another_actor(
        self, delete_client
    ):
        service = _service(delete_client, [["/s/summary/actors/p1"]])
        with Stubber(delete_client) as stub:
            _get(stub, 3, "/s/summary/actors/p10/sessions/d1")

            assert service.delete_owned_memory("p1", _record_id(3)) is False

    def test_record_of_the_second_strategy_is_deleted(self, delete_client):
        service = _service(
            delete_client, [["/s/summary/actors/p1"], ["/s/semantic/actors/p1"]]
        )
        with Stubber(delete_client) as stub:
            _get(stub, 4, "/s/semantic/actors/p1")
            _delete(stub, 4)

            assert service.delete_owned_memory("p1", _record_id(4)) is True

    def test_missing_record_is_not_found(self, delete_client):
        service = _service(delete_client, [["/s/summary/actors/p1"]])
        with Stubber(delete_client) as stub:
            stub.add_client_error("get_memory_record", "ResourceNotFoundException")

            assert service.delete_owned_memory("p1", _record_id(5)) is False

    def test_a_failed_read_raises_the_service_error(self, delete_client):
        from src.services.memory.memory_service import MemoryServiceError

        service = _service(delete_client, [["/s/summary/actors/p1"]])
        with Stubber(delete_client) as stub:
            stub.add_client_error("get_memory_record", "AccessDeniedException")

            with pytest.raises(MemoryServiceError):
                service.delete_owned_memory("p1", _record_id(6))


class TestStrategyDeleteUsesTheActor:
    """Each strategy's ``delete`` checks the record's namespace too (R-01)."""

    @pytest.mark.parametrize("strategy_class", [SummaryStrategy, SemanticStrategy])
    def test_record_of_another_actor_is_not_deleted(self, strategy_class):
        strategy = strategy_class.__new__(strategy_class)
        strategy.memory_id = MEMORY_ID
        strategy.memory_strategy_id = "st-1"
        strategy._client = Mock()
        strategy._client.get_memory_record.return_value = {
            "memoryRecord": {"namespaces": ["/strategies/st-1/actors/p2"]}
        }

        assert strategy.delete(actor_id="p1", memory_id=_record_id(7)) is False
        strategy._client.delete_memory_record.assert_not_called()

    @pytest.mark.parametrize("strategy_class", [SummaryStrategy, SemanticStrategy])
    def test_own_record_is_deleted(self, strategy_class):
        strategy = strategy_class.__new__(strategy_class)
        strategy.memory_id = MEMORY_ID
        strategy.memory_strategy_id = "st-1"
        strategy._client = Mock()
        strategy._client.get_memory_record.return_value = {
            "memoryRecord": {"namespaces": ["/strategies/st-1/actors/p1/sessions/d"]}
        }

        assert strategy.delete(actor_id="p1", memory_id=_record_id(8)) is True
        strategy._client.delete_memory_record.assert_called_once_with(
            memoryId=MEMORY_ID, memoryRecordId=_record_id(8)
        )


class TestStrategyNamespaces:
    def test_summary_namespace_is_the_actor_prefix(self):
        strategy = SummaryStrategy.__new__(SummaryStrategy)
        strategy.memory_strategy_id = "sum-1"
        assert strategy.namespaces_for("p1") == ["/strategies/sum-1/actors/p1"]

    def test_semantic_namespace_is_the_actor_prefix(self):
        strategy = SemanticStrategy.__new__(SemanticStrategy)
        strategy.memory_strategy_id = "sem-1"
        assert strategy.namespaces_for("p1") == ["/strategies/sem-1/actors/p1"]
