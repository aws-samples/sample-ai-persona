"""PersonaMemoryManager の単体テスト"""

from datetime import datetime
from typing import Any
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from src.models.errors import ErrorCode
from src.services.memory.memory_service import DELETE_CLIENT_CONFIG, MemoryService
from src.services.memory.summary_strategy import SummaryStrategy
from tests.error_helpers import raises_code

from src.managers.persona_memory_manager import (
    PersonaMemoryManager,
    PersonaMemoryManagerError,
)
from src.managers.components.resource_lookup import ResourceLookup
from src.models.authz import AccessTarget, Principal, ResourceType, Role
from src.models.memory import MemoryEntry
from src.models.persona import Persona
from src.services.memory.memory_service import MemoryServiceError
from tests.conftest import ADMIN, MEMBER_A, MEMBER_B
from tests.log_helpers import LogCapture


#: The persona's owner (U5: the memory screen is for managers only, BR4.1)
OWNER = MEMBER_A.principal()

#: AgentCore Memory IDs for the Stubber cases (the API needs 12+ / 40+ chars)
STUB_MEMORY_ID = "memory-u5-r01-test"
A_RECORD_ID = "mem-" + "a" * 40
B_RECORD_ID = "mem-" + "b" * 40


def _real_memory_service(monkeypatch) -> tuple[MemoryService, Any]:
    """A real MemoryService whose AgentCore client is stubbed (R-01)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    client = boto3.client(
        "bedrock-agentcore", region_name="us-east-1", config=DELETE_CLIENT_CONFIG
    )
    summary = Mock(spec=SummaryStrategy)
    summary.namespaces_for.side_effect = lambda actor: [
        f"/strategies/sum-1/actors/{actor}"
    ]
    service = MemoryService(
        memory_id=STUB_MEMORY_ID,
        region="us-east-1",
        strategy=summary,
        validate_connection=False,
        delete_client=client,
    )
    service._semantic_strategy = None
    return service, client


def _record_response(record_id: str, namespace: str) -> dict[str, Any]:
    return {
        "memoryRecord": {
            "memoryRecordId": record_id,
            "content": {"text": "x"},
            "memoryStrategyId": "sum-1",
            "namespaces": [namespace],
            "createdAt": "2026-10-14T00:00:00Z",
        }
    }


@pytest.fixture
def mock_db():
    mock = Mock()
    mock.get.return_value = Persona.create_new(
        name="テスト",
        age=30,
        occupation="会社員",
        background="テスト背景",
        values=["v1"],
        pain_points=["p1"],
        goals=["g1"],
    ).owned_by(OWNER.sub)
    return mock


class _PersonaReader:
    """Reads the decision columns from the mocked database (``ResourceLookup``)."""

    def __init__(self, db: Mock) -> None:
        self.db = db

    def read(self, ids, deadline):
        found = {}
        for i in ids:
            persona = self.db.get(i)
            if persona is not None:
                found[i] = AccessTarget(
                    ResourceType.PERSONA,
                    i,
                    persona.owner_sub,
                    persona.shared_group_ids,
                    persona.name,
                )
        return found


def _lookup(db: Mock) -> ResourceLookup:
    return ResourceLookup({ResourceType.PERSONA: _PersonaReader(db)}, sample_rate=0)


@pytest.fixture
def mock_memory_service():
    mock = Mock()
    mock.is_semantic_enabled = True
    mock.save_knowledge.return_value = "mem-123"
    mock.list_memories.return_value = []
    mock.delete_memory.return_value = True
    mock.delete_owned_memory.return_value = True
    return mock


@pytest.fixture
def manager(mock_db, mock_memory_service):
    return PersonaMemoryManager(
        memory_service=mock_memory_service,
        resource_lookup=_lookup(mock_db),
    )


@pytest.mark.unit
class TestAddKnowledge:
    """add_knowledge のテスト"""

    def test_success(self, manager, mock_memory_service):
        result = manager.add_knowledge(OWNER, "p1", "好きな食べ物", "ラーメン")
        assert result == "mem-123"
        mock_memory_service.save_knowledge.assert_called_once()
        call_kwargs = mock_memory_service.save_knowledge.call_args[1]
        assert call_kwargs["actor_id"] == "p1"
        assert '<topic name="好きな食べ物">ラーメン</topic>' in call_kwargs["content"]

    def test_empty_persona_id(self, manager):
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_ID_INVALID):
            manager.add_knowledge(OWNER, "", "topic", "content")

    def test_whitespace_persona_id(self, manager):
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_ID_INVALID):
            manager.add_knowledge(OWNER, "   ", "topic", "content")

    def test_empty_topic_name(self, manager):
        with raises_code(
            PersonaMemoryManagerError, ErrorCode.MEMORY_TOPIC_NAME_REQUIRED
        ):
            manager.add_knowledge(OWNER, "p1", "", "content")

    def test_empty_topic_content(self, manager):
        with raises_code(PersonaMemoryManagerError, ErrorCode.MEMORY_CONTENT_REQUIRED):
            manager.add_knowledge(OWNER, "p1", "topic", "")

    def test_topic_name_too_long(self, manager):
        with raises_code(
            PersonaMemoryManagerError,
            ErrorCode.MEMORY_TOPIC_NAME_TOO_LONG,
            max_length=100,
        ):
            manager.add_knowledge(OWNER, "p1", "x" * 101, "content")

    def test_topic_content_too_long(self, manager):
        with raises_code(
            PersonaMemoryManagerError,
            ErrorCode.MEMORY_CONTENT_TOO_LONG,
            max_length=10000,
        ):
            manager.add_knowledge(OWNER, "p1", "topic", "x" * 10001)

    def test_persona_not_found(self, manager, mock_db):
        mock_db.get.return_value = None
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_NOT_FOUND):
            manager.add_knowledge(OWNER, "p1", "topic", "content")

    def test_memory_service_disabled(self, mock_db):
        mgr = PersonaMemoryManager(
            memory_service=None,
            resource_lookup=_lookup(mock_db),
        )
        # _memory_service_resolved=True なので service_factory は呼ばれない
        with raises_code(PersonaMemoryManagerError, ErrorCode.MEMORY_FEATURE_DISABLED):
            mgr.add_knowledge(OWNER, "p1", "topic", "content")

    def test_semantic_not_enabled(self, manager, mock_memory_service):
        mock_memory_service.is_semantic_enabled = False
        with raises_code(
            PersonaMemoryManagerError, ErrorCode.MEMORY_STRATEGY_NOT_CONFIGURED
        ):
            manager.add_knowledge(OWNER, "p1", "topic", "content")

    def test_memory_service_error(self, manager, mock_memory_service):
        mock_memory_service.save_knowledge.side_effect = MemoryServiceError("fail")
        with raises_code(PersonaMemoryManagerError, ErrorCode.MEMORY_OPERATION_FAILED):
            manager.add_knowledge(OWNER, "p1", "topic", "content")


@pytest.mark.unit
class TestGetMemories:
    """get_memories のテスト"""

    def test_success_with_memories(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1, 0, 0, i),
            )
            for i in range(3)
        ]
        mock_memory_service.list_memories.return_value = entries

        memories, page, total_pages = manager.get_memories(OWNER, "p1", "summary")
        assert len(memories) == 3
        assert page == 1
        assert total_pages == 1

    def test_filters_by_strategy_type(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id="m1",
                actor_id="p1",
                session_id="s1",
                content="summary content",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1),
            ),
            MemoryEntry(
                id="m2",
                actor_id="p1",
                session_id="s1",
                content='<topic name="知識">内容</topic>',
                metadata={"strategy_type": "semantic"},
                created_at=datetime(2024, 1, 2),
            ),
        ]
        mock_memory_service.list_memories.return_value = entries

        memories, _, _ = manager.get_memories(OWNER, "p1", "semantic")
        assert len(memories) == 1
        assert memories[0].id == "m2"
        assert memories[0].parsed_topic == {"name": "知識", "content": "内容"}

    def test_pagination(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1, 0, 0, i),
            )
            for i in range(15)
        ]
        mock_memory_service.list_memories.return_value = entries

        memories, page, total_pages = manager.get_memories(
            OWNER, "p1", "summary", page=2, per_page=10
        )
        assert len(memories) == 5
        assert page == 2
        assert total_pages == 2

    def test_empty_persona_id(self, manager):
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_ID_INVALID):
            manager.get_memories(OWNER, "")

    def test_persona_not_found(self, manager, mock_db):
        mock_db.get.return_value = None
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_NOT_FOUND):
            manager.get_memories(OWNER, "p1")

    def test_memory_disabled_returns_empty(self, mock_db):
        mgr = PersonaMemoryManager(
            memory_service=None,
            resource_lookup=_lookup(mock_db),
        )
        memories, page, total = mgr.get_memories(OWNER, "p1")
        assert memories == []
        assert page == 1
        assert total == 1


@pytest.mark.unit
class TestDeleteMemory:
    """delete_memory のテスト"""

    def test_success(self, manager, mock_memory_service):
        result = manager.delete_memory(OWNER, "p1", "mem-1")
        assert result is True
        # U5 R-01: the record's owner is checked before deleting it
        mock_memory_service.delete_owned_memory.assert_called_once_with(
            actor_id="p1", memory_id="mem-1"
        )
        mock_memory_service.delete_memory.assert_not_called()

    def test_not_found(self, manager, mock_memory_service):
        mock_memory_service.delete_owned_memory.return_value = False
        result = manager.delete_memory(OWNER, "p1", "mem-nonexist")
        assert result is False

    def test_memory_of_another_persona_is_not_deleted(self, mock_db, monkeypatch):
        """R-01: persona A's ID with persona B's record ID leaves B's record."""
        service, client = _real_memory_service(monkeypatch)
        manager = PersonaMemoryManager(
            memory_service=service,
            resource_lookup=_lookup(mock_db),
        )
        with Stubber(client) as stub:
            stub.add_response(
                "get_memory_record",
                _record_response(
                    B_RECORD_ID, "/strategies/sum-1/actors/p2/sessions/d1"
                ),
                {"memoryId": STUB_MEMORY_ID, "memoryRecordId": B_RECORD_ID},
            )
            # no delete_memory_record is queued: a call to it fails the test

            result = manager.delete_memory(OWNER, "p1", B_RECORD_ID)

            stub.assert_no_pending_responses()
        # the same answer as a record that does not exist (MEMORY_ALREADY_DELETED)
        assert result is False

    def test_own_record_is_deleted(self, mock_db, monkeypatch):
        service, client = _real_memory_service(monkeypatch)
        manager = PersonaMemoryManager(
            memory_service=service,
            resource_lookup=_lookup(mock_db),
        )
        with Stubber(client) as stub:
            stub.add_response(
                "get_memory_record",
                _record_response(
                    A_RECORD_ID, "/strategies/sum-1/actors/p1/sessions/d1"
                ),
                {"memoryId": STUB_MEMORY_ID, "memoryRecordId": A_RECORD_ID},
            )
            stub.add_response(
                "delete_memory_record",
                {"memoryRecordId": A_RECORD_ID},
                {"memoryId": STUB_MEMORY_ID, "memoryRecordId": A_RECORD_ID},
            )

            assert manager.delete_memory(OWNER, "p1", A_RECORD_ID) is True

            stub.assert_no_pending_responses()

    def test_memory_disabled(self, mock_db):
        mgr = PersonaMemoryManager(
            memory_service=None,
            resource_lookup=_lookup(mock_db),
        )
        with raises_code(PersonaMemoryManagerError, ErrorCode.MEMORY_FEATURE_DISABLED):
            mgr.delete_memory(OWNER, "p1", "mem-1")

    def test_connection_error(self, manager, mock_memory_service):
        mock_memory_service.delete_owned_memory.side_effect = ConnectionError("timeout")
        with raises_code(
            PersonaMemoryManagerError, ErrorCode.MEMORY_SERVICE_UNAVAILABLE
        ):
            manager.delete_memory(OWNER, "p1", "mem-1")

    def test_service_error(self, manager, mock_memory_service):
        mock_memory_service.delete_owned_memory.side_effect = MemoryServiceError("fail")
        with raises_code(PersonaMemoryManagerError, ErrorCode.MEMORY_OPERATION_FAILED):
            manager.delete_memory(OWNER, "p1", "mem-1")


@pytest.mark.unit
class TestDeleteAllMemories:
    """delete_all_memories のテスト"""

    def test_success(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1),
            )
            for i in range(3)
        ]
        mock_memory_service.list_memories.return_value = entries

        count = manager.delete_all_memories(OWNER, "p1", "summary")
        assert count == 3
        assert mock_memory_service.delete_memory.call_count == 3

    def test_no_matching_memories(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id="m1",
                actor_id="p1",
                session_id="s1",
                content="content",
                metadata={"strategy_type": "semantic"},
                created_at=datetime(2024, 1, 1),
            ),
        ]
        mock_memory_service.list_memories.return_value = entries

        count = manager.delete_all_memories(OWNER, "p1", "summary")
        assert count == 0

    def test_memory_disabled(self, mock_db):
        mgr = PersonaMemoryManager(
            memory_service=None,
            resource_lookup=_lookup(mock_db),
        )
        with raises_code(PersonaMemoryManagerError, ErrorCode.MEMORY_FEATURE_DISABLED):
            mgr.delete_all_memories(OWNER, "p1")

    def test_partial_failure(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1),
            )
            for i in range(3)
        ]
        mock_memory_service.list_memories.return_value = entries
        mock_memory_service.delete_memory.side_effect = [True, Exception("fail"), True]

        count = manager.delete_all_memories(OWNER, "p1", "summary")
        assert count == 2


@pytest.mark.unit
class TestSafeGetMemories:
    """safe_get_memories のテスト"""

    def test_success(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id="m1",
                actor_id="p1",
                session_id="s1",
                content="content",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1),
            ),
        ]
        mock_memory_service.list_memories.return_value = entries

        result = manager.safe_get_memories(OWNER, "p1", "summary")
        assert len(result) == 1

    def test_error_returns_empty(self, manager, mock_memory_service):
        mock_memory_service.list_memories.side_effect = Exception("fail")
        result = manager.safe_get_memories(OWNER, "p1")
        assert result == []

    def test_memory_disabled_returns_empty(self, mock_db):
        mgr = PersonaMemoryManager(
            memory_service=None,
            resource_lookup=_lookup(mock_db),
        )
        result = mgr.safe_get_memories(OWNER, "p1")
        assert result == []


@pytest.mark.unit
class TestParseTopicContent:
    """_parse_topic_content のテスト"""

    def test_valid_topic(self, manager):
        content = '<topic name="好きな食べ物">ラーメンが好き</topic>'
        result = manager._parse_topic_content(content)
        assert result == {"name": "好きな食べ物", "content": "ラーメンが好き"}

    def test_multiline_content(self, manager):
        content = '<topic name="趣味">\n読書\nプログラミング\n</topic>'
        result = manager._parse_topic_content(content)
        assert result == {"name": "趣味", "content": "読書\nプログラミング"}

    def test_non_topic_content(self, manager):
        content = "plain text without topic tags"
        result = manager._parse_topic_content(content)
        assert result is None


# =============================================================================
# U5: メモリ画面は管理できる人だけ（BR4.1、AC4.2.4、NFR1.23）
# =============================================================================

#: Someone in ``sales``; the persona below is shared with ``sales``.
SHARED_READER = Principal(
    "local:peer@example.com", frozenset({Role.MEMBER}), frozenset({"sales"})
)


@pytest.fixture
def shared_db(mock_db):
    persona = mock_db.get.return_value
    from dataclasses import replace

    mock_db.get.return_value = replace(persona, shared_group_ids=frozenset({"sales"}))
    return mock_db


def _calls(principal):
    return [
        lambda m: m.add_knowledge(principal, "p1", "topic", "content"),
        lambda m: m.get_memories(principal, "p1"),
        lambda m: m.delete_memory(principal, "p1", "mem-1"),
        lambda m: m.delete_all_memories(principal, "p1"),
        lambda m: m.ensure_memory_manageable(principal, "p1"),
    ]


@pytest.mark.unit
class TestMemoryAccess:
    @pytest.mark.parametrize("call", range(5))
    def test_shared_reader_gets_not_found(self, shared_db, mock_memory_service, call):
        manager = PersonaMemoryManager(
            memory_service=mock_memory_service,
            resource_lookup=_lookup(shared_db),
        )
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_NOT_FOUND):
            _calls(SHARED_READER)[call](manager)
        mock_memory_service.save_knowledge.assert_not_called()
        mock_memory_service.list_memories.assert_not_called()
        mock_memory_service.delete_memory.assert_not_called()
        mock_memory_service.delete_owned_memory.assert_not_called()

    @pytest.mark.parametrize("call", range(5))
    def test_unrelated_user_gets_not_found(self, manager, call):
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_NOT_FOUND):
            _calls(MEMBER_B.principal())[call](manager)

    @pytest.mark.parametrize("call", range(5))
    def test_owner_and_admin_pass(self, manager, call):
        _calls(OWNER)[call](manager)
        _calls(ADMIN.principal())[call](manager)

    def test_safe_get_memories_is_empty_for_others(self, manager, mock_memory_service):
        mock_memory_service.list_memories.return_value = [
            MemoryEntry(
                id="m",
                actor_id="p1",
                session_id="s",
                content="c",
                metadata={"strategy_type": "summary"},
            )
        ]
        assert manager.safe_get_memories(MEMBER_B.principal(), "p1") == []
        assert len(manager.safe_get_memories(OWNER, "p1")) == 1

    def test_denial_is_logged(self, shared_db, mock_memory_service, capsys):
        logs = LogCapture(capsys)
        manager = PersonaMemoryManager(
            memory_service=mock_memory_service,
            resource_lookup=_lookup(shared_db),
        )
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_NOT_FOUND):
            manager.get_memories(SHARED_READER, "p1")
        (denied,) = logs.events("authz.denied")
        assert denied["sub"] == SHARED_READER.sub
        assert "p1" not in str(denied)

    def test_access_check_comes_before_the_disabled_feature(self, mock_db):
        manager = PersonaMemoryManager(
            memory_service=None,
            resource_lookup=_lookup(mock_db),
        )
        with raises_code(PersonaMemoryManagerError, ErrorCode.PERSONA_NOT_FOUND):
            manager.get_memories(MEMBER_B.principal(), "p1")


#: 記憶の中身・知識の本文に入れる目印（NFR3.11）。ログ・例外に出てはならない
_CONTENT_MARK = "MEMO-CONTENT-SENTINEL-7d1e"


@pytest.mark.unit
class TestMemoryContentStaysOutOfLogs:
    """NFR3.11: 記憶の中身（知識の題名と本文を含む）をログ・例外に出さない。

    ログに入れてよいのはペルソナ・記憶の ID、件数、例外の種類だけ。
    """

    @staticmethod
    def _entries() -> list[MemoryEntry]:
        return [
            MemoryEntry(
                id="m-summary",
                actor_id="p1",
                session_id="s1",
                content=f"議論の要約 {_CONTENT_MARK}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2026, 10, 1),
            ),
            MemoryEntry(
                id="m-semantic",
                actor_id="p1",
                session_id="s1",
                content=(
                    f'<topic name="題名{_CONTENT_MARK}">本文 {_CONTENT_MARK}</topic>'
                ),
                metadata={"strategy_type": "semantic"},
                created_at=datetime(2026, 10, 1),
            ),
        ]

    def test_content_is_not_logged_or_raised(
        self, manager, mock_memory_service, caplog, capsys
    ):
        import logging

        from tests.log_helpers import assert_not_leaked

        caplog.set_level(logging.DEBUG)
        logs = LogCapture(capsys)
        raised: list[BaseException] = []

        def attempt(call: Any) -> None:
            try:
                call()
            except Exception as e:  # noqa: BLE001 - 上がった例外も調べる
                raised.append(e)

        service = mock_memory_service
        service.list_memories.return_value = self._entries()
        topic, body = f"題名{_CONTENT_MARK}", f"本文 {_CONTENT_MARK}"

        # 成功の経路: 一覧・追加・1 件の削除・全件の削除
        attempt(lambda: manager.get_memories(OWNER, "p1", strategy_type="summary"))
        attempt(lambda: manager.get_memories(OWNER, "p1", strategy_type="semantic"))
        attempt(lambda: manager.add_knowledge(OWNER, "p1", topic, body))
        attempt(lambda: manager.delete_memory(OWNER, "p1", "m-summary"))
        attempt(lambda: manager.delete_all_memories(OWNER, "p1", "semantic"))
        attempt(lambda: manager.safe_get_memories(OWNER, "p1", "semantic"))
        assert not raised

        # Service が失敗する経路
        service.save_knowledge.side_effect = MemoryServiceError("save failed")
        attempt(lambda: manager.add_knowledge(OWNER, "p1", topic, body))
        service.save_knowledge.side_effect = ConnectionError("timeout")
        attempt(lambda: manager.add_knowledge(OWNER, "p1", topic, body))
        service.delete_owned_memory.side_effect = MemoryServiceError("delete failed")
        attempt(lambda: manager.delete_memory(OWNER, "p1", "m-summary"))
        service.delete_memory.side_effect = MemoryServiceError("delete failed")
        attempt(lambda: manager.delete_all_memories(OWNER, "p1", "semantic"))
        service.list_memories.side_effect = MemoryServiceError("list failed")
        attempt(lambda: manager.get_memories(OWNER, "p1"))
        attempt(lambda: manager.delete_all_memories(OWNER, "p1", "summary"))
        attempt(lambda: manager.safe_get_memories(OWNER, "p1"))
        # 失敗はコード付きの例外で上がる（全件の削除の 1 件ずつの失敗は件数に数えないだけ、
        # safe_get_memories は空を返す）
        assert len(raised) == 5

        assert caplog.records  # 何も記録しない状態での空振りを防ぐ
        assert_not_leaked(_CONTENT_MARK, caplog, logs.records(), raised)
