"""AgentCore Memory の Summary / Semantic 戦略（src/services/memory/*_strategy.py）。

本物の MemoryClient が持つ bedrock-agentcore クライアントに botocore Stubber を当て、
送るリクエストの形と、応答・エラーの扱いを確かめる。実 AWS には触れない。
"""

from datetime import datetime, timezone
from typing import Any, Iterator
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from src.services.memory.semantic_strategy import SemanticStrategy
from src.services.memory.summary_strategy import MemoryOperationError, SummaryStrategy

MEMORY_ID = "memory-strategy-test"
CREATED = datetime(2026, 10, 14, tzinfo=timezone.utc)


def rid(name: str) -> str:
    """A memoryRecordId long enough for the botocore model (40-50 chars)."""
    return "mem-" + name.ljust(40, "0")


STRATEGIES = {
    "summary": (SummaryStrategy, "sum-1"),
    "semantic": (SemanticStrategy, "sem-1"),
}


@pytest.fixture(autouse=True)
def _fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture(params=sorted(STRATEGIES))
def strategy(request: pytest.FixtureRequest) -> SummaryStrategy | SemanticStrategy:
    cls, strategy_id = STRATEGIES[request.param]
    return cls(memory_id=MEMORY_ID, region="us-east-1", memory_strategy_id=strategy_id)


@pytest.fixture
def stub(strategy: Any) -> Iterator[Stubber]:
    with Stubber(strategy._client.gmdp_client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _prefix(strategy: Any, actor: str = "p1") -> str:
    return f"/strategies/{strategy.memory_strategy_id}/actors/{actor}"


def _summary(
    strategy: Any,
    record_id: str,
    text: str = "t",
    namespace: str | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "memoryRecordId": record_id,
        "content": {"text": text},
        "memoryStrategyId": strategy.memory_strategy_id,
        "namespaces": [namespace or _prefix(strategy)],
        "createdAt": CREATED,
    }
    if score is not None:
        record["score"] = score
    return record


def _error(stub: Stubber, operation: str, code: str) -> None:
    stub.add_client_error(operation, service_error_code=code, service_message="m")


# --- save (create_event) --------------------------------------------------


def test_save_sends_the_content_as_an_assistant_event(strategy, stub):
    stub.add_response(
        "create_event",
        {
            "event": {
                "memoryId": MEMORY_ID,
                "actorId": "p1",
                "sessionId": "d1",
                "eventId": "ev-1",
                "eventTimestamp": CREATED,
                "payload": [],
            }
        },
        {
            "memoryId": MEMORY_ID,
            "actorId": "p1",
            "sessionId": "d1",
            "eventTimestamp": ANY,
            "payload": [
                {"conversational": {"content": {"text": "要約"}, "role": "ASSISTANT"}}
            ],
        },
    )

    assert strategy.save("p1", "d1", "要約") == "ev-1"


@pytest.mark.parametrize("code", ["ThrottledException", "AccessDeniedException"])
def test_save_wraps_api_errors(strategy, stub, code):
    _error(stub, "create_event", code)
    with pytest.raises(MemoryOperationError):
        strategy.save("p1", "d1", "x")


# --- retrieve (retrieve_memory_records) -----------------------------------


def test_retrieve_searches_the_actor_namespace(strategy, stub):
    stub.add_response(
        "retrieve_memory_records",
        {
            "memoryRecordSummaries": [
                _summary(strategy, rid("r1"), "a"),
                _summary(strategy, rid("r2"), "b"),
            ]
        },
        {
            "memoryId": MEMORY_ID,
            "namespace": _prefix(strategy),
            "searchCriteria": {"searchQuery": "価格", "topK": 3},
        },
    )

    entries = strategy.retrieve("p1", "価格", top_k=3)

    assert [(e.id, e.content, e.actor_id) for e in entries] == [
        (rid("r1"), "a", "p1"),
        (rid("r2"), "b", "p1"),
    ]
    assert entries[0].created_at == CREATED


def test_retrieve_with_no_records_is_empty(strategy, stub):
    stub.add_response("retrieve_memory_records", {"memoryRecordSummaries": []})
    assert strategy.retrieve("p1", "q") == []


def test_retrieve_api_error_yields_no_memories(strategy, stub):
    # MemoryClient.retrieve_memories が ClientError を握りつぶして [] を返すため、
    # 戦略側の ClientError 分岐には届かない
    _error(stub, "retrieve_memory_records", "ThrottledException")
    assert strategy.retrieve("p1", "q") == []


@pytest.mark.parametrize(
    "code, expected", [("ResourceNotFoundException", []), ("ThrottledException", None)]
)
def test_retrieve_client_error_from_the_memory_client(strategy, code, expected):
    strategy._client = Mock()
    strategy._client.retrieve_memories.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "m"}}, "RetrieveMemoryRecords"
    )
    if expected is None:
        with pytest.raises(MemoryOperationError):
            strategy.retrieve("p1", "q")
    else:
        assert strategy.retrieve("p1", "q") == expected


def test_retrieve_unexpected_error_is_wrapped(strategy):
    strategy._client = Mock()
    strategy._client.retrieve_memories.side_effect = RuntimeError("boom")
    with pytest.raises(MemoryOperationError):
        strategy.retrieve("p1", "q")


def test_malformed_records_are_skipped(strategy):
    strategy._client = Mock()
    strategy._client.retrieve_memories.return_value = [
        {"memoryRecordId": rid("ok"), "content": {"text": "a"}, "createdAt": CREATED},
        {"memoryRecordId": rid("bad-date"), "createdAt": "not-a-date"},
        {
            "memoryRecordId": rid("iso"),
            "content": "raw",
            "createdAt": "2026-10-14T00:00Z",
        },
    ]

    entries = strategy.retrieve("p1", "q")

    assert [e.id for e in entries] == [rid("ok"), rid("iso")]
    assert entries[1].content == "raw"
    assert entries[1].created_at == CREATED


@pytest.mark.xfail(
    strict=True,
    reason=(
        "API の関連度は score だが relevanceScore を読むため relevance_score が常に None で、"
        "関連度順の並べ替えも効かない（summary_strategy.py:435, semantic_strategy.py:428）"
    ),
)
def test_retrieve_orders_by_the_api_score(strategy, stub):
    stub.add_response(
        "retrieve_memory_records",
        {
            "memoryRecordSummaries": [
                _summary(strategy, rid("low"), score=0.2),
                _summary(strategy, rid("high"), score=0.9),
            ]
        },
    )
    entries = strategy.retrieve("p1", "q")
    assert [(e.id, e.relevance_score) for e in entries] == [
        (rid("high"), 0.9),
        (rid("low"), 0.2),
    ]


# --- list_all (list_memory_records) ---------------------------------------


def test_list_all_lists_the_actor_namespace(strategy, stub):
    stub.add_response(
        "list_memory_records",
        {"memoryRecordSummaries": [_summary(strategy, rid("r1"), "a")]},
        {"memoryId": MEMORY_ID, "namespace": _prefix(strategy, "p9")},
    )

    entries = strategy.list_all("p9")

    assert [(e.id, e.actor_id, e.content) for e in entries] == [(rid("r1"), "p9", "a")]
    assert entries[0].metadata["strategy_type"] in ("summary", "semantic")


def test_list_all_with_no_records_is_empty(strategy, stub):
    stub.add_response("list_memory_records", {"memoryRecordSummaries": []})
    assert strategy.list_all("p1") == []


def test_list_all_missing_namespace_is_empty(strategy, stub):
    _error(stub, "list_memory_records", "ResourceNotFoundException")
    assert strategy.list_all("p1") == []


@pytest.mark.parametrize("code", ["ThrottledException", "AccessDeniedException"])
def test_list_all_other_api_errors_are_wrapped(strategy, stub, code):
    _error(stub, "list_memory_records", code)
    with pytest.raises(MemoryOperationError):
        strategy.list_all("p1")


def test_list_all_unexpected_error_is_wrapped(strategy):
    strategy._client = Mock()
    strategy._client.list_memory_records.side_effect = RuntimeError("boom")
    with pytest.raises(MemoryOperationError):
        strategy.list_all("p1")


def test_list_all_follows_pagination(strategy, stub):
    """Every page is read, so the memory screen and per-strategy deletes see all records."""
    first = {"memoryId": MEMORY_ID, "namespace": _prefix(strategy)}
    stub.add_response(
        "list_memory_records",
        {
            "memoryRecordSummaries": [_summary(strategy, rid("r1"))],
            "nextToken": "t2",
        },
        first,
    )
    stub.add_response(
        "list_memory_records",
        {"memoryRecordSummaries": [_summary(strategy, rid("r2"))]},
        {**first, "nextToken": "t2"},
    )

    entries = strategy.list_all("p1")

    assert [e.id for e in entries] == [rid("r1"), rid("r2")]


# --- delete (get_memory_record + delete_memory_record) --------------------


def _record(strategy: Any, record_id: str, namespace: str) -> dict[str, Any]:
    return {"memoryRecord": _summary(strategy, record_id, namespace=namespace)}


def test_delete_removes_a_record_of_the_actor(strategy, stub):
    params = {"memoryId": MEMORY_ID, "memoryRecordId": rid("r1")}
    stub.add_response(
        "get_memory_record",
        _record(strategy, rid("r1"), _prefix(strategy) + "/sessions/d1"),
        params,
    )
    stub.add_response("delete_memory_record", {"memoryRecordId": rid("r1")}, params)

    assert strategy.delete("p1", rid("r1")) is True


@pytest.mark.parametrize("other", ["p2", "p10"])
def test_delete_refuses_a_record_of_another_actor(strategy, stub, other):
    # delete_memory_record を積んでいないので、呼ばれれば Stubber が失敗する
    stub.add_response(
        "get_memory_record", _record(strategy, rid("r1"), _prefix(strategy, other))
    )
    assert strategy.delete("p1", rid("r1")) is False


def test_delete_of_a_missing_record_is_false(strategy, stub):
    _error(stub, "get_memory_record", "ResourceNotFoundException")
    assert strategy.delete("p1", rid("r1")) is False


def test_delete_api_error_is_wrapped(strategy, stub):
    stub.add_response(
        "get_memory_record", _record(strategy, rid("r1"), _prefix(strategy))
    )
    _error(stub, "delete_memory_record", "ThrottledException")
    with pytest.raises(MemoryOperationError):
        strategy.delete("p1", rid("r1"))


def test_delete_unexpected_error_is_wrapped(strategy):
    strategy._client = Mock()
    strategy._client.get_memory_record.side_effect = RuntimeError("boom")
    with pytest.raises(MemoryOperationError):
        strategy.delete("p1", rid("r1"))


# --- client initialisation ------------------------------------------------


@pytest.mark.parametrize("cls", [SummaryStrategy, SemanticStrategy])
@pytest.mark.parametrize("failure", [ImportError("no module"), RuntimeError("bad")])
def test_client_init_failure_raises_memory_operation_error(monkeypatch, cls, failure):
    import bedrock_agentcore.memory as agentcore_memory

    def broken(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(agentcore_memory, "MemoryClient", broken)
    with pytest.raises(MemoryOperationError) as exc_info:
        cls(memory_id=MEMORY_ID, memory_strategy_id="s-1")
    assert exc_info.value.__cause__ is failure


@pytest.mark.parametrize(
    "cls, attr", [(SummaryStrategy, "SUMMARY"), (SemanticStrategy, "SEMANTIC")]
)
def test_strategy_id_defaults_to_config(monkeypatch, cls, attr):
    from src.config import config

    monkeypatch.setattr(config, f"{attr}_MEMORY_STRATEGY_ID", "from-config")
    assert cls(memory_id=MEMORY_ID).memory_strategy_id == "from-config"


# --- SummaryStrategy only -------------------------------------------------


@pytest.mark.parametrize(
    "namespace, session_id",
    [
        ("/strategies/sum-1/actors/p1/sessions/d1", "d1"),
        ("strategies/sum-1/actors/p1/sessions/d1/", "d1"),
        ("/summaries/p1/d-old", "d-old"),
        ("/strategies/sum-1/actors/p1", ""),
        ("/strategies/sum-1/people/p1/sessions/d1", ""),
        ("", ""),
    ],
)
def test_summary_session_id_is_read_from_the_namespace(namespace, session_id):
    strategy = SummaryStrategy(memory_id=MEMORY_ID, memory_strategy_id="sum-1")
    assert strategy._extract_session_id_from_namespace(namespace) == session_id


def test_summary_build_namespace_includes_the_session():
    strategy = SummaryStrategy(memory_id=MEMORY_ID, memory_strategy_id="sum-1")
    assert (
        strategy._build_namespace("p1", "d1")
        == "/strategies/sum-1/actors/p1/sessions/d1"
    )
    assert strategy.namespaces_for("p1") == ["/strategies/sum-1/actors/p1"]


def test_summary_save_unexpected_error_is_wrapped():
    strategy = SummaryStrategy(memory_id=MEMORY_ID, memory_strategy_id="sum-1")
    strategy._client = Mock()
    strategy._client.create_event.side_effect = ValueError("bad role")
    with pytest.raises(MemoryOperationError):
        strategy.save("p1", "d1", "x")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "API は namespaces（配列）を返すが namespace を読むため、"
        "session_id が常に空になる（summary_strategy.py:419-420）"
    ),
)
def test_summary_session_id_comes_from_the_api_namespaces():
    strategy = SummaryStrategy(memory_id=MEMORY_ID, memory_strategy_id="sum-1")
    with Stubber(strategy._client.gmdp_client) as stub:
        stub.add_response(
            "list_memory_records",
            {
                "memoryRecordSummaries": [
                    _summary(
                        strategy,
                        rid("r1"),
                        namespace="/strategies/sum-1/actors/p1/sessions/d1",
                    )
                ]
            },
        )
        entries = strategy.list_all("p1")
    assert entries[0].session_id == "d1"


# --- SemanticStrategy only ------------------------------------------------


@pytest.fixture
def semantic() -> SemanticStrategy:
    return SemanticStrategy(
        memory_id=MEMORY_ID, region="us-east-1", memory_strategy_id="sem-1"
    )


def test_semantic_without_strategy_id_is_inert(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config, "SEMANTIC_MEMORY_STRATEGY_ID", None)
    strategy = SemanticStrategy(memory_id=MEMORY_ID)

    assert strategy._client is None
    assert strategy.retrieve("p1", "q") == []
    assert strategy.list_all("p1") == []
    assert strategy.delete("p1", rid("r1")) is False
    with pytest.raises(MemoryOperationError):
        strategy.save("p1", "d1", "x")
    with pytest.raises(MemoryOperationError):
        strategy.save_directly_to_ltm("p1", "x")


def test_semantic_save_unexpected_error_is_wrapped(semantic):
    semantic._client = Mock()
    semantic._client.create_event.side_effect = ValueError("bad role")
    with pytest.raises(MemoryOperationError):
        semantic.save("p1", "d1", "x")


@pytest.fixture
def ltm_stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    """Stub the data-plane client that save_directly_to_ltm creates itself."""
    client = boto3.client("bedrock-agentcore", region_name="us-east-1")
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: client)
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _ltm_params() -> dict[str, Any]:
    return {
        "memoryId": MEMORY_ID,
        "records": [
            {
                "content": {"text": "知識"},
                "namespaces": ["/strategies/sem-1/actors/p1"],
                "requestIdentifier": ANY,
                "timestamp": ANY,
                "memoryStrategyId": "sem-1",
            }
        ],
    }


def test_save_directly_to_ltm_writes_to_the_actor_namespace(semantic, ltm_stub):
    ltm_stub.add_response(
        "batch_create_memory_records",
        {
            "successfulRecords": [
                {"memoryRecordId": rid("rec-1"), "status": "SUCCEEDED"}
            ],
            "failedRecords": [],
        },
        _ltm_params(),
    )
    assert semantic.save_directly_to_ltm("p1", "知識") == rid("rec-1")


def test_save_directly_to_ltm_failed_record_raises(semantic, ltm_stub):
    ltm_stub.add_response(
        "batch_create_memory_records",
        {
            "successfulRecords": [],
            "failedRecords": [
                {
                    "memoryRecordId": rid("x"),
                    "status": "FAILED",
                    "errorMessage": "too long",
                }
            ],
        },
    )
    with pytest.raises(MemoryOperationError):
        semantic.save_directly_to_ltm("p1", "知識")


def test_save_directly_to_ltm_with_no_record_returns_the_request_id(semantic, ltm_stub):
    ltm_stub.add_response(
        "batch_create_memory_records", {"successfulRecords": [], "failedRecords": []}
    )
    assert semantic.save_directly_to_ltm("p1", "知識").startswith("manual-")


@pytest.mark.parametrize("code", ["ValidationException", "ThrottledException"])
def test_save_directly_to_ltm_api_error_is_wrapped(semantic, ltm_stub, code):
    _error(ltm_stub, "batch_create_memory_records", code)
    with pytest.raises(MemoryOperationError):
        semantic.save_directly_to_ltm("p1", "知識")


def test_save_directly_to_ltm_unexpected_error_is_wrapped(semantic, monkeypatch):
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no endpoint")

    monkeypatch.setattr(boto3, "client", broken)
    with pytest.raises(MemoryOperationError):
        semantic.save_directly_to_ltm("p1", "知識")
