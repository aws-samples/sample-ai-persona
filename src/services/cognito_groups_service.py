"""
Cognito ``ListGroups`` for the department list (ADR-009, contract C4).

Departments are the user pool groups named ``group:<id>``; their description
is the display name, or ``<id>`` when the description is empty (BR2.11). Role
groups (``role:``) and any other groups are ignored.

Limits (``reliability-design.md``, NFR Design R-02 (3)):

- The whole listing, across every page, gets :data:`LIST_GROUPS_TIMEOUT_SECONDS`.
  Before each page the remaining time is checked; a page is only requested
  when one page's worst case (connect + read timeout) still fits.
- No retry (:data:`CLIENT_CONFIG` makes a single attempt). The caller
  (``DepartmentDirectory``) keeps the previous list and waits 60 seconds
  before asking again.

Failures raise :class:`CognitoGroupsError` (``LOOKUP_UNAVAILABLE``).
"""

import logging
import time
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.models.authz import Department
from src.models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)

#: Total time for one listing, all pages together.
LIST_GROUPS_TIMEOUT_SECONDS = 5.0

#: One attempt only; failures are handled by the caller's back-off.
CLIENT_CONFIG = Config(
    connect_timeout=0.5,
    read_timeout=1.5,
    retries={"total_max_attempts": 1},
)

#: Worst case of one page (connect + read timeout).
_PAGE_BUDGET_SECONDS = 2.0

#: Prefix of department groups.
GROUP_PREFIX = "group:"

#: ``ListGroups`` returns at most 60 groups per page.
_PAGE_SIZE = 60


class CognitoGroupsError(CodedError):
    """The group list could not be read.

    Attributes:
        reason: ``deadline`` or ``unreachable`` (for logs).
    """

    code = ErrorCode.LOOKUP_UNAVAILABLE

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class CognitoGroupsService:
    """Reads department groups from the user pool."""

    def __init__(
        self,
        *,
        user_pool_id: str,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the service.

        Args:
            user_pool_id: ``<region>_<id>``; the region part selects the endpoint.
            client: Optional pre-built ``cognito-idp`` client (tests).
            clock: Monotonic clock for the overall time limit.
        """
        self.user_pool_id = user_pool_id
        region = user_pool_id.split("_", 1)[0]
        self.client = client or boto3.client(
            "cognito-idp", region_name=region, config=CLIENT_CONFIG
        )
        self._clock = clock

    def list_departments(self) -> list[Department]:
        """Return every ``group:`` group as a Department (pool order).

        Raises:
            CognitoGroupsError: Out of time or Cognito unreachable.
        """
        deadline = self._clock() + LIST_GROUPS_TIMEOUT_SECONDS
        departments: list[Department] = []
        next_token: str | None = None
        while True:
            if deadline - self._clock() < _PAGE_BUDGET_SECONDS:
                raise CognitoGroupsError(
                    "ListGroups did not finish within the time limit",
                    reason="deadline",
                )
            response = self._list_page(next_token)
            departments += [
                department
                for group in response.get("Groups", [])
                if (department := _to_department(group)) is not None
            ]
            next_token = response.get("NextToken")
            if not next_token:
                return departments

    def _list_page(self, next_token: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"UserPoolId": self.user_pool_id, "Limit": _PAGE_SIZE}
        if next_token:
            kwargs["NextToken"] = next_token
        try:
            response: dict[str, Any] = self.client.list_groups(**kwargs)
            return response
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            logger.warning("ListGroups failed: %s", error_code)
            raise CognitoGroupsError(
                f"ListGroups failed: {error_code}", reason="unreachable"
            ) from e
        except BotoCoreError as e:
            logger.warning("ListGroups unreachable: %s", type(e).__name__)
            raise CognitoGroupsError(
                f"ListGroups unreachable: {type(e).__name__}", reason="unreachable"
            ) from e


def _to_department(group: dict[str, Any]) -> Department | None:
    name = str(group.get("GroupName", ""))
    if not name.startswith(GROUP_PREFIX):
        return None
    group_id = name[len(GROUP_PREFIX) :]
    if not group_id:
        return None
    description = str(group.get("Description") or "").strip()
    return Department(group_id=group_id, display_name=description or group_id)
