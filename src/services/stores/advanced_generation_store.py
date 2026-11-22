"""DynamoDB reads and writes of advanced generations (``AdvancedGenerations``, C5).

One table, two kinds of partitions (contract C5, scalability-design.md §2):

==============================  ============================  ===================
``pk``                          ``sk``                        item
==============================  ============================  ===================
``GEN#<generation_id>``         ``GENERATION``                the generation
``GEN#<generation_id>``         ``EVENT#<n>`` (zero-padded)   a progress event
``GEN#<generation_id>``         ``PART#<name>#<n>``           a result slice
``OWNER_LOCK#<owner_sub>``      ``LOCK``                      one run per user
==============================  ============================  ===================

The generation item also carries ``event_sequence``, the counter that numbers
the events. GSI ``owner_sub-created_at`` only sees generation items (the other
items have no ``created_at``) and is for display only, never for the lock.

The start writes the lock, the generation and the in-use marks of the source
files (``SourceFiles``, by table name: stores never import each other) in one
``TransactWriteItems`` (BR7.5, BR6.4). Every later state change is conditioned
on the current state (BR7.1); a failed condition is dropped and logged, and
the caller gets ``False``. Business rules stay in the Manager.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial
from typing import Any, Callable, Mapping, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.advanced_generation import (
    AdvancedGeneration,
    ControlRequest,
    EventType,
    GenerationEvent,
    GenerationStatus,
    OwnerLock,
    ResultPart,
    ResultPartName,
)
from src.models.errors import CodedError, ErrorCode
from src.models.source_file import SourceFileStatus

logger = logging.getLogger(__name__)

#: GSI that lists one user's generations (display only).
OWNER_INDEX = "owner_sub-created_at"
#: Digits of the event number in the sort key; 8 digits keep lexical order.
EVENT_DIGITS = 8
#: Largest event number (the upper bound of the re-read range).
MAX_EVENT_SEQUENCE = 10**EVENT_DIGITS - 1
#: Digits of the part index in the sort key.
PART_DIGITS = 4
#: ``BatchWriteItem`` accepts at most 25 requests.
WRITE_BATCH_LIMIT = 25
#: Re-sends of ``UnprocessedItems`` before giving up.
UNPROCESSED_RETRIES = 3
#: The lock item stays this long after ``lock_until`` before the TTL removes it.
LOCK_TTL_GRACE_SECONDS = 3600

_GENERATION_SK = "GENERATION"
_LOCK_SK = "LOCK"
_RUNNING_STATES = tuple(s for s in GenerationStatus if not s.is_terminal)


class AdvancedGenerationStoreError(CodedError):
    """A read or write of ``AdvancedGenerations`` failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class GenerationLockHeld(AdvancedGenerationStoreError):
    """The owner already has a generation whose lock has not expired (BR7.5)."""

    code = ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING


class GenerationSourceUnavailable(AdvancedGenerationStoreError):
    """A source file is not the owner's free ready table, or is missing (BR6.4)."""

    code = ErrorCode.GENERATION_SOURCE_NOT_FOUND


class GenerationResultTooLarge(AdvancedGenerationStoreError):
    """A result slice or the whole result is over its size limit (NFR5.1)."""

    code = ErrorCode.GENERATION_RESULT_TOO_LARGE


class _ConditionFailed(AdvancedGenerationStoreError):
    """Internal: a single-item condition failed."""


@dataclass(frozen=True)
class FinishOutcome:
    """Which of the independent finishing writes took effect (W5-8)."""

    lock_released: bool
    marks_released: bool
    finished_written: bool


# --- keys ---------------------------------------------------------------------------


def generation_pk(generation_id: str) -> str:
    """Partition key of everything that belongs to one generation."""
    return f"GEN#{generation_id}"


def lock_pk(owner_sub: str) -> str:
    """Partition key of one owner's lock."""
    return f"OWNER_LOCK#{owner_sub}"


def event_sk(sequence: int) -> str:
    """Sort key of an event; zero-padded so the text sorts like the number."""
    return f"EVENT#{sequence:0{EVENT_DIGITS}d}"


def part_prefix(name: ResultPartName) -> str:
    """Sort key prefix of the slices of one result list."""
    return f"PART#{name.value}#"


def part_sk(name: ResultPartName, index: int) -> str:
    """Sort key of one result slice."""
    return f"{part_prefix(name)}{index:0{PART_DIGITS}d}"


# --- conversions --------------------------------------------------------------------


def _stamp(value: datetime) -> str:
    """Comparable UTC text, the same format the models write."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


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
    return len(str(inner).encode("utf-8"))  # pragma: no cover - sets are unused


class AdvancedGenerationStore:
    """Conditional reads and writes of ``AdvancedGenerations``."""

    def __init__(
        self,
        *,
        region: str,
        table_name: str,
        source_files_table_name: str,
        client: Any = None,
    ) -> None:
        """Create the store.

        Args:
            region: AWS region of the tables.
            table_name: Physical name of ``AdvancedGenerations``.
            source_files_table_name: Physical name of ``SourceFiles`` (the
                in-use marks are written in the same transaction).
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.source_files_table_name = source_files_table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- items ----------------------------------------------------------------------

    def _serialize(self, value: Any) -> dict[str, Any]:
        result: dict[str, Any] = self._serializer.serialize(_to_dynamo(value))
        return result

    def _item(self, pk: str, sk: str, data: Mapping[str, Any]) -> dict[str, Any]:
        item = {k: self._serialize(v) for k, v in data.items()}
        item["pk"] = {"S": pk}
        item["sk"] = {"S": sk}
        return item

    def _plain(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            k: _from_dynamo(self._deserializer.deserialize(v))
            for k, v in item.items()
            if k not in ("pk", "sk")
        }

    @staticmethod
    def _key(pk: str, sk: str) -> dict[str, Any]:
        return {"pk": {"S": pk}, "sk": {"S": sk}}

    @staticmethod
    def _size(item: Mapping[str, Any]) -> int:
        return sum(
            len(name.encode("utf-8")) + _attribute_size(value)
            for name, value in item.items()
        )

    @classmethod
    def item_size(cls, data: Mapping[str, Any]) -> int:
        """Size of a generation item as written: its keys plus ``data`` (NFR5.1).

        Args:
            data: Plain attribute values (``AdvancedGeneration.to_dict()`` shape).
        """
        serializer = TypeSerializer()
        item: dict[str, Any] = {
            k: serializer.serialize(_to_dynamo(v)) for k, v in data.items()
        }
        item["pk"] = {"S": generation_pk(str(data.get("generation_id", "")))}
        item["sk"] = {"S": _GENERATION_SK}
        return cls._size(item)

    # --- start ----------------------------------------------------------------------

    def create_with_lock(
        self,
        generation: AdvancedGeneration,
        *,
        lock_until: datetime,
        in_use_until: datetime,
        now: datetime,
        source_expires_at: datetime | None = None,
    ) -> None:
        """Write the lock, the generation and the in-use marks at once (BR7.5, BR6.4).

        Nothing is written when any condition fails: a lock that has not
        expired, an existing generation, or a source file that is not the
        owner's unexpired ``ready`` table with a free mark. The same update
        stamps ``last_used_at`` and, when ``source_expires_at`` is given, the
        new ``expires_at`` of each file (U2 BR13.3). A file without
        ``expires_at`` (U1) passes the expiry part; the Manager checks it.

        Raises:
            GenerationLockHeld: The owner's lock is still held.
            GenerationSourceUnavailable: A source file failed its condition.
            AdvancedGenerationStoreError: Any other failure.
        """
        now_text = _stamp(now)
        lock = OwnerLock(generation.owner_sub, generation.generation_id, lock_until)
        lock_item = self._item(
            lock_pk(generation.owner_sub),
            _LOCK_SK,
            {
                **lock.to_dict(),
                "expires_at": _epoch(lock_until) + LOCK_TTL_GRACE_SECONDS,
            },
        )
        generation_item = self._item(
            generation_pk(generation.generation_id),
            _GENERATION_SK,
            generation.to_dict(),
        )
        transact: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": lock_item,
                    "ConditionExpression": (
                        "attribute_not_exists(pk) OR lock_until < :now"
                    ),
                    "ExpressionAttributeValues": {":now": {"S": now_text}},
                }
            },
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": generation_item,
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]
        mark_values: dict[str, Any] = {
            ":generation": {"S": generation.generation_id},
            ":until": {"S": _stamp(in_use_until)},
            ":now": {"S": now_text},
            ":epoch": {"N": str(_epoch(now))},
            ":owner": {"S": generation.owner_sub},
            ":ready": {"S": SourceFileStatus.READY.value},
        }
        mark_update = (
            "SET in_use_by = :generation, in_use_until = :until, last_used_at = :now"
        )
        if source_expires_at is not None:
            mark_update += ", expires_at = :expires"
            mark_values[":expires"] = {"N": str(_epoch(source_expires_at))}
        for source_file_id in generation.source_file_ids:
            transact.append(
                {
                    "Update": {
                        "TableName": self.source_files_table_name,
                        "Key": {"source_file_id": {"S": source_file_id}},
                        "UpdateExpression": mark_update,
                        "ConditionExpression": (
                            "owner_sub = :owner AND #status = :ready AND "
                            "(attribute_not_exists(expires_at) OR "
                            "expires_at > :epoch) AND "
                            "(attribute_not_exists(in_use_by) OR in_use_until < :now)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": mark_values,
                    }
                }
            )
        try:
            self.client.transact_write_items(TransactItems=transact)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                reasons = [
                    r.get("Code", "") for r in e.response.get("CancellationReasons", [])
                ]
                failed = [
                    i for i, r in enumerate(reasons) if r == "ConditionalCheckFailed"
                ]
                if 0 in failed:
                    raise GenerationLockHeld("owner lock is held") from e
                if any(i >= 2 for i in failed):
                    raise GenerationSourceUnavailable(
                        "a source file failed its condition"
                    ) from e
            logger.warning("start transaction of a generation failed: %s", code)
            raise AdvancedGenerationStoreError(f"start failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "start transaction of a generation unreachable: %s", type(e).__name__
            )
            raise AdvancedGenerationStoreError(
                f"start unreachable: {type(e).__name__}"
            ) from e

    # --- state ----------------------------------------------------------------------

    def transition(
        self,
        generation_id: str,
        expected: GenerationStatus,
        new_status: GenerationStatus,
        *,
        updates: Mapping[str, Any] | None = None,
        removes: Sequence[str] = (),
        unless_requested: bool = False,
    ) -> bool:
        """Move ``expected -> new_status`` and write ``updates`` with it (BR7.1).

        Args:
            generation_id: The generation.
            expected: The state the generation must be in.
            new_status: The state to move to.
            updates: Plain attribute values (``to_dict()`` shapes) to set.
            removes: Attributes to remove with the move (U5: the request and
                ``awaiting_since``).
            unless_requested: Also require that no control request is pending
                (U5 BR31.2: ``done`` never overwrites a cancel).

        Returns:
            ``False`` when the generation is missing, not in ``expected`` or
            (``unless_requested``) has a pending request (the write is dropped
            and logged, NFR5.18).
        """
        names = {"#status": "status"}
        values: dict[str, Any] = {
            ":new": {"S": new_status.value},
            ":expected": {"S": expected.value},
        }
        condition = "#status = :expected"
        if unless_requested:
            condition += " AND attribute_not_exists(control_request)"
        written = self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(generation_pk(generation_id), _GENERATION_SK),
            UpdateExpression=self._move_expression(names, values, updates, removes),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        if not written:
            logger.info(
                "advanced generation transition dropped: %s -> %s",
                expected.value,
                new_status.value,
            )
        return written

    def heartbeat(self, generation_id: str, *, now: datetime) -> bool:
        """Write ``heartbeat_at`` while the generation has not ended (BR7.3)."""
        values: dict[str, Any] = {":now": {"S": _stamp(now)}}
        placeholders = []
        for i, state in enumerate(_RUNNING_STATES):
            values[f":s{i}"] = {"S": state.value}
            placeholders.append(f":s{i}")
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(generation_pk(generation_id), _GENERATION_SK),
            UpdateExpression="SET heartbeat_at = :now",
            ConditionExpression=f"#status IN ({', '.join(placeholders)})",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )

    def _move_expression(
        self,
        names: dict[str, str],
        values: dict[str, Any],
        updates: Mapping[str, Any] | None,
        removes: Sequence[str],
    ) -> str:
        """``SET #status = :new, ... [REMOVE ...]``; fills ``names`` and ``values``."""
        sets = ["#status = :new"]
        for i, (name, value) in enumerate((updates or {}).items()):
            names[f"#u{i}"] = name
            values[f":u{i}"] = self._serialize(value)
            sets.append(f"#u{i} = :u{i}")
        expression = "SET " + ", ".join(sets)
        dropped = [r for r in dict.fromkeys(removes) if r not in (updates or {})]
        if dropped:
            for i, name in enumerate(dropped):
                names[f"#r{i}"] = name
            expression += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(dropped)))
        return expression

    # --- U5: control requests and the reader's end (reliability-design.md) ----------

    def put_control_request(
        self,
        generation_id: str,
        request: ControlRequest,
        *,
        allowed_statuses: Sequence[GenerationStatus],
        require_empty: bool,
    ) -> bool:
        """Write the pending request while the state allows it (BR29.3, BR31.1).

        Args:
            generation_id: The generation.
            request: The cancel / approve / replan.
            allowed_statuses: The states the generation must be in.
            require_empty: Only when no request is pending (approve, replan);
                a cancel overwrites a pending one.

        Returns:
            ``False`` when the state or the pending request does not allow it.
        """
        values: dict[str, Any] = {":request": self._serialize(request.to_dict())}
        placeholders = []
        for i, status in enumerate(dict.fromkeys(allowed_statuses)):
            values[f":s{i}"] = {"S": status.value}
            placeholders.append(f":s{i}")
        if not placeholders:
            return False
        condition = f"#status IN ({', '.join(placeholders)})"
        if require_empty:
            condition += " AND attribute_not_exists(control_request)"
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(generation_pk(generation_id), _GENERATION_SK),
            UpdateExpression="SET control_request = :request",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )

    def take_control_request(
        self,
        generation_id: str,
        expected: GenerationStatus,
        new_status: GenerationStatus,
        request: ControlRequest,
        *,
        updates: Mapping[str, Any] | None = None,
        removes: Sequence[str] = (),
    ) -> bool:
        """The run takes ``request``: remove it and move ``expected -> new_status``.

        The write is conditioned on the state and on the request being the one
        read (its action and time), so a cancel written in between wins.
        """
        names = {"#status": "status"}
        values: dict[str, Any] = {
            ":new": {"S": new_status.value},
            ":expected": {"S": expected.value},
            ":action": {"S": request.action.value},
            ":requested": {"S": _stamp(request.requested_at)},
        }
        expression = self._move_expression(
            names, values, updates, ("control_request", *removes)
        )
        names.update(
            {"#cr": "control_request", "#act": "action", "#req": "requested_at"}
        )
        written = self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(generation_pk(generation_id), _GENERATION_SK),
            UpdateExpression=expression,
            ConditionExpression=(
                # ``action`` is a DynamoDB reserved word: names stand for the path
                "#status = :expected AND #cr.#act = :action AND #cr.#req = :requested"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        if not written:
            logger.info(
                "advanced generation control request not taken: %s -> %s",
                expected.value,
                new_status.value,
            )
        return written

    def end_if_unchanged(
        self,
        generation_id: str,
        expected: GenerationStatus,
        new_status: GenerationStatus,
        *,
        field: str,
        value: datetime | None,
        updates: Mapping[str, Any] | None = None,
        removes: Sequence[str] = (),
    ) -> bool:
        """A reader ends a generation only if nothing moved since it read (BR32.1).

        Args:
            field: ``heartbeat_at`` (stalled, a cancel without a thread) or
                ``awaiting_since`` (the plan wait ran out).
            value: The value read; ``None`` means the attribute was missing.

        Returns:
            ``False`` when the state or ``field`` changed (the run is alive).
        """
        names = {"#status": "status", "#field": field}
        values: dict[str, Any] = {
            ":new": {"S": new_status.value},
            ":expected": {"S": expected.value},
        }
        if value is None:
            condition = "#status = :expected AND attribute_not_exists(#field)"
        else:
            values[":value"] = {"S": _stamp(value)}
            condition = "#status = :expected AND #field = :value"
        written = self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(generation_pk(generation_id), _GENERATION_SK),
            UpdateExpression=self._move_expression(names, values, updates, removes),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        if not written:
            logger.info("advanced generation reader end dropped: %s", expected.value)
        return written

    def enter_awaiting(
        self,
        generation_id: str,
        owner_sub: str,
        source_file_ids: Sequence[str],
        *,
        now: datetime,
        until: datetime,
        updates: Mapping[str, Any] | None = None,
    ) -> bool:
        """``planning -> awaiting_plan_approval`` and the longer lock, at once.

        One ``TransactWriteItems`` writes the move with ``awaiting_since`` and
        ``updates``, the lock's ``lock_until`` and each file's ``in_use_until``
        (NFR5.43). The lock and the marks are only touched while they belong
        to ``generation_id``.

        Returns:
            ``False`` when any condition failed (nothing is written).

        Raises:
            AdvancedGenerationStoreError: DynamoDB failed.
        """
        until_text = _stamp(until)
        names = {"#status": "status"}
        values: dict[str, Any] = {
            ":new": {"S": GenerationStatus.AWAITING_PLAN_APPROVAL.value},
            ":expected": {"S": GenerationStatus.PLANNING.value},
        }
        moved = {**(updates or {}), "awaiting_since": _stamp(now)}
        transact: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": self._key(generation_pk(generation_id), _GENERATION_SK),
                    "UpdateExpression": self._move_expression(names, values, moved, ()),
                    "ConditionExpression": "#status = :expected",
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            },
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": self._key(lock_pk(owner_sub), _LOCK_SK),
                    "UpdateExpression": (
                        "SET lock_until = :until, expires_at = :expires"
                    ),
                    "ConditionExpression": "generation_id = :generation",
                    "ExpressionAttributeValues": {
                        ":until": {"S": until_text},
                        ":expires": {"N": str(_epoch(until) + LOCK_TTL_GRACE_SECONDS)},
                        ":generation": {"S": generation_id},
                    },
                }
            },
        ]
        for source_file_id in source_file_ids:
            transact.append(
                {
                    "Update": {
                        "TableName": self.source_files_table_name,
                        "Key": {"source_file_id": {"S": source_file_id}},
                        "UpdateExpression": "SET in_use_until = :until",
                        "ConditionExpression": "in_use_by = :generation",
                        "ExpressionAttributeValues": {
                            ":until": {"S": until_text},
                            ":generation": {"S": generation_id},
                        },
                    }
                }
            )
        try:
            self.client.transact_write_items(TransactItems=transact)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                logger.info("advanced generation wait not entered: a condition failed")
                return False
            logger.warning("plan wait transaction failed: %s", code)
            raise AdvancedGenerationStoreError(f"plan wait failed: {code}") from e
        except BotoCoreError as e:
            logger.warning("plan wait transaction unreachable: %s", type(e).__name__)
            raise AdvancedGenerationStoreError(
                f"plan wait unreachable: {type(e).__name__}"
            ) from e
        return True

    # --- events ---------------------------------------------------------------------

    def append_event(
        self,
        generation_id: str,
        event_type: EventType,
        *,
        summary: str | None,
        now: datetime,
        expires_at: int,
    ) -> GenerationEvent:
        """Number and write one progress event (BR7.2).

        The number comes from ``event_sequence`` on the generation item (an
        atomic counter), so events of one generation never share a number.

        Raises:
            AdvancedGenerationStoreError: The generation is missing or DynamoDB
                failed.
        """
        try:
            response = self._call(
                "update_item",
                TableName=self.table_name,
                Key=self._key(generation_pk(generation_id), _GENERATION_SK),
                UpdateExpression="ADD event_sequence :one",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":one": {"N": "1"}},
                ReturnValues="UPDATED_NEW",
                conditional=True,
            )
        except _ConditionFailed as e:
            raise AdvancedGenerationStoreError("generation is missing") from e
        sequence = int(response["Attributes"]["event_sequence"]["N"])
        event = GenerationEvent.create_new(
            generation_id=generation_id,
            sequence=sequence,
            event_type=event_type,
            summary=summary,
            now=now,
        )
        self._call(
            "put_item",
            TableName=self.table_name,
            Item=self._item(
                generation_pk(generation_id),
                event_sk(sequence),
                {**event.to_dict(), "expires_at": int(expires_at)},
            ),
        )
        return event

    def read_events(
        self, generation_id: str, *, after_sequence: int | None = None
    ) -> list[GenerationEvent]:
        """Events after ``after_sequence`` (all of them when ``None``), in order.

        The range is closed on both ends (``BETWEEN``): the generation and part
        items of the same partition sort after ``EVENT#`` (NFR5.2).
        """
        pk = generation_pk(generation_id)
        if after_sequence is None:
            condition = "pk = :pk AND begins_with(sk, :prefix)"
            values: dict[str, Any] = {":pk": {"S": pk}, ":prefix": {"S": "EVENT#"}}
        else:
            if after_sequence >= MAX_EVENT_SEQUENCE:
                return []
            condition = "pk = :pk AND sk BETWEEN :low AND :high"
            values = {
                ":pk": {"S": pk},
                ":low": {"S": event_sk(after_sequence + 1)},
                ":high": {"S": event_sk(MAX_EVENT_SEQUENCE)},
            }
        return [
            GenerationEvent.from_dict(self._plain(item))
            for item in self._query(condition, values)
        ]

    # --- result parts ---------------------------------------------------------------

    def put_result_parts(
        self,
        generation_id: str,
        lists: Mapping[ResultPartName, Sequence[Mapping[str, Any]]],
        *,
        expires_at: int,
        max_part_bytes: int,
        max_total_bytes: int,
    ) -> dict[str, int]:
        """Split result lists into slices and write them (contract C5, NFR5.1).

        Every slice stays within ``max_part_bytes``; all slices together within
        ``max_total_bytes``. The limits are checked before anything is written.

        Returns:
            ``{name: number of slices}`` (the generation's ``part_counts``).

        Raises:
            GenerationResultTooLarge: One element alone is over a slice, or the
                total is over its limit.
            AdvancedGenerationStoreError: DynamoDB failed.
        """
        pk = generation_pk(generation_id)
        items: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for name, elements in lists.items():
            slices = self._split(pk, name, list(elements), expires_at, max_part_bytes)
            counts[name.value] = len(slices)
            items.extend(slices)
        total = sum(self._size(item) for item in items)
        if total > max_total_bytes:
            raise GenerationResultTooLarge("result parts are over the total limit")
        requests = [{"PutRequest": {"Item": item}} for item in items]
        for start in range(0, len(requests), WRITE_BATCH_LIMIT):
            self._batch_write(requests[start : start + WRITE_BATCH_LIMIT])
        return counts

    def _split(
        self,
        pk: str,
        name: ResultPartName,
        elements: list[Mapping[str, Any]],
        expires_at: int,
        max_part_bytes: int,
    ) -> list[dict[str, Any]]:
        def build(index: int, chunk: list[Mapping[str, Any]]) -> dict[str, Any]:
            part = ResultPart(name, index, tuple(dict(e) for e in chunk))
            return self._item(
                pk,
                part_sk(name, index),
                {**part.to_dict(), "expires_at": int(expires_at)},
            )

        slices: list[dict[str, Any]] = []
        chunk: list[Mapping[str, Any]] = []
        for element in elements:
            candidate = build(len(slices), [*chunk, element])
            if self._size(candidate) <= max_part_bytes:
                chunk.append(element)
                continue
            if not chunk:
                raise GenerationResultTooLarge("one result element is over a part")
            slices.append(build(len(slices), chunk))
            chunk = [element]
            if self._size(build(len(slices), chunk)) > max_part_bytes:
                raise GenerationResultTooLarge("one result element is over a part")
        if chunk:
            slices.append(build(len(slices), chunk))
        return slices

    def read_result_parts(
        self, generation_id: str, name: ResultPartName
    ) -> list[dict[str, Any]]:
        """The elements of one result list, slices joined in index order."""
        items = self._query(
            "pk = :pk AND begins_with(sk, :prefix)",
            {
                ":pk": {"S": generation_pk(generation_id)},
                ":prefix": {"S": part_prefix(name)},
            },
        )
        elements: list[dict[str, Any]] = []
        for item in items:
            elements.extend(ResultPart.from_dict(self._plain(item)).items)
        return elements

    # --- end ------------------------------------------------------------------------

    def finish(
        self,
        generation_id: str,
        owner_sub: str,
        source_file_ids: Sequence[str],
        *,
        now: datetime,
        expires_at: int,
    ) -> FinishOutcome:
        """Release the lock and the marks and write the end (W5-8, NFR5.15).

        Each step is tried on its own; one failure never skips the others.
        The lock and the marks are only removed while they still belong to
        ``generation_id``.
        """
        lock_released, marks_released = self.release_holds(
            generation_id, owner_sub, source_file_ids
        )
        finished_written = self._try(
            "write the end", partial(self._write_end, generation_id, now, expires_at)
        )
        return FinishOutcome(lock_released, marks_released, finished_written)

    def release_holds(
        self, generation_id: str, owner_sub: str, source_file_ids: Sequence[str]
    ) -> tuple[bool, bool]:
        """Release the lock and the in-use marks only, not the end (U5 review fix).

        Each step is tried on its own, and each only while it still belongs
        to ``generation_id``.

        Returns:
            ``(lock released, every mark released)``.
        """
        lock_released = self._try(
            "release the lock", partial(self.release_lock, owner_sub, generation_id)
        )
        marks = [
            self._try(
                "release an in-use mark",
                partial(self._release_mark, source_file_id, generation_id),
            )
            for source_file_id in source_file_ids
        ]
        return lock_released, all(marks)

    def release_lock(self, owner_sub: str, generation_id: str) -> bool:
        """Delete the owner's lock while it belongs to ``generation_id`` (ADR-009).

        Returns:
            ``False`` when the lock is gone or held by another generation.
        """
        return self._conditional(
            "delete_item",
            TableName=self.table_name,
            Key=self._key(lock_pk(owner_sub), _LOCK_SK),
            ConditionExpression="generation_id = :generation",
            ExpressionAttributeValues={":generation": {"S": generation_id}},
        )

    def _release_mark(self, source_file_id: str, generation_id: str) -> bool:
        return self._conditional(
            "update_item",
            TableName=self.source_files_table_name,
            Key={"source_file_id": {"S": source_file_id}},
            # U2 BR13.3: the last use was stamped at the start; only the mark goes
            UpdateExpression="REMOVE in_use_by, in_use_until",
            ConditionExpression="in_use_by = :generation",
            ExpressionAttributeValues={":generation": {"S": generation_id}},
        )

    def _write_end(self, generation_id: str, now: datetime, expires_at: int) -> bool:
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(generation_pk(generation_id), _GENERATION_SK),
            UpdateExpression="SET finished_at = :now, expires_at = :expires",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={
                ":now": {"S": _stamp(now)},
                ":expires": {"N": str(int(expires_at))},
            },
        )

    # --- reads ----------------------------------------------------------------------

    def get(self, generation_id: str) -> AdvancedGeneration | None:
        """Read one generation with a strongly consistent ``GetItem``."""
        item = self._get(generation_pk(generation_id), _GENERATION_SK)
        return AdvancedGeneration.from_dict(self._plain(item)) if item else None

    def get_lock(self, owner_sub: str) -> OwnerLock | None:
        """The owner's lock item, held or not (callers compare ``lock_until``)."""
        item = self._get(lock_pk(owner_sub), _LOCK_SK)
        return OwnerLock.from_dict(self._plain(item)) if item else None

    # --- plumbing -------------------------------------------------------------------

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key=self._key(pk, sk),
            ConsistentRead=True,
        )
        item: dict[str, Any] | None = response.get("Item")
        return item

    def _query(self, condition: str, values: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self.table_name,
                "KeyConditionExpression": condition,
                "ExpressionAttributeValues": values,
                "ConsistentRead": True,
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._call("query", **kwargs)
            items.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return items

    def _batch_write(self, requests: list[dict[str, Any]]) -> None:
        pending = requests
        for _ in range(UNPROCESSED_RETRIES + 1):
            response = self._call(
                "batch_write_item", RequestItems={self.table_name: pending}
            )
            pending = response.get("UnprocessedItems", {}).get(self.table_name, [])
            if not pending:
                return
        logger.warning("BatchWriteItem left %d result parts unprocessed", len(pending))
        raise AdvancedGenerationStoreError("result parts left unprocessed")

    @staticmethod
    def _try(step: str, write: Callable[[], bool]) -> bool:
        try:
            return bool(write())
        except AdvancedGenerationStoreError:
            logger.warning("advanced generation finish: could not %s", step)
            return False

    def _conditional(self, operation: str, **kwargs: Any) -> bool:
        try:
            self._call(operation, conditional=True, **kwargs)
        except _ConditionFailed:
            return False
        return True

    def _call(
        self, operation: str, *, conditional: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if conditional and code == "ConditionalCheckFailedException":
                raise _ConditionFailed(f"{operation} condition failed") from e
            logger.warning("%s on advanced generations failed: %s", operation, code)
            raise AdvancedGenerationStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on advanced generations unreachable: %s",
                operation,
                type(e).__name__,
            )
            raise AdvancedGenerationStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
