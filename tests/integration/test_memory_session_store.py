"""MemorySessionStore（AUTH_MODE=local のプロセス内の置き場）のテスト。

SessionStoreService と同じ口で、同じ振る舞いをすること（BR1.15）。
"""

from datetime import datetime, timedelta, timezone

from src.models.authz import Role
from src.models.login_session import AuthMethod, LoginSession
from src.services.memory_session_store import MemorySessionStore

_NOW = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)


def _session(hash_: str = "h" * 64) -> LoginSession:
    return LoginSession.create_new(
        session_id_hash=hash_,
        user_sub="local-dev",
        auth_method=AuthMethod.LOCAL,
        now=_NOW,
        roles=frozenset({Role.MEMBER}),
    )


def test_saved_session_is_read_back():
    store = MemorySessionStore()
    session = _session()
    store.put(session)
    assert store.get(session.session_id_hash) == session


def test_unknown_id_returns_none():
    assert MemorySessionStore().get("missing") is None


def test_touch_writes_only_last_accessed_at_and_never_recreates():
    store = MemorySessionStore()
    session = _session()
    store.put(session)
    later = _NOW + timedelta(minutes=6)

    store.touch(session.update(roles=frozenset({Role.ADMIN})).touch(later))

    stored = store.get(session.session_id_hash)
    assert stored.last_accessed_at == later
    assert stored.roles == frozenset({Role.MEMBER})
    assert stored.version == 1

    store.delete(session.session_id_hash)
    store.touch(session.touch(later))
    assert store.get(session.session_id_hash) is None


def test_only_the_first_versioned_writer_wins():
    store = MemorySessionStore()
    session = _session()
    store.put(session)

    first = session.update(roles=frozenset({Role.ADMIN}))
    assert store.update_versioned(first, expected_version=1) is True
    assert store.update_versioned(session.update(), expected_version=1) is False
    assert store.get(session.session_id_hash).roles == frozenset({Role.ADMIN})


def test_delete_respects_the_version():
    store = MemorySessionStore()
    session = _session()
    store.put(session)

    assert store.delete(session.session_id_hash, expected_version=3) is False
    assert store.delete(session.session_id_hash, expected_version=1) is True
    assert store.get(session.session_id_hash) is None
