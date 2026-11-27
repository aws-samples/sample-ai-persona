"""Integration tests for CognitoGroupsService (ListGroups for the department list).

Only groups with the ``group:`` prefix become departments, and the group name is
used when there is no description. The 5-second limit covers all pages together,
and there is no retry. The boto3 client is replaced with unittest.mock.
"""

from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from src.models.authz import Department
from src.models.errors import ErrorCode
from src.services.cognito_groups_service import (
    CLIENT_CONFIG,
    LIST_GROUPS_TIMEOUT_SECONDS,
    CognitoGroupsError,
    CognitoGroupsService,
)
from tests.error_helpers import raises_code

POOL = "ap-northeast-1_TestPool"


class FakeClock:
    def __init__(self, now: float = 500.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _service(client: Mock, clock: FakeClock | None = None) -> CognitoGroupsService:
    return CognitoGroupsService(
        user_pool_id=POOL, client=client, clock=clock or FakeClock()
    )


def _group(name: str, description: str | None = None) -> dict:
    group = {"GroupName": name, "UserPoolId": POOL}
    if description is not None:
        group["Description"] = description
    return group


class TestListDepartments:
    def test_reads_every_page(self):
        client = Mock()
        client.list_groups.side_effect = [
            {"Groups": [_group("group:sales", "営業部")], "NextToken": "t-1"},
            {"Groups": [_group("group:dev", "開発部")]},
        ]

        departments = _service(client).list_departments()

        assert departments == [
            Department(group_id="sales", display_name="営業部"),
            Department(group_id="dev", display_name="開発部"),
        ]
        first, second = client.list_groups.call_args_list
        assert first.kwargs["UserPoolId"] == POOL
        assert "NextToken" not in first.kwargs
        assert second.kwargs["NextToken"] == "t-1"

    def test_only_department_groups_are_departments(self):
        client = Mock()
        client.list_groups.return_value = {
            "Groups": [
                _group("role:admin", "管理者"),
                _group("role:member"),
                _group("group:sales", "営業部"),
                _group("other"),
            ]
        }
        departments = _service(client).list_departments()
        assert [d.group_id for d in departments] == ["sales"]

    @pytest.mark.parametrize("description", [None, "", "   "])
    def test_group_name_when_there_is_no_description(self, description):
        client = Mock()
        client.list_groups.return_value = {
            "Groups": [_group("group:sales", description)]
        }
        departments = _service(client).list_departments()
        assert departments == [Department(group_id="sales", display_name="sales")]

    def test_the_five_seconds_cover_all_pages(self):
        clock = FakeClock()

        def slow_page(**kwargs: object) -> dict:
            clock.now += 4.0  # One second left; the next page cannot be waited for.
            return {"Groups": [_group("group:sales")], "NextToken": "t-1"}

        client = Mock()
        client.list_groups.side_effect = slow_page

        with raises_code(CognitoGroupsError, ErrorCode.LOOKUP_UNAVAILABLE) as info:
            _service(client, clock).list_departments()

        assert info.value.reason == "deadline"
        assert client.list_groups.call_count == 1
        assert LIST_GROUPS_TIMEOUT_SECONDS == 5.0

    @pytest.mark.parametrize(
        "error",
        [
            EndpointConnectionError(endpoint_url="https://cognito-idp.example"),
            ClientError({"Error": {"Code": "TooManyRequestsException"}}, "ListGroups"),
        ],
        ids=["connection", "throttled"],
    )
    def test_failures_are_not_retried(self, error):
        client = Mock()
        client.list_groups.side_effect = error

        with raises_code(CognitoGroupsError, ErrorCode.LOOKUP_UNAVAILABLE) as info:
            _service(client).list_departments()

        assert info.value.reason == "unreachable"
        assert client.list_groups.call_count == 1

    def test_client_makes_a_single_attempt(self):
        # botocore adds a "mode" key to this shared Config once any client is
        # created from it, so only the attempt count is compared (order-independent).
        assert CLIENT_CONFIG.retries is not None
        assert CLIENT_CONFIG.retries.get("total_max_attempts") == 1
        assert CLIENT_CONFIG.connect_timeout + CLIENT_CONFIG.read_timeout <= 5.0

    def test_region_comes_from_the_pool_id(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        service = CognitoGroupsService(user_pool_id=POOL)
        assert service.client.meta.region_name == "ap-northeast-1"
