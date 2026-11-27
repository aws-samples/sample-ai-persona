"""Small conversions shared by the advanced generation (beta) models.

Times are stored as ISO 8601 text in UTC with a fixed precision, so the text
sorts and compares like the time itself (the stores put them into DynamoDB
condition expressions such as ``lock_until < :now``). TTL attributes are epoch
seconds. Naive datetimes are read as UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc(value: datetime) -> datetime:
    """``value`` in UTC (naive values are taken as UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    """Comparable ISO 8601 text of ``value`` (UTC, microsecond precision)."""
    return utc(value).isoformat(timespec="microseconds")


def parse_dt(value: Any) -> datetime:
    """Read an ISO 8601 text written by :func:`iso`."""
    return utc(datetime.fromisoformat(str(value)))


def parse_optional_dt(value: Any) -> datetime | None:
    """Like :func:`parse_dt`, but ``None`` / empty stays ``None``."""
    return parse_dt(value) if value else None


def epoch(value: datetime) -> int:
    """Epoch seconds of ``value`` (the DynamoDB TTL format)."""
    return int(utc(value).timestamp())


def drop_none(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` without the ``None`` values (``to_dict()`` never stores them)."""
    return {k: v for k, v in data.items() if v is not None}
