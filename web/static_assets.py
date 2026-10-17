"""Versioned URLs for the app's own static assets.

CloudFront caches ``/static/*`` (cdk/lib/constructs/cloudfront.ts). Without a
version in the URL a deploy is hidden behind the cached copy for up to a day
(old JS/CSS with new templates). ``static_url()`` appends ``?v=<content hash>``
and the static cache policy keys on ``v``, so a changed file is a new URL.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
_HASH_LENGTH = 12


@lru_cache(maxsize=None)
def static_url(path: str) -> str:
    """Return ``/static/<path>?v=<hash>`` for a file under ``web/static``.

    Args:
        path: Path relative to ``web/static`` (e.g. ``js/app.js``).

    Returns:
        The URL with the first 12 hex digits of the file's SHA-256 as ``v``,
        or the plain URL when the file does not exist (nothing to version).

    Raises:
        ValueError: When ``path`` points outside ``web/static``.
    """
    base = STATIC_DIR.resolve()
    target = (base / path).resolve()
    if base not in target.parents:
        raise ValueError(f"static path outside the static directory: {path!r}")
    url = f"/static/{path}"
    if not target.is_file():
        return url
    digest = hashlib.sha256(target.read_bytes()).hexdigest()[:_HASH_LENGTH]
    return f"{url}?v={digest}"
