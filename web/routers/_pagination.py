"""Cursor helpers for DynamoDB-backed pagination in htmx routers."""

import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CURSOR_SECRET = os.environ.get("CURSOR_SECRET", "ai-persona-default-cursor-key")


def encode_cursor(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey dict to a signed, URL-safe string."""
    if not key:
        return None
    payload = base64.urlsafe_b64encode(json.dumps(key).encode()).decode()
    sig = hmac.new(_CURSOR_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}.{sig}"


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode and verify a signed cursor. Returns None on invalid/tampered input."""
    if not cursor or "." not in cursor:
        return None
    try:
        payload, sig = cursor.rsplit(".", 1)
        expected = hmac.new(_CURSOR_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            logger.warning("Cursor signature mismatch — possible tampering")
            return None
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception as e:
        logger.warning(f"Invalid cursor: {e}")
        return None
