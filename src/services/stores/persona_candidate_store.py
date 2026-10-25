"""DynamoDB reads and writes of generated personas (U5, ADR-008, NFR Design Q1 A).

``PersonaCandidates`` (PK ``candidate_id``, TTL ``expires_at``) holds the
generated personas until their owner saves them. Every rule that decides
*whether* something may happen lives in ``PersonaGenerationManager``; this
service only runs the conditional writes (security-design.md §2):

- ``claim``: one conditional update; the condition carries the owner, the
  expiry, "not saved" and "no live claim" checks, so nothing is compared after
  reading (NFR1.22). A failed condition raises :class:`CandidateUnavailable`
  whatever the reason (someone else's, expired, being saved, missing).
- ``release`` / ``delete`` / ``mark_saved``: only when the item still holds the
  caller's claim token; otherwise nothing changes and ``False`` comes back.
- ``confirm_claim`` / ``mark_saved``: also need "not saved yet", so a hold that
  went stale and was taken over never saves the candidate twice (R-04).

Retries are botocore's standard ones (``reliability-design.md`` §4).
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.persona_candidate import CLAIM_TIMEOUT_SECONDS, PersonaCandidate

logger = logging.getLogger(__name__)

#: ``BatchWriteItem`` accepts at most 25 requests.
WRITE_BATCH_LIMIT = 25
#: Re-sends of ``UnprocessedItems`` before giving up.
UNPROCESSED_RETRIES = 3

_KEY = "candidate_id"
#: The request still holds the candidate and nobody has saved it (R-04).
_HELD_AND_UNSAVED = "claim_token = :token AND attribute_not_exists(saved_at)"


class PersonaCandidateStoreError(CodedError):
    """A candidate read or write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class CandidateUnavailable(PersonaCandidateStoreError):
    """The claim condition failed (not told apart: owner, expiry, claim, missing)."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    """Comparable UTC text (ISO 8601 with a fixed precision)."""
    return _utc(value).isoformat(timespec="microseconds")


def _to_dynamo(value: Any) -> Any:
    """Floats become ``Decimal`` (``TypeSerializer`` refuses floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """``Decimal`` back to ``int`` / ``float``."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


def _attribute_size(value: Mapping[str, Any]) -> int:
    """Approximate DynamoDB size of one typed attribute value."""
    ((kind, inner),) = value.items()
    if kind == "S":
        return len(str(inner).encode("utf-8"))
    if kind == "N":
        return len(str(inner)) // 2 + 2
    if kind == "B":
        return len(inner)
    if kind in ("BOOL", "NULL"):
        return 1
    if kind == "M":
        return 3 + sum(
            len(k.encode("utf-8")) + _attribute_size(v) + 1 for k, v in inner.items()
        )
    if kind == "L":
        return 3 + sum(_attribute_size(v) + 1 for v in inner)
    if kind in ("SS", "NS", "BS"):
        return sum(len(str(v).encode("utf-8")) for v in inner)
    return len(str(inner).encode("utf-8"))  # pragma: no cover - unknown type


class PersonaCandidateStore:
    """Conditional reads and writes of ``PersonaCandidates``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``PersonaCandidates``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- items ----------------------------------------------------------------------

    @classmethod
    def item_size(cls, candidate: PersonaCandidate) -> int:
        """Size of the candidate as written: attribute names plus values (NFR2.12)."""
        serializer = TypeSerializer()
        item = _to_dynamo(cls._stored(candidate))
        return sum(
            len(name.encode("utf-8")) + _attribute_size(serializer.serialize(value))
            for name, value in item.items()
        )

    @staticmethod
    def _stored(candidate: PersonaCandidate) -> dict[str, Any]:
        data = candidate.to_dict()
        # Claim stamps are written by this store in one comparable format.
        if candidate.claimed_at is not None:
            data["claimed_at"] = _stamp(candidate.claimed_at)
        if candidate.saved_at is not None:
            data["saved_at"] = _stamp(candidate.saved_at)
        return data

    def _item(self, candidate: PersonaCandidate) -> dict[str, Any]:
        stored = _to_dynamo(self._stored(candidate))
        return {k: self._serializer.serialize(v) for k, v in stored.items()}

    def _candidate(self, item: Mapping[str, Any]) -> PersonaCandidate:
        plain = {
            k: _from_dynamo(self._deserializer.deserialize(v)) for k, v in item.items()
        }
        return PersonaCandidate.from_dict(plain)

    # --- writes ---------------------------------------------------------------------

    def put_all(self, candidates: Sequence[PersonaCandidate]) -> None:
        """Write the candidates of one generation (BR1.1).

        Raises:
            PersonaCandidateStoreError: A write failed or items stayed
                unprocessed after three re-sends.
        """
        requests = [{"PutRequest": {"Item": self._item(c)}} for c in candidates]
        for start in range(0, len(requests), WRITE_BATCH_LIMIT):
            self._batch_write(requests[start : start + WRITE_BATCH_LIMIT])

    def _batch_write(self, requests: list[dict[str, Any]]) -> None:
        pending = requests
        for _ in range(UNPROCESSED_RETRIES + 1):
            response = self._call(
                "batch_write_item", RequestItems={self.table_name: pending}
            )
            pending = response.get("UnprocessedItems", {}).get(self.table_name, [])
            if not pending:
                return
        logger.warning("BatchWriteItem left %d candidates unprocessed", len(pending))
        raise PersonaCandidateStoreError("candidates left unprocessed")

    def claim(
        self, candidate_id: str, owner_sub: str, token: str, now: datetime
    ) -> PersonaCandidate:
        """Mark one candidate as being saved by ``token`` (BR1.3).

        Returns:
            The candidate as claimed.

        Raises:
            CandidateUnavailable: Not the owner's, expired, saved, claimed less
                than five minutes ago, or missing.
            PersonaCandidateStoreError: DynamoDB failed.
        """
        stale = now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
        response = self._call(
            "update_item",
            TableName=self.table_name,
            Key={_KEY: {"S": candidate_id}},
            UpdateExpression="SET claimed_at = :now, claim_token = :token",
            ConditionExpression=(
                "attribute_exists(candidate_id) AND owner_sub = :owner"
                " AND expires_at > :now_epoch AND attribute_not_exists(saved_at)"
                " AND (attribute_not_exists(claimed_at) OR claimed_at < :stale)"
            ),
            ExpressionAttributeValues={
                ":now": {"S": _stamp(now)},
                ":token": {"S": token},
                ":owner": {"S": owner_sub},
                ":now_epoch": {"N": str(int(_utc(now).timestamp()))},
                ":stale": {"S": _stamp(stale)},
            },
            ReturnValues="ALL_NEW",
            unavailable=True,
        )
        return self._candidate(response["Attributes"])

    def release(self, candidate_id: str, token: str) -> bool:
        """Drop the claim of ``token``; ``False`` when it is not held."""
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key={_KEY: {"S": candidate_id}},
            UpdateExpression="REMOVE claimed_at, claim_token",
            ConditionExpression="claim_token = :token",
            ExpressionAttributeValues={":token": {"S": token}},
        )

    def delete(self, candidate_id: str, token: str) -> bool:
        """Delete a candidate claimed by ``token``; ``False`` otherwise."""
        return self._conditional(
            "delete_item",
            TableName=self.table_name,
            Key={_KEY: {"S": candidate_id}},
            ConditionExpression="claim_token = :token",
            ExpressionAttributeValues={":token": {"S": token}},
        )

    def confirm_claim(self, candidate_id: str, token: str, now: datetime) -> bool:
        """Check right before a save that ``token`` still holds the candidate.

        One conditional update (R-04): the item must still carry ``token`` and
        no ``saved_at``. The hold is renewed (``claimed_at = now``), so another
        request cannot take it over for the next five minutes.

        Returns:
            ``False`` when the hold was taken over, the candidate was saved or
            it is gone (nothing changes).
        """
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key={_KEY: {"S": candidate_id}},
            UpdateExpression="SET claimed_at = :now",
            ConditionExpression=_HELD_AND_UNSAVED,
            ExpressionAttributeValues={
                ":token": {"S": token},
                ":now": {"S": _stamp(now)},
            },
        )

    def mark_saved(self, candidate_id: str, token: str, now: datetime) -> bool:
        """Mark a candidate held by ``token`` and not yet saved as saved.

        The same condition as :meth:`confirm_claim` (R-04).

        Returns:
            ``False`` when not held by ``token`` or already saved.
        """
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key={_KEY: {"S": candidate_id}},
            UpdateExpression="SET saved_at = :now",
            ConditionExpression=_HELD_AND_UNSAVED,
            ExpressionAttributeValues={
                ":token": {"S": token},
                ":now": {"S": _stamp(now)},
            },
        )

    # --- reads ----------------------------------------------------------------------

    def get(self, candidate_id: str) -> PersonaCandidate | None:
        """Read one candidate with a strongly consistent ``GetItem`` (NFR9.27)."""
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key={_KEY: {"S": candidate_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._candidate(item) if item else None

    # --- plumbing -------------------------------------------------------------------

    def _conditional(self, operation: str, **kwargs: Any) -> bool:
        try:
            self._call(operation, unavailable=True, **kwargs)
        except CandidateUnavailable:
            return False
        return True

    def _call(
        self, operation: str, *, unavailable: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if unavailable and code == "ConditionalCheckFailedException":
                raise CandidateUnavailable(f"{operation} condition failed") from e
            logger.warning("%s on candidates failed: %s", operation, code)
            raise PersonaCandidateStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on candidates unreachable: %s", operation, type(e).__name__
            )
            raise PersonaCandidateStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
