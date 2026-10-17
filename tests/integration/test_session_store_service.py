"""SessionStoreService（Sessions テーブル）の結合テスト。moto の DynamoDB を使う。

BR1.10（last_accessed_at だけの部分更新）、BR1.12（version 付きの更新・削除）、
BR1.18 / NFR9.8（届かなければ SESSION_STORE_UNAVAILABLE）、
reliability-design.md（0.5 秒・0.5 秒・max_attempts=2・standard）。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

from src.models.authz import Role  # noqa: E402
from src.models.errors import ErrorCode  # noqa: E402
from src.models.login_challenge import LoginChallenge  # noqa: E402
from src.models.login_session import AuthMethod, LoginSession  # noqa: E402
from src.services.session_store_service import (  # noqa: E402
    SessionStoreError,
    SessionStoreService,
)
from tests.error_helpers import raises_code  # noqa: E402

REGION = "ap-northeast-1"
TABLE = "SessionsTest"
_NOW = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)


def _session(hash_: str = "h" * 64, **overrides: object) -> LoginSession:
    params: dict[str, object] = {
        "session_id_hash": hash_,
        "user_sub": "user-1",
        "auth_method": AuthMethod.OIDC,
        "now": _NOW,
        "roles": frozenset({Role.MEMBER}),
        "group_ids": frozenset({"sales"}),
        "display_name": "山田",
        "access_token_enc": "v1.access-1",
        "refresh_token_enc": "v1.refresh-1",
        "key_version": "v1",
        "access_expires_at": _NOW + timedelta(hours=1),
    }
    params.update(overrides)
    return LoginSession.create_new(**params)  # type: ignore[arg-type]


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "session_id_hash", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "session_id_hash", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield SessionStoreService(table_name=TABLE, region=REGION)


class TestReadWrite:
    def test_saved_session_is_read_back_with_a_strong_read(self, store):
        session = _session()
        store.put(session)
        spy = Mock(wraps=store.client)
        store.client = spy

        assert store.get(session.session_id_hash) == session
        assert spy.get_item.call_args.kwargs["ConsistentRead"] is True

    def test_unknown_id_returns_none(self, store):
        assert store.get("missing") is None


class TestActivityWrite:
    def test_touch_writes_only_last_accessed_at(self, store):
        session = _session()
        store.put(session)
        later = _NOW + timedelta(minutes=6)
        # 別の値を持つ手元の写しで touch しても、トークン・version は書かない
        stale_copy = session.update(access_token_enc="v1.other").touch(later)

        store.touch(stale_copy)

        stored = store.get(session.session_id_hash)
        assert stored.last_accessed_at == later
        assert stored.access_token_enc == "v1.access-1"
        assert stored.version == 1

    def test_touch_does_not_recreate_a_deleted_session(self, store):
        session = _session()
        store.touch(session.touch(_NOW + timedelta(minutes=6)))
        assert store.get(session.session_id_hash) is None


class TestVersionedWrite:
    def test_only_the_first_writer_wins(self, store):
        session = _session()
        store.put(session)
        first = session.update(access_token_enc="v1.access-2")
        second = session.update(access_token_enc="v1.access-3")

        assert store.update_versioned(first, expected_version=1) is True
        assert store.update_versioned(second, expected_version=1) is False

        stored = store.get(session.session_id_hash)
        assert stored.access_token_enc == "v1.access-2"
        assert stored.version == 2

    def test_cleared_values_are_removed_from_the_item(self, store):
        session = _session(refresh_failing_since=_NOW)
        store.put(session)
        later = _NOW + timedelta(minutes=6)
        store.touch(session.touch(later))
        cleared = session.update(roles=frozenset(), refresh_failing_since=None)
        assert cleared.roles == frozenset()

        assert store.update_versioned(cleared, expected_version=1) is True

        stored = store.get(session.session_id_hash)
        assert stored.refresh_failing_since is None
        assert stored.roles == frozenset()
        # 部分更新で書いた last_accessed_at を上書きしない
        assert stored.last_accessed_at == later

    def test_delete_respects_the_version(self, store):
        session = _session()
        store.put(session)

        assert store.delete(session.session_id_hash, expected_version=5) is False
        assert store.get(session.session_id_hash) is not None
        assert store.delete(session.session_id_hash, expected_version=1) is True
        assert store.get(session.session_id_hash) is None

        store.put(session)
        assert store.delete(session.session_id_hash) is True
        assert store.get(session.session_id_hash) is None


class TestFailures:
    @pytest.mark.parametrize(
        "error",
        [
            EndpointConnectionError(endpoint_url="https://dynamodb.invalid"),
            ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "GetItem",
            ),
        ],
    )
    def test_unreachable_table_raises_store_unavailable(self, store, error):
        broken = Mock()
        broken.get_item.side_effect = error
        store.client = broken

        with raises_code(SessionStoreError, ErrorCode.SESSION_STORE_UNAVAILABLE):
            store.get("any")

    def test_errors_do_not_carry_the_session_hash(self, store):
        broken = Mock()
        broken.delete_item.side_effect = EndpointConnectionError(
            endpoint_url="https://dynamodb.invalid"
        )
        store.client = broken
        with pytest.raises(SessionStoreError) as exc_info:
            store.delete("secret-session-hash")
        assert "secret-session-hash" not in str(exc_info.value)

    def test_client_uses_short_timeouts_and_two_attempts(self, store):
        cfg = store.client.meta.config
        assert cfg.connect_timeout == 0.5
        assert cfg.read_timeout == 0.5
        # 試行は合計 2 回（botocore の total_max_attempts は最初の呼び出しを含む）
        assert cfg.retries["total_max_attempts"] == 2
        assert cfg.retries["mode"] == "standard"


# =============================================================================
# U8: LoginChallenge（BR2.2、BR2.7、NFR3.17、NFR9.42、NFR9.45）
# =============================================================================


def _challenge(challenge_id: str = "raw-challenge-1") -> LoginChallenge:
    return LoginChallenge.create_new(
        challenge_id=challenge_id,
        cognito_session_enc="v1.cipher-of-session",
        key_version="v1",
        username="user-sub-1",
        next="/persona",
        now=_NOW,
    )


class TestChallenge:
    def test_put_then_strong_read(self, store):
        challenge = _challenge()
        store.put_challenge(challenge)

        assert store.get_challenge(challenge.session_id_hash) == challenge

    def test_strong_read_is_used(self, store):
        store.put_challenge(_challenge())
        real = store.client
        spy = Mock(wraps=real)
        store.client = spy
        store.get_challenge(_challenge().session_id_hash)
        assert spy.get_item.call_args.kwargs["ConsistentRead"] is True

    def test_claim_takes_it_once(self, store):
        challenge = _challenge()
        store.put_challenge(challenge)

        assert store.claim_challenge(challenge.session_id_hash) == challenge
        assert store.claim_challenge(challenge.session_id_hash) is None
        assert store.get_challenge(challenge.session_id_hash) is None

    def test_restore_writes_back_only_when_absent(self, store):
        challenge = _challenge()
        store.put_challenge(challenge)
        claimed = store.claim_challenge(challenge.session_id_hash)
        assert claimed is not None

        assert store.restore_challenge(claimed) is True
        assert store.get_challenge(challenge.session_id_hash) == challenge
        assert store.restore_challenge(claimed) is False

    def test_challenge_item_is_not_a_login_session(self, store):
        challenge = _challenge()
        store.put_challenge(challenge)
        assert store.get(challenge.session_id_hash) is None

    def test_login_session_item_is_not_a_challenge(self, store):
        session = _session()
        store.put(session)
        assert store.get_challenge(session.session_id_hash) is None
        assert store.claim_challenge(session.session_id_hash) is None
        # 取り出しに失敗しても、ログインセッションは消えない
        assert store.get(session.session_id_hash) == session

    def test_unknown_challenge_is_none(self, store):
        assert store.get_challenge("challenge#missing") is None
        assert store.claim_challenge("challenge#missing") is None

    def test_no_plain_cognito_session_or_password_in_the_item(self, store):
        challenge = _challenge()
        store.put_challenge(challenge)
        item = boto3.client("dynamodb", region_name=REGION).get_item(
            TableName=TABLE, Key={"session_id_hash": {"S": challenge.session_id_hash}}
        )["Item"]
        assert item["item_type"] == {"S": "challenge"}
        assert not any("password" in name for name in item)

    @pytest.mark.parametrize(
        "operation",
        ["put_item", "get_item", "delete_item"],
    )
    def test_unreachable_table_raises_store_unavailable(self, store, operation):
        broken = Mock()
        getattr(broken, operation).side_effect = EndpointConnectionError(
            endpoint_url="https://dynamodb.invalid"
        )
        store.client = broken
        calls = {
            "put_item": lambda: store.put_challenge(_challenge()),
            "get_item": lambda: store.get_challenge("challenge#x"),
            "delete_item": lambda: store.claim_challenge("challenge#x"),
        }
        with raises_code(SessionStoreError, ErrorCode.SESSION_STORE_UNAVAILABLE):
            calls[operation]()

    def test_restore_on_unreachable_table_raises(self, store):
        broken = Mock()
        broken.put_item.side_effect = EndpointConnectionError(
            endpoint_url="https://dynamodb.invalid"
        )
        store.client = broken
        with raises_code(SessionStoreError, ErrorCode.SESSION_STORE_UNAVAILABLE):
            store.restore_challenge(_challenge())
