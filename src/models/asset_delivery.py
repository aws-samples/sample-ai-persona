"""Values of one file delivery (closed network U4, FR5.3).

``AssetDelivery`` (``src/managers/components/asset_delivery.py``) turns an
:class:`AssetHandle` into a :class:`DeliveryResult`: a presigned URL to redirect
to (``presigned``, the public behaviour) or an opened S3 body to stream through
the app (``proxy``, the closed network).

Rules (``construction/u4-asset-delivery/functional-design/rules.md``):

- BR2.3: ``upload`` files stop above the upload limit (``MAX_FILE_SIZE``), the
  survey result CSV above 200 MiB.
- BR2.7: only PNG, JPEG, WebP and PDF open inline; everything else is an
  attachment. The type always comes from the stored metadata.

Standard library only: the body is an iterator of bytes and a close callable,
so no boto3 type reaches this layer.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from .errors import ErrorCode

#: Content types the browser may open inside the page (BR2.7).
INLINE_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "application/pdf"}
)

#: The largest survey result CSV streamed through the app (BR2.3, Q1 A).
SURVEY_CSV_MAX_BYTES = 200 * 1024 * 1024


class AssetDeliveryMode(StrEnum):
    """How files reach the browser (``Config.ASSET_DELIVERY``, BR1.1)."""

    PRESIGNED = "presigned"
    PROXY = "proxy"


class SizeLimit(StrEnum):
    """Which size limit applies when a file is streamed (BR2.3)."""

    UPLOAD = "upload"
    SURVEY_CSV = "survey_csv"


class DeliveryKind(StrEnum):
    """The two shapes of a :class:`DeliveryResult`."""

    REDIRECT = "redirect"
    STREAM = "stream"


def max_bytes(limit: SizeLimit, *, upload_max_bytes: int) -> int:
    """The byte limit of ``limit``.

    Args:
        limit: The handle's size limit.
        upload_max_bytes: ``Config.MAX_FILE_SIZE`` (models cannot read config).
    """
    if limit is SizeLimit.SURVEY_CSV:
        return SURVEY_CSV_MAX_BYTES
    return upload_max_bytes


def disposition_for(content_type: str) -> str:
    """``inline`` for images and PDF, ``attachment`` for anything else (BR2.7)."""
    base = content_type.split(";", 1)[0].strip().lower()
    return "inline" if base in INLINE_CONTENT_TYPES else "attachment"


@dataclass(frozen=True)
class AssetHandle:
    """One delivery request, made by a Manager after its authorization (BR2.4).

    Attributes:
        s3_path: ``s3://`` path taken from stored metadata, never user input.
        content_type: MIME type from stored metadata.
        download_name: File name shown to the browser (may be Japanese).
        size_limit: The limit checked when streaming.
        not_found_code: The caller's "not found" code when the object is gone.
        presigned_expiration: Lifetime of the presigned URL in seconds.
        check_exists: Check the object before signing (``presigned`` only).
    """

    s3_path: str
    content_type: str
    download_name: str
    size_limit: SizeLimit
    not_found_code: ErrorCode
    presigned_expiration: int
    check_exists: bool = False

    def __post_init__(self) -> None:
        if self.presigned_expiration < 1:
            raise ValueError(
                f"presigned_expiration must be >= 1, got {self.presigned_expiration}"
            )


@dataclass(frozen=True)
class AssetStream:
    """An opened S3 object: its body in chunks, its size and how to close it.

    ``close`` may be called any number of times (BR4.5).
    """

    chunks: Iterator[bytes]
    content_length: int
    close: Callable[[], None]


@dataclass(frozen=True)
class DeliveryResult:
    """What a Manager hands to its Router: redirect to a URL or stream a body."""

    kind: DeliveryKind
    presigned_url: Optional[str] = None
    asset: Optional[AssetStream] = None
    content_type: Optional[str] = None
    disposition: Optional[str] = None
    download_name: Optional[str] = None

    @classmethod
    def redirect(cls, url: str) -> "DeliveryResult":
        """A result that sends the browser to a presigned URL (BR2.1)."""
        return cls(kind=DeliveryKind.REDIRECT, presigned_url=url)

    @classmethod
    def stream(
        cls, asset: AssetStream, content_type: str, download_name: str
    ) -> "DeliveryResult":
        """A result that streams ``asset`` through the app (BR2.2, BR2.7)."""
        return cls(
            kind=DeliveryKind.STREAM,
            asset=asset,
            content_type=content_type,
            disposition=disposition_for(content_type),
            download_name=download_name,
        )

    @property
    def content_length(self) -> Optional[int]:
        """The streamed body's size, or ``None`` for a redirect."""
        return self.asset.content_length if self.asset is not None else None
