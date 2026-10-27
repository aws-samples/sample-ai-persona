"""Turn a :class:`DeliveryResult` into a response (closed network U4, BR4.1).

Shared by the five file routes (avatar, result CSV, result image, template
image preview, discussion document). Router layer only: it reads the Models
value the Manager returned and imports no Service.

- ``redirect``: 302 to the presigned URL (the public behaviour).
- ``stream``: a ``StreamingResponse`` of the opened S3 body with
  ``Content-Type`` / ``Content-Length`` / ``Content-Disposition`` (RFC 5987 for
  Japanese names) / ``X-Content-Type-Options: nosniff`` / ``Cache-Control``.
  The body is closed when the response ends on any path: read to the end, S3
  failing mid-way, or sending failing before or after the first chunk (BR4.5).
"""

from collections.abc import Iterator
from typing import Any, Optional
from urllib.parse import quote

from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from src.models.asset_delivery import AssetStream, DeliveryKind, DeliveryResult

#: ``Cache-Control`` of a streamed file unless the route says otherwise (BR4.3).
STREAM_CACHE_CONTROL = "private, no-store"


def content_disposition(disposition: str, filename: str) -> str:
    """``Content-Disposition`` with an ASCII fallback and the RFC 5987 name.

    Control characters are replaced first: a stored name (a survey name, an
    upload's file name) must never break the header line.
    """
    filename = "".join("_" if ord(c) < 0x20 or ord(c) == 0x7F else c for c in filename)
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_")
    return (
        f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    )


def asset_response(
    result: DeliveryResult, cache_control: Optional[str] = None
) -> Response:
    """The response of one delivered file.

    Args:
        result: What the Manager returned.
        cache_control: ``Cache-Control`` for both shapes (the avatar keeps
            ``private, max-age=300``). When omitted, a redirect carries none
            and a stream carries :data:`STREAM_CACHE_CONTROL`.
    """
    if result.kind is DeliveryKind.REDIRECT:
        assert result.presigned_url is not None
        headers = {"Cache-Control": cache_control} if cache_control else None
        return RedirectResponse(result.presigned_url, status_code=302, headers=headers)
    asset = result.asset
    assert asset is not None
    return _ClosingStreamingResponse(
        asset,
        _body(asset),
        media_type=result.content_type,
        headers={
            "Content-Length": str(asset.content_length),
            "Content-Disposition": content_disposition(
                result.disposition or "attachment", result.download_name or "file"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": cache_control or STREAM_CACHE_CONTROL,
        },
    )


class _ClosingStreamingResponse(StreamingResponse):
    """A ``StreamingResponse`` that always closes the S3 body when it ends.

    Starlette skips the background task when sending fails (a dropped
    connection surfaces as ``ClientDisconnect``), and the body iterator is then
    abandoned mid-way or never started, so its own ``finally`` would wait for
    GC. Wrapping the whole response closes the body on every path (BR4.5).
    """

    def __init__(self, asset: AssetStream, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._asset = asset

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._asset.close()


def _body(asset: AssetStream) -> Iterator[bytes]:
    """The chunks; closes the body when they end or fail (BR4.5)."""
    try:
        yield from asset.chunks
    finally:
        asset.close()
