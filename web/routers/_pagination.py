"""Cursor helpers for DynamoDB-backed pagination in htmx routers."""

import base64
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def encode_cursor(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey dict to a URL-safe base64 string."""
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a base64 cursor. Returns None on invalid input."""
    if not cursor:
        return None
    try:
        result: Dict[str, Any] = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return result
    except Exception as e:
        logger.warning(f"Invalid cursor: {e}")
        return None
