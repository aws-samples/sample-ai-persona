"""UserDirectoryService（所有者の名前の写しの ListUsers）の結合テスト。

BR5.1（全ページを読む）、NFR2.7（sub・name・利用中かだけ、メールを持たない）、
performance-design.md §3（全ページの締め切り 5 秒、接続 0.5 秒・読み取り 1.5 秒、再試行なし）。
``botocore.stub.Stubber`` で本物の client の要求の形を確かめる。
"""

from typing import Any
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from src.models.sharing import OwnerSummary
from src.services.user_directory_service import (
    CLIENT_CONFIG,
    LIST_USERS_TIMEOUT_SECONDS,
    UserDirectoryError,
    UserDirectoryService,
)

POOL = "ap-northeast-1_TestPool"


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _user(sub: str, name: str | None = None, enabled: bool = True) -> dict[str, Any]:
    attributes = [{"Name": "sub", "Value": sub}]
    if name is not None:
        attributes.append({"Name": "name", "Value": name})
    return {"Username": f"user-{sub}", "Attributes": attributes, "Enabled": enabled}


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    client = boto3.client("cognito-idp", region_name="ap-northeast-1")
    with Stubber(client) as stubber:
        yield client, stubber


class TestListUsers:
    def test_reads_every_page_of_60(self, stubbed):
        client, stubber = stubbed
        # No AttributesToGet: naming "name" makes ListUsers fail when a user
        # (e.g. the first admin) has no name, so read every attribute and keep
        # only sub / name / Enabled.
        expected = {"UserPoolId": POOL, "Limit": 60}
        stubber.add_response(
            "list_users",
            {"Users": [_user("s1", "中村 由美")], "PaginationToken": "t-1"},
            expected,
        )
        stubber.add_response(
            "list_users",
            {"Users": [_user("s2", None, enabled=False)]},
            {**expected, "PaginationToken": "t-1"},
        )

        users = UserDirectoryService(user_pool_id=POOL, client=client).list_users()

        assert users == [
            OwnerSummary(sub="s1", display_name="中村 由美", enabled=True),
            OwnerSummary(sub="s2", display_name=None, enabled=False),
        ]
        stubber.assert_no_pending_responses()

    def test_email_is_never_kept(self, stubbed):
        client, stubber = stubbed
        user = _user("s1", "名前")
        user["Attributes"].append({"Name": "email", "Value": "a@example.com"})
        stubber.add_response("list_users", {"Users": [user]})

        users = UserDirectoryService(user_pool_id=POOL, client=client).list_users()

        assert "a@example.com" not in repr(users)

    def test_users_without_sub_are_skipped(self, stubbed):
        client, stubber = stubbed
        stubber.add_response(
            "list_users",
            {"Users": [{"Username": "x", "Attributes": [], "Enabled": True}]},
        )
        assert UserDirectoryService(user_pool_id=POOL, client=client).list_users() == []

    def test_cognito_failure_becomes_own_error(self, stubbed):
        client, stubber = stubbed
        stubber.add_client_error(
            "list_users", service_error_code="TooManyRequestsException"
        )

        with pytest.raises(UserDirectoryError) as exc:
            UserDirectoryService(user_pool_id=POOL, client=client).list_users()
        assert exc.value.reason == "unreachable"

    def test_unreachable_becomes_own_error(self):
        client = Mock()
        client.list_users.side_effect = EndpointConnectionError(
            endpoint_url="https://x"
        )
        with pytest.raises(UserDirectoryError):
            UserDirectoryService(user_pool_id=POOL, client=client).list_users()


class TestDeadline:
    def test_stops_when_the_whole_listing_runs_out_of_time(self):
        clock = FakeClock()
        client = Mock()

        def page(**_kwargs: Any) -> dict[str, Any]:
            clock.now += 2.0
            return {"Users": [_user("s1", "a")], "PaginationToken": "more"}

        client.list_users.side_effect = page

        with pytest.raises(UserDirectoryError) as exc:
            UserDirectoryService(
                user_pool_id=POOL, client=client, clock=clock
            ).list_users()
        assert exc.value.reason == "deadline"
        assert client.list_users.call_count == 2

    def test_limits(self):
        assert LIST_USERS_TIMEOUT_SECONDS == 5.0
        assert CLIENT_CONFIG.connect_timeout == 0.5
        assert CLIENT_CONFIG.read_timeout == 1.5
        # botocore may add "mode" to the shared dict once a client is built,
        # so check the value that matters instead of the whole dict.
        assert CLIENT_CONFIG.retries is not None
        assert CLIENT_CONFIG.retries.get("total_max_attempts") == 1
