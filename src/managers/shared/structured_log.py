"""
Structured auth / access log lines (NFR8.1, NFR8.2, NFR3.3).

Every auth event goes through :func:`log_event`, which writes exactly one JSON
object per line to standard output (ECS ships stdout to CloudWatch Logs, where
the metric filters of ``infrastructure-specification.md`` match ``$.event``).

Values whose field name is in :data:`FORBIDDEN_FIELDS` are dropped here, in
one place, so that tokens, session ids, authorization codes, PKCE verifiers,
passwords and cookies can never reach the log even if a caller passes them.
"""

import json
import sys
from datetime import datetime, timezone
from threading import Lock
from typing import Any, TextIO

#: Field names that are never written (compared case-insensitively).
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "session_id",
        "code",
        "state",
        "nonce",
        "code_verifier",
        "password",
        "cookie",
        "authorization",
        "client_secret",
    }
)

_write_lock = Lock()


def log_event(event: str, *, stream: TextIO | None = None, **fields: Any) -> None:
    """Write one JSON line with ``event``, ``ts`` and the allowed ``fields``.

    Args:
        event: Event name such as ``auth.login_succeeded``.
        stream: Output stream; standard output when omitted.
        **fields: Extra values. ``None`` values and forbidden names are skipped.
    """
    record: dict[str, Any] = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    for name, value in fields.items():
        if value is None or name.lower() in FORBIDDEN_FIELDS:
            continue
        record[name] = value
    line = json.dumps(record, ensure_ascii=False, default=str)
    target = stream if stream is not None else sys.stdout
    with _write_lock:
        target.write(line + "\n")
        target.flush()
