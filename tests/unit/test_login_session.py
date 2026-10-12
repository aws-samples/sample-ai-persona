"""LoginSession モデルの単体テスト（entities.md、BR1.5、BR1.10、BR1.15）。"""

from datetime import datetime, timedelta, timezone

from src.models.authz import Role
from src.models.login_session import AuthMethod, LoginSession

_NOW = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
_HASH = "a" * 64


def _oidc_session(**overrides: object) -> LoginSession:
    params: dict[str, object] = {
        "session_id_hash": _HASH,
        "user_sub": "user-1",
        "auth_method": AuthMethod.OIDC,
        "now": _NOW,
        "roles": frozenset({Role.MEMBER}),
        "group_ids": frozenset({"sales"}),
        "display_name": "山田 太郎",
        "email": "yamada@example.com",
        "access_token_enc": "v1.access-cipher",
        "refresh_token_enc": "v1.refresh-cipher",
        "key_version": "v1",
        "access_expires_at": _NOW + timedelta(minutes=60),
    }
    params.update(overrides)
    return LoginSession.create_new(**params)  # type: ignore[arg-type]


class TestCreateNew:
    def test_starts_at_version_one_with_seven_day_ttl(self):
        session = _oidc_session()
        assert session.version == 1
        assert session.created_at == _NOW
        assert session.last_accessed_at == _NOW
        assert session.ttl == int((_NOW + timedelta(days=7)).timestamp())

    def test_local_session_carries_no_tokens(self):
        session = LoginSession.create_new(
            session_id_hash=_HASH,
            user_sub="local-dev",
            auth_method=AuthMethod.LOCAL,
            now=_NOW,
        )
        data = session.to_dict()
        for key in (
            "access_token_enc",
            "refresh_token_enc",
            "key_version",
            "access_expires_at",
        ):
            assert key not in data


class TestToDict:
    def test_omits_none_and_empty_sets(self):
        session = _oidc_session(
            roles=frozenset(), group_ids=frozenset(), display_name=None, email=None
        )
        data = session.to_dict()
        assert "roles" not in data
        assert "group_ids" not in data
        assert "display_name" not in data
        assert "email" not in data
        assert "refresh_failing_since" not in data
        assert None not in data.values()

    def test_serializes_sets_and_timestamps(self):
        data = _oidc_session().to_dict()
        assert data["roles"] == {"member"}
        assert data["group_ids"] == {"sales"}
        assert data["auth_method"] == "oidc"
        assert data["created_at"] == _NOW.isoformat()
        assert data["version"] == 1


class TestFromDict:
    def test_missing_roles_and_groups_become_empty_sets(self):
        data = _oidc_session().to_dict()
        del data["roles"]
        del data["group_ids"]
        session = LoginSession.from_dict(data)
        assert session.roles == frozenset()
        assert session.group_ids == frozenset()

    def test_round_trip_keeps_every_value(self):
        original = _oidc_session(refresh_failing_since=_NOW)
        assert LoginSession.from_dict(original.to_dict()) == original


class TestUpdates:
    def test_update_returns_a_new_instance_with_next_version(self):
        original = _oidc_session()
        updated = original.update(
            roles=frozenset({Role.ADMIN}), refresh_failing_since=_NOW
        )
        assert updated is not original
        assert original.roles == frozenset({Role.MEMBER})
        assert original.version == 1
        assert updated.roles == frozenset({Role.ADMIN})
        assert updated.refresh_failing_since == _NOW
        assert updated.version == 2

    def test_touch_moves_last_access_without_changing_version(self):
        original = _oidc_session()
        later = _NOW + timedelta(minutes=6)
        touched = original.touch(later)
        assert touched.last_accessed_at == later
        assert touched.version == original.version
        assert original.last_accessed_at == _NOW
