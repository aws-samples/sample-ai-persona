"""
DynamoDB Sessions table access for ``oidc`` / ``admin_api`` LoginSessions.

Timeouts and retries follow ``reliability-design.md``: connect 0.5 s, read
0.5 s, two attempts in botocore's ``standard`` retry mode. A healthy table
answers in well under that; an unreachable one fails in about 2 seconds plus
the retry back-off. Under throttling the ``standard`` mode's back-off can
stretch one call to roughly 3 seconds; no overall deadline is applied on top
(NFR Design review R-09).

Every read is strongly consistent so that a token refresh written by another
task is visible immediately (``performance-design.md``). The two write kinds
of :mod:`src.models.login_session` map to two disjoint ``UpdateItem`` calls so
that neither overwrites the other's attributes (BR1.10, BR1.12).

U8 adds the LoginChallenge items (``challenge#`` keys, ``item_type``) of the
first password change: strong reads, a conditional ``DeleteItem`` that takes a
challenge out exactly once, and a conditional write-back (BR2.7, NFR3.17).

Failures to reach the table raise :class:`SessionStoreError`
(``SESSION_STORE_UNAVAILABLE``). Messages never contain the session id hash.
"""

import logging
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.login_challenge import CHALLENGE_ITEM_TYPE, LoginChallenge
from src.models.login_session import LoginSession

logger = logging.getLogger(__name__)

_KEY = "session_id_hash"
_CONDITION_FAILED = "ConditionalCheckFailedException"

#: botocore settings for the Sessions table (reliability-design.md).
#: ``total_max_attempts`` counts the first call, so 2 means "two attempts in
#: total". botocore reads ``max_attempts`` as the number of *retries* and would
#: turn ``max_attempts=2`` into three attempts.
CLIENT_CONFIG = Config(
    connect_timeout=0.5,
    read_timeout=0.5,
    retries={"total_max_attempts": 2, "mode": "standard"},
)


class SessionStoreError(CodedError):
    """The Sessions table could not be reached or answered with an error."""

    code = ErrorCode.SESSION_STORE_UNAVAILABLE


class SessionStoreService:
    """LoginSession storage in the DynamoDB Sessions table."""

    def __init__(self, table_name: str, region: str, client: Any = None) -> None:
        """Create the store.

        Args:
            table_name: Physical name of the Sessions table (from CDK).
            region: AWS region of the table.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client(
            "dynamodb", region_name=region, config=CLIENT_CONFIG
        )
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    def _key(self, session_id_hash: str) -> dict[str, Any]:
        return {_KEY: {"S": session_id_hash}}

    def _values(self, values: dict[str, Any]) -> dict[str, Any]:
        return {k: self._serializer.serialize(v) for k, v in values.items()}

    def _call(self, operation: str, **kwargs: Any) -> Any:
        """Run one client call, mapping failures to :class:`SessionStoreError`.

        A failed condition is returned as ``None`` so callers can report it.
        """
        try:
            return getattr(self.client, operation)(TableName=self.table_name, **kwargs)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == _CONDITION_FAILED:
                return None
            logger.warning("Sessions table %s failed: %s", operation, error_code)
            raise SessionStoreError(
                f"Sessions table {operation} failed: {error_code}"
            ) from e
        except BotoCoreError as e:
            logger.warning(
                "Sessions table %s unreachable: %s", operation, type(e).__name__
            )
            raise SessionStoreError(
                f"Sessions table {operation} unreachable: {type(e).__name__}"
            ) from e

    def _read_item(self, key: str) -> dict[str, Any] | None:
        response = self._call("get_item", Key=self._key(key), ConsistentRead=True)
        item = (response or {}).get("Item")
        if not item:
            return None
        return self._deserialize(item)

    def _deserialize(self, item: dict[str, Any]) -> dict[str, Any]:
        return {k: self._deserializer.deserialize(v) for k, v in item.items()}

    def get(self, session_id_hash: str) -> LoginSession | None:
        """Read one session with a strongly consistent ``GetItem``.

        A LoginChallenge item (``item_type="challenge"``, U8 BR2.2) is never a
        session and reads as absent.
        """
        data = self._read_item(session_id_hash)
        if data is None or data.get("item_type") == CHALLENGE_ITEM_TYPE:
            return None
        return LoginSession.from_dict(data)

    # --- LoginChallenge (U8 BR2.2, BR2.7, NFR3.17) ------------------------------

    @staticmethod
    def _challenge_or_none(data: dict[str, Any] | None) -> LoginChallenge | None:
        if data is None:
            return None
        try:
            return LoginChallenge.from_dict(data)
        except ValueError:
            # An incomplete 3-minute item is treated as gone (the user signs in again).
            logger.warning("Sessions table holds an unreadable challenge item")
            return None

    def put_challenge(self, challenge: LoginChallenge) -> None:
        """Store a new first-password-change challenge."""
        self._call("put_item", Item=self._values(challenge.to_dict()))

    def get_challenge(self, key: str) -> LoginChallenge | None:
        """Read a challenge with a strong read; ``None`` for any other item."""
        return self._challenge_or_none(self._read_item(key))

    def claim_challenge(self, key: str) -> LoginChallenge | None:
        """Take the challenge out once: conditional ``DeleteItem`` + ``ALL_OLD``.

        Returns:
            The removed challenge, or ``None`` when it was already taken (or
            the item is not a challenge, which is then left untouched).
        """
        response = self._call(
            "delete_item",
            Key=self._key(key),
            ConditionExpression=f"attribute_exists({_KEY}) AND #type = :challenge",
            ExpressionAttributeNames={"#type": "item_type"},
            ExpressionAttributeValues=self._values({":challenge": CHALLENGE_ITEM_TYPE}),
            ReturnValues="ALL_OLD",
        )
        attributes = (response or {}).get("Attributes")
        if not attributes:
            return None
        return self._challenge_or_none(self._deserialize(attributes))

    def restore_challenge(self, challenge: LoginChallenge) -> bool:
        """Write a claimed challenge back unchanged, only if the key is free.

        Returns:
            ``False`` when an item with the key already exists.
        """
        response = self._call(
            "put_item",
            Item=self._values(challenge.to_dict()),
            ConditionExpression=f"attribute_not_exists({_KEY})",
        )
        return response is not None

    def put(self, session: LoginSession) -> None:
        """Write the whole item (a new login)."""
        self._call("put_item", Item=self._values(session.to_dict()))

    def touch(self, session: LoginSession) -> None:
        """Write ``last_accessed_at`` only, if the item exists (BR1.10).

        ``version`` and the token attributes are left untouched. A missing
        item (logged out or expired meanwhile) is not recreated.
        """
        values = session.last_access_attributes()
        self._call(
            "update_item",
            Key=self._key(session.session_id_hash),
            UpdateExpression="SET #last = :last",
            ConditionExpression=f"attribute_exists({_KEY})",
            ExpressionAttributeNames={"#last": "last_accessed_at"},
            ExpressionAttributeValues=self._values(
                {":last": values["last_accessed_at"]}
            ),
        )

    def update_versioned(self, session: LoginSession, expected_version: int) -> bool:
        """Write tokens, roles, departments and failure state if versions match.

        Returns:
            ``False`` when another writer got there first (BR1.12).
        """
        to_set, to_remove = session.versioned_attributes()
        names: dict[str, str] = {}
        values: dict[str, Any] = {":expected": expected_version}
        set_parts: list[str] = []
        for i, (name, value) in enumerate(to_set.items()):
            names[f"#s{i}"] = name
            values[f":s{i}"] = value
            set_parts.append(f"#s{i} = :s{i}")
        remove_parts: list[str] = []
        for i, name in enumerate(to_remove):
            names[f"#r{i}"] = name
            remove_parts.append(f"#r{i}")
        names["#version"] = "version"
        expression = "SET " + ", ".join(set_parts)
        if remove_parts:
            expression += " REMOVE " + ", ".join(remove_parts)
        response = self._call(
            "update_item",
            Key=self._key(session.session_id_hash),
            UpdateExpression=expression,
            ConditionExpression=f"attribute_exists({_KEY}) AND #version = :expected",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=self._values(values),
        )
        return response is not None

    def delete(self, session_id_hash: str, expected_version: int | None = None) -> bool:
        """Delete one session, optionally only if ``version`` matches.

        Returns:
            ``False`` when the version condition failed.
        """
        kwargs: dict[str, Any] = {"Key": self._key(session_id_hash)}
        if expected_version is not None:
            kwargs["ConditionExpression"] = "#version = :expected"
            kwargs["ExpressionAttributeNames"] = {"#version": "version"}
            kwargs["ExpressionAttributeValues"] = self._values(
                {":expected": expected_version}
            )
        return self._call("delete_item", **kwargs) is not None
