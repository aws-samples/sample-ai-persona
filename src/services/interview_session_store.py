"""DynamoDB reads and writes of running interviews (U6, ADR-004).

``InterviewSessions`` (PK ``id``, TTL ``expires_at``) and ``InterviewMessages``
(PK ``session_id``, SK ``seq`` N, TTL ``expires_at``). Reads are strongly
consistent so every task sees the same ``last_seq`` and reply mark
(NFR9.31). Who may use a session and when it expired are decided by
``InterviewManager``; this service runs the conditional transactions of a send
(``reliability-design.md`` §3):

- (a) :meth:`append_user_message`: the user's message (``seq = n + 1``) and the
  session (``last_seq``, reply mark, last action) in one transaction,
  conditioned on ``last_seq = n`` and "no reply mark, or one older than five
  minutes".
- (b) :meth:`append_reply`: one persona's reply and the session, conditioned on
  ``last_seq`` and the caller's ``reply_token``; the last reply removes the
  mark, the others renew ``reply_started_at``.

A failed condition or a ``TransactionConflict`` raises
:class:`InterviewConflict` (the Manager answers ``CONCURRENT_UPDATE``);
anything else raises :class:`InterviewSessionStoreError`.
"""

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.interview_session import (
    REPLY_TIMEOUT_SECONDS,
    InterviewMessage,
    InterviewSession,
    stamp,
)

logger = logging.getLogger(__name__)

_ID = "id"
_SESSION_ID = "session_id"
_SEQ = "seq"
#: The mark is absent, or older than five minutes (a crashed task's mark).
_NO_LIVE_MARK = "(attribute_not_exists(#token) OR #started < :stale)"


class InterviewSessionStoreError(CodedError):
    """An interview read or write failed."""

    code = ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED


class InterviewConflict(InterviewSessionStoreError):
    """``last_seq`` or the reply mark changed (another send got there first)."""


def _decimal_free(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _decimal_free(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_free(v) for v in value]
    return value


class InterviewSessionStore:
    """Conditional reads and writes of ``InterviewSessions`` / ``InterviewMessages``."""

    def __init__(
        self,
        *,
        region: str,
        sessions_table: str,
        messages_table: str,
        client: Any = None,
    ) -> None:
        """Create the store.

        Args:
            region: AWS region of the tables.
            sessions_table: Physical name of ``InterviewSessions``.
            messages_table: Physical name of ``InterviewMessages``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.sessions_table = sessions_table
        self.messages_table = messages_table
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- sessions ------------------------------------------------------------------

    def create(self, session: InterviewSession) -> None:
        """Write a new session (BR3.1).

        Raises:
            InterviewSessionStoreError: The ID exists or DynamoDB failed.
        """
        self._call(
            "put_item",
            TableName=self.sessions_table,
            Item=self._item(session.to_dict()),
            ConditionExpression="attribute_not_exists(#id)",
            ExpressionAttributeNames={"#id": _ID},
        )

    def get(self, session_id: str) -> InterviewSession | None:
        """Read one session with a strongly consistent ``GetItem``."""
        response = self._call(
            "get_item",
            TableName=self.sessions_table,
            Key={_ID: {"S": session_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return InterviewSession.from_dict(self._plain(item)) if item else None

    def extend(self, session: InterviewSession) -> None:
        """Write the session's last action and expiry (BR3.5)."""
        self._call(
            "update_item",
            TableName=self.sessions_table,
            Key={_ID: {"S": session.id}},
            UpdateExpression="SET #active = :active, #expires = :expires",
            ConditionExpression="attribute_exists(#id)",
            ExpressionAttributeNames={
                "#id": _ID,
                "#active": "last_active_at",
                "#expires": "expires_at",
            },
            ExpressionAttributeValues={
                ":active": {"S": stamp(session.last_active_at)},
                ":expires": {"N": str(session.expires_at)},
            },
        )

    def end(self, session_id: str) -> None:
        """Make the session unusable (``DELETE /interview/{id}``).

        The session item is removed; its messages are unreachable without it
        and expire with their TTL.
        """
        self._call(
            "delete_item", TableName=self.sessions_table, Key={_ID: {"S": session_id}}
        )

    def mark_saved(self, session_id: str, discussion_id: str, *, now: datetime) -> None:
        """Record the saved discussion, unless a reply is in progress (NFR1.60).

        Raises:
            InterviewConflict: A reply mark younger than five minutes.
        """
        try:
            self.client.update_item(
                TableName=self.sessions_table,
                Key={_ID: {"S": session_id}},
                UpdateExpression="SET #saved = :saved",
                ConditionExpression=f"attribute_exists(#id) AND {_NO_LIVE_MARK}",
                ExpressionAttributeNames={
                    "#id": _ID,
                    "#saved": "saved_discussion_id",
                    "#token": "reply_token",
                    "#started": "reply_started_at",
                },
                ExpressionAttributeValues={
                    ":saved": {"S": discussion_id},
                    ":stale": {"S": self._stale(now)},
                },
            )
        except ClientError as e:
            raise self._classify(e) from e
        except BotoCoreError as e:
            raise self._unreachable("update_item", e) from e

    # --- a send --------------------------------------------------------------------

    def append_user_message(
        self,
        session: InterviewSession,
        message: InterviewMessage,
        *,
        token: str,
        now: datetime,
    ) -> InterviewSession:
        """(a) Write the user's message and put the reply mark (BR3.3).

        ``session`` is the session as read (its ``last_seq`` is the condition)
        with the attachments of this send already added. ``message.seq`` must
        be ``session.last_seq + 1``.

        Returns:
            The session as written.

        Raises:
            InterviewConflict: ``last_seq`` moved or a live reply mark exists.
        """
        updated = session.touched(now)
        names = {
            "#seq": "last_seq",
            "#token": "reply_token",
            "#started": "reply_started_at",
            "#active": "last_active_at",
            "#expires": "expires_at",
            "#docids": "document_ids",
            "#docs": "documents",
        }
        values = {
            ":expected": {"N": str(session.last_seq)},
            ":seq": {"N": str(message.seq)},
            ":token": {"S": token},
            ":now": {"S": stamp(now)},
            ":stale": {"S": self._stale(now)},
            ":expires": {"N": str(updated.expires_at)},
            ":docids": self._serializer.serialize(list(session.document_ids)),
            ":docs": self._serializer.serialize(
                [d.to_dict() for d in session.documents]
            ),
        }
        self._transact(
            [
                self._put_message(message),
                {
                    "Update": {
                        "TableName": self.sessions_table,
                        "Key": {_ID: {"S": session.id}},
                        "UpdateExpression": (
                            "SET #seq = :seq, #token = :token, #started = :now, "
                            "#active = :now, #expires = :expires, "
                            "#docids = :docids, #docs = :docs"
                        ),
                        "ConditionExpression": f"#seq = :expected AND {_NO_LIVE_MARK}",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                },
            ]
        )

        return replace(
            updated, last_seq=message.seq, reply_token=token, reply_started_at=now
        )

    def append_reply(
        self,
        session_id: str,
        message: InterviewMessage,
        *,
        expected_seq: int,
        token: str,
        now: datetime,
        last: bool,
    ) -> None:
        """(b) Write one persona's reply (BR3.3, NFR1.59).

        Args:
            expected_seq: ``last_seq`` before this reply.
            last: The last persona of the send: the mark is removed; otherwise
                ``reply_started_at`` is renewed to ``now``.

        Raises:
            InterviewConflict: The mark was taken over or ``last_seq`` moved.
        """
        names = {
            "#seq": "last_seq",
            "#token": "reply_token",
            "#active": "last_active_at",
        }
        values: dict[str, Any] = {
            ":expected": {"N": str(expected_seq)},
            ":seq": {"N": str(message.seq)},
            ":token": {"S": token},
            ":now": {"S": stamp(now)},
        }
        names["#started"] = "reply_started_at"
        if last:
            expression = "SET #seq = :seq, #active = :now REMOVE #token, #started"
        else:
            expression = "SET #seq = :seq, #active = :now, #started = :now"
        self._transact(
            [
                self._put_message(message),
                {
                    "Update": {
                        "TableName": self.sessions_table,
                        "Key": {_ID: {"S": session_id}},
                        "UpdateExpression": expression,
                        "ConditionExpression": "#seq = :expected AND #token = :token",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                },
            ]
        )

    def renew_reply(self, session_id: str, token: str, now: datetime) -> bool:
        """Renew the mark's time; ``False`` when ``token`` no longer holds it."""
        return self._conditional_mark(
            session_id,
            "SET #started = :now",
            token,
            {":now": {"S": stamp(now)}},
        )

    def release_reply(self, session_id: str, token: str) -> bool:
        """Remove the caller's mark; ``False`` when it does not hold it."""
        return self._conditional_mark(session_id, "REMOVE #token, #started", token, {})

    def _conditional_mark(
        self,
        session_id: str,
        expression: str,
        token: str,
        values: dict[str, Any],
    ) -> bool:
        try:
            self.client.update_item(
                TableName=self.sessions_table,
                Key={_ID: {"S": session_id}},
                UpdateExpression=expression,
                ConditionExpression="#token = :token",
                ExpressionAttributeNames={
                    "#token": "reply_token",
                    "#started": "reply_started_at",
                },
                ExpressionAttributeValues={":token": {"S": token}, **values},
            )
        except ClientError as e:
            if isinstance(self._classify(e), InterviewConflict):
                return False
            raise self._classify(e) from e
        except BotoCoreError as e:
            raise self._unreachable("update_item", e) from e
        return True

    # --- messages ------------------------------------------------------------------

    def query_messages(self, session_id: str, *, up_to: int) -> list[InterviewMessage]:
        """Messages ``1 .. up_to`` in order, through every page (BR3.4)."""
        params: dict[str, Any] = {
            "TableName": self.messages_table,
            "KeyConditionExpression": "#s = :s AND #q <= :n",
            "ExpressionAttributeNames": {"#s": _SESSION_ID, "#q": _SEQ},
            "ExpressionAttributeValues": {
                ":s": {"S": session_id},
                ":n": {"N": str(up_to)},
            },
            "ConsistentRead": True,
            "ScanIndexForward": True,
        }
        messages: list[InterviewMessage] = []
        while True:
            response = self._call("query", **params)
            messages += [
                InterviewMessage.from_dict(self._plain(i))
                for i in response.get("Items", [])
            ]
            start = response.get("LastEvaluatedKey")
            if not start:
                return messages
            params["ExclusiveStartKey"] = start

    # --- plumbing ------------------------------------------------------------------

    def _put_message(self, message: InterviewMessage) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self.messages_table,
                "Item": self._item(message.to_dict()),
                "ConditionExpression": "attribute_not_exists(#q)",
                "ExpressionAttributeNames": {"#q": _SEQ},
            }
        }

    @staticmethod
    def _stale(now: datetime) -> str:
        return stamp(now - timedelta(seconds=REPLY_TIMEOUT_SECONDS))

    def _transact(self, items: list[dict[str, Any]]) -> None:
        try:
            self.client.transact_write_items(TransactItems=items)
        except ClientError as e:
            raise self._classify(e) from e
        except BotoCoreError as e:
            raise self._unreachable("transact_write_items", e) from e

    @staticmethod
    def _classify(error: ClientError) -> InterviewSessionStoreError:
        code = error.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return InterviewConflict("session condition failed")
        if code != "TransactionCanceledException":
            logger.warning("Interview write failed: %s", code)
            return InterviewSessionStoreError(f"write failed: {code}")
        reasons = [
            str(r.get("Code") or "None")
            for r in error.response.get("CancellationReasons", [])
        ]
        if "TransactionConflict" in reasons or "ConditionalCheckFailed" in reasons:
            return InterviewConflict("send conflict")
        logger.warning("Interview transaction cancelled: %s", ",".join(reasons))
        return InterviewSessionStoreError("transaction cancelled for another reason")

    def _item(self, data: Mapping[str, Any]) -> dict[str, Any]:
        return {k: self._serializer.serialize(v) for k, v in data.items()}

    def _plain(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            k: _decimal_free(self._deserializer.deserialize(v)) for k, v in item.items()
        }

    @staticmethod
    def _unreachable(
        operation: str, error: BaseException
    ) -> InterviewSessionStoreError:
        logger.warning(
            "%s on interviews unreachable: %s", operation, type(error).__name__
        )
        return InterviewSessionStoreError(
            f"{operation} unreachable: {type(error).__name__}"
        )

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.warning("%s on interviews failed: %s", operation, code)
            raise InterviewSessionStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            raise self._unreachable(operation, e) from e
