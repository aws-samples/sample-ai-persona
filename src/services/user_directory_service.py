"""
Cognito ``ListUsers`` for the owner-name copy (BR5.1, Q3 of Functional Design).

Only ``sub``, ``name`` and whether the user is enabled are kept; e-mail
addresses are never requested (NFR2.7, security-requirements.md). The copy is
for display only (BR5.3).

Limits (``performance-design.md`` §3, same as ``CognitoGroupsService``):

- The whole listing, across every page, gets :data:`LIST_USERS_TIMEOUT_SECONDS`.
  A page is only requested when one page's worst case still fits.
- No retry (:data:`CLIENT_CONFIG`); ``OwnerDirectory`` keeps the previous copy
  and waits 60 seconds before asking again.

Failures raise :class:`UserDirectoryError` (``LOOKUP_UNAVAILABLE``).
"""

import logging
import time
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.sharing import OwnerSummary

logger = logging.getLogger(__name__)

#: Total time for one listing, all pages together.
LIST_USERS_TIMEOUT_SECONDS = 5.0

#: One attempt only; failures are handled by the caller's back-off.
CLIENT_CONFIG = Config(
    connect_timeout=0.5,
    read_timeout=1.5,
    retries={"total_max_attempts": 1},
)

#: Worst case of one page (connect + read timeout).
_PAGE_BUDGET_SECONDS = 2.0

#: ``ListUsers`` returns at most 60 users per page.
_PAGE_SIZE = 60


class UserDirectoryError(CodedError):
    """The user list could not be read.

    Attributes:
        reason: ``deadline`` or ``unreachable`` (for logs).
    """

    code = ErrorCode.LOOKUP_UNAVAILABLE

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class UserDirectoryService:
    """Reads the users of the user pool."""

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

    def list_users(self) -> list[OwnerSummary]:
        """Return every user that has a ``sub`` (pool order).

        Raises:
            UserDirectoryError: Out of time or Cognito unreachable.
        """
        deadline = self._clock() + LIST_USERS_TIMEOUT_SECONDS
        users: list[OwnerSummary] = []
        token: str | None = None
        while True:
            if deadline - self._clock() < _PAGE_BUDGET_SECONDS:
                raise UserDirectoryError(
                    "ListUsers did not finish within the time limit", reason="deadline"
                )
            response = self._list_page(token)
            users += [
                summary
                for user in response.get("Users", [])
                if (summary := _to_summary(user)) is not None
            ]
            token = response.get("PaginationToken")
            if not token:
                return users

    def _list_page(self, token: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "UserPoolId": self.user_pool_id,
            "Limit": _PAGE_SIZE,
            "AttributesToGet": ["sub", "name"],
        }
        if token:
            kwargs["PaginationToken"] = token
        try:
            response: dict[str, Any] = self.client.list_users(**kwargs)
            return response
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            logger.warning("ListUsers failed: %s", error_code)
            raise UserDirectoryError(
                f"ListUsers failed: {error_code}", reason="unreachable"
            ) from e
        except BotoCoreError as e:
            logger.warning("ListUsers unreachable: %s", type(e).__name__)
            raise UserDirectoryError(
                f"ListUsers unreachable: {type(e).__name__}", reason="unreachable"
            ) from e


def _to_summary(user: dict[str, Any]) -> OwnerSummary | None:
    attributes = {a.get("Name"): a.get("Value") for a in user.get("Attributes", [])}
    sub = attributes.get("sub")
    if not sub:
        return None
    name = (attributes.get("name") or "").strip()
    return OwnerSummary(
        sub=str(sub), display_name=name or None, enabled=bool(user.get("Enabled", True))
    )
