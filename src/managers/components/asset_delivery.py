"""How a file reaches the browser (closed network U4, FR5.3, ADR-003).

Five entry points in four Managers (``PersonaManager``, ``SurveyExecutionManager``,
``FileManager``, ``DiscussionManager``) hand files to the browser, and Managers
cannot import each other, so the delivery lives in this component
(``.claude/rules/architecture.md``, Component layer).

Rules (``construction/u4-asset-delivery/functional-design/rules.md``):

- BR2.1: ``presigned`` signs a URL (after an existence check when the handle
  asks for one) and never opens the object.
- BR2.2: ``proxy`` opens the object before anything is sent and returns a
  stream of 256 KiB chunks.
- BR2.3: a streamed file above its limit is closed and refused with
  ``ASSET_TOO_LARGE``; not one byte is sent.
- BR2.4: no authorization here. A handle exists only after the Manager's own
  checks passed.
- BR2.5 / BR2.6: a missing object becomes the handle's ``not_found_code``;
  other S3 failures stay ``S3_OPERATION_FAILED``.
- BR2.7: the content type and disposition come from the stored metadata.

The ``S3Service`` and the mode are given to the constructor; this component
never reads ``service_factory``.
"""

from typing import TYPE_CHECKING, Optional

from ...config import config
from ...models.asset_delivery import (
    AssetDeliveryMode,
    AssetHandle,
    DeliveryResult,
    max_bytes,
)
from ...models.errors import CodedError, ErrorCode

if TYPE_CHECKING:
    from ...services.s3_service import S3Service


class AssetDelivery:
    """Turns an authorized :class:`AssetHandle` into a :class:`DeliveryResult`."""

    def __init__(self, s3_service: Optional["S3Service"], mode: str) -> None:
        """
        Args:
            s3_service: The app bucket (``None`` without S3: nothing is served).
            mode: ``Config.ASSET_DELIVERY`` (``presigned`` or ``proxy``).

        Raises:
            ValueError: ``mode`` is not a delivery mode.
        """
        self._s3 = s3_service
        self._mode = AssetDeliveryMode(mode.strip().lower())

    def deliver(
        self, handle: AssetHandle, *, error_type: type[CodedError] = CodedError
    ) -> DeliveryResult:
        """Deliver one file the caller already authorized.

        Args:
            handle: What to serve, built from stored metadata.
            error_type: The caller's Manager exception, so that its router's
                ``except`` clause catches every refusal.

        Raises:
            CodedError: as ``error_type``: ``handle.not_found_code`` (no storage
                or no object), ``ASSET_TOO_LARGE`` or ``S3_OPERATION_FAILED``.
        """
        if self._s3 is None:
            raise error_type("no file storage", code=handle.not_found_code)
        try:
            if self._mode is AssetDeliveryMode.PROXY:
                return self._stream(self._s3, handle, error_type)
            return self._redirect(self._s3, handle, error_type)
        except CodedError as e:
            if e.code is ErrorCode.S3_OBJECT_NOT_FOUND:
                raise error_type("object is gone", code=handle.not_found_code) from e
            if isinstance(e, error_type):
                raise
            raise error_type(
                f"asset delivery failed ({e.code.value})", code=e.code
            ) from e

    @staticmethod
    def _redirect(
        s3: "S3Service", handle: AssetHandle, error_type: type[CodedError]
    ) -> DeliveryResult:
        if handle.check_exists and not s3.exists(handle.s3_path):
            raise error_type("object is gone", code=handle.not_found_code)
        url = s3.generate_presigned_url(handle.s3_path, handle.presigned_expiration)
        return DeliveryResult.redirect(url)

    @staticmethod
    def _stream(
        s3: "S3Service", handle: AssetHandle, error_type: type[CodedError]
    ) -> DeliveryResult:
        asset = s3.open_stream(handle.s3_path)
        limit = max_bytes(handle.size_limit, upload_max_bytes=config.MAX_FILE_SIZE)
        if asset.content_length > limit:
            asset.close()
            raise error_type(
                f"object of {asset.content_length} bytes exceeds {limit}",
                code=ErrorCode.ASSET_TOO_LARGE,
            )
        return DeliveryResult.stream(asset, handle.content_type, handle.download_name)
