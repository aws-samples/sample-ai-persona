"""LoginChallenge モデルの単体テスト（U8 entities.md、BR2.2〜BR2.4、BR2.7）。

初回パスワード変更の途中の状態。Sessions テーブルに ``challenge#`` + SHA-256 の
キーで置き、``item_type="challenge"`` でログインセッションと区別する。期限は
``created_at`` から 3 分で、``ttl`` は DynamoDB の自動削除の目安にすぎない。
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from src.models.login_challenge import (
    CHALLENGE_ITEM_TYPE,
    CHALLENGE_KEY_PREFIX,
    CHALLENGE_TTL,
    LoginChallenge,
    challenge_key,
)

_NOW = datetime(2026, 10, 17, 0, 0, 0, tzinfo=timezone.utc)
_CHALLENGE_ID = "raw-challenge-id-0123456789abcdef"


def _challenge(**overrides: object) -> LoginChallenge:
    params: dict[str, object] = {
        "challenge_id": _CHALLENGE_ID,
        "cognito_session_enc": "v1.cipher-of-cognito-session",
        "key_version": "v1",
        "username": "0b5c0f2e-user-sub",
        "next": "/persona/management",
        "now": _NOW,
    }
    params.update(overrides)
    return LoginChallenge.create_new(**params)  # type: ignore[arg-type]


class TestKey:
    def test_key_is_prefix_plus_sha256_hex_of_the_raw_id(self):
        expected = hashlib.sha256(_CHALLENGE_ID.encode("utf-8")).hexdigest()
        assert challenge_key(_CHALLENGE_ID) == f"challenge#{expected}"
        assert CHALLENGE_KEY_PREFIX == "challenge#"

    def test_key_never_contains_the_raw_id(self):
        assert _CHALLENGE_ID not in challenge_key(_CHALLENGE_ID)

    def test_key_never_collides_with_a_login_session_hash(self):
        # ログインセッションのキーは接頭辞のない 64 桁の 16 進（U1 BR1.5）
        session_hash = hashlib.sha256(_CHALLENGE_ID.encode("utf-8")).hexdigest()
        assert challenge_key(_CHALLENGE_ID) != session_hash


class TestCreateNew:
    def test_carries_the_item_type_key_and_a_three_minute_ttl(self):
        challenge = _challenge()

        assert challenge.session_id_hash == challenge_key(_CHALLENGE_ID)
        assert challenge.item_type == CHALLENGE_ITEM_TYPE == "challenge"
        assert challenge.created_at == _NOW
        assert challenge.ttl == int((_NOW + timedelta(minutes=3)).timestamp())
        assert CHALLENGE_TTL == timedelta(minutes=3)

    def test_keeps_only_encrypted_session_and_no_password(self):
        data = _challenge().to_dict()

        assert data["cognito_session_enc"] == "v1.cipher-of-cognito-session"
        assert not any("password" in name for name in data)

    def test_is_immutable(self):
        challenge = _challenge()
        with pytest.raises(AttributeError):
            challenge.username = "someone-else"  # type: ignore[misc]


class TestSerialization:
    def test_round_trip(self):
        challenge = _challenge()
        assert LoginChallenge.from_dict(challenge.to_dict()) == challenge

    def test_stored_form_names_the_item_type(self):
        data = _challenge().to_dict()
        assert data["item_type"] == "challenge"
        assert data["created_at"] == _NOW.isoformat()
        assert data["next"] == "/persona/management"

    @pytest.mark.parametrize("item_type", [None, "session", ""])
    def test_another_item_type_is_not_a_challenge(self, item_type):
        data = _challenge().to_dict()
        if item_type is None:
            del data["item_type"]
        else:
            data["item_type"] = item_type
        assert LoginChallenge.from_dict(data) is None

    def test_login_session_shaped_item_is_not_a_challenge(self):
        session_item = {
            "session_id_hash": "a" * 64,
            "user_sub": "user-1",
            "auth_method": "admin_api",
            "created_at": _NOW.isoformat(),
            "last_accessed_at": _NOW.isoformat(),
            "ttl": 1,
            "version": 1,
        }
        assert LoginChallenge.from_dict(session_item) is None

    def test_challenge_missing_a_field_cannot_be_read(self):
        data = _challenge().to_dict()
        del data["cognito_session_enc"]
        with pytest.raises(ValueError):
            LoginChallenge.from_dict(data)


class TestExpiry:
    def test_exactly_three_minutes_is_still_valid(self):
        assert not _challenge().is_expired(_NOW + timedelta(minutes=3))

    def test_past_three_minutes_is_expired(self):
        assert _challenge().is_expired(_NOW + timedelta(minutes=3, seconds=1))

    def test_ttl_is_not_used_for_the_decision(self):
        # ttl が先でも後でも、判定は created_at から（BR2.4）
        challenge = _challenge()
        stale_ttl = LoginChallenge(
            session_id_hash=challenge.session_id_hash,
            cognito_session_enc=challenge.cognito_session_enc,
            key_version=challenge.key_version,
            username=challenge.username,
            next=challenge.next,
            created_at=challenge.created_at,
            ttl=0,
        )
        assert not stale_ttl.is_expired(_NOW + timedelta(minutes=1))
