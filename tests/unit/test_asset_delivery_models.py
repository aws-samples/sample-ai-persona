"""U4 の配信の値（``src/models/asset_delivery.py``、BR2.3、BR2.7）。"""

import pytest

from src.config import config
from src.models.asset_delivery import (
    INLINE_CONTENT_TYPES,
    SURVEY_CSV_MAX_BYTES,
    AssetDeliveryMode,
    AssetHandle,
    AssetStream,
    DeliveryKind,
    DeliveryResult,
    SizeLimit,
    disposition_for,
    max_bytes,
)
from src.models.errors import ErrorCode

pytestmark = pytest.mark.unit


def _stream(length: int = 3) -> AssetStream:
    return AssetStream(chunks=iter([b"abc"]), content_length=length, close=lambda: None)


class TestDeliveryResult:
    def test_redirect_holds_only_the_url(self):
        result = DeliveryResult.redirect("https://signed.example/x")
        assert result.kind is DeliveryKind.REDIRECT
        assert result.presigned_url == "https://signed.example/x"
        assert result.asset is None
        assert result.content_type is None
        assert result.disposition is None

    def test_stream_holds_no_url(self):
        body = _stream(3)
        result = DeliveryResult.stream(body, "image/png", "a.png")
        assert result.kind is DeliveryKind.STREAM
        assert result.presigned_url is None
        assert result.asset is body
        assert result.content_type == "image/png"
        assert result.download_name == "a.png"
        assert result.disposition == "inline"
        assert result.content_length == 3

    def test_redirect_has_no_content_length(self):
        assert DeliveryResult.redirect("https://x").content_length is None


class TestDisposition:
    @pytest.mark.parametrize(
        "content_type",
        ["image/png", "image/jpeg", "image/webp", "application/pdf", "IMAGE/PNG"],
    )
    def test_images_and_pdf_open_inline(self, content_type):
        assert disposition_for(content_type) == "inline"

    @pytest.mark.parametrize(
        "content_type",
        ["text/html", "image/svg+xml", "text/csv", "application/octet-stream", ""],
    )
    def test_everything_else_is_an_attachment(self, content_type):
        assert disposition_for(content_type) == "attachment"

    def test_parameters_are_ignored(self):
        assert disposition_for("application/pdf; charset=binary") == "inline"
        assert disposition_for("text/html; charset=utf-8") == "attachment"

    def test_inline_types_are_exactly_four(self):
        assert INLINE_CONTENT_TYPES == frozenset(
            {"image/png", "image/jpeg", "image/webp", "application/pdf"}
        )


class TestSizeLimits:
    def test_upload_is_the_upload_limit(self):
        assert (
            max_bytes(SizeLimit.UPLOAD, upload_max_bytes=config.MAX_FILE_SIZE)
            == config.MAX_FILE_SIZE
        )

    def test_survey_csv_is_200_mib(self):
        assert SURVEY_CSV_MAX_BYTES == 200 * 1024 * 1024
        assert (
            max_bytes(SizeLimit.SURVEY_CSV, upload_max_bytes=1) == SURVEY_CSV_MAX_BYTES
        )


class TestValues:
    def test_modes(self):
        assert {m.value for m in AssetDeliveryMode} == {"presigned", "proxy"}

    def test_handle_defaults_to_no_existence_check(self):
        handle = AssetHandle(
            s3_path="s3://b/k.webp",
            content_type="image/webp",
            download_name="p1.webp",
            size_limit=SizeLimit.UPLOAD,
            not_found_code=ErrorCode.PERSONA_AVATAR_NOT_FOUND,
            presigned_expiration=3600,
        )
        assert handle.check_exists is False

    def test_handle_rejects_a_non_positive_expiration(self):
        with pytest.raises(ValueError):
            AssetHandle(
                s3_path="s3://b/k",
                content_type="text/csv",
                download_name="a.csv",
                size_limit=SizeLimit.SURVEY_CSV,
                not_found_code=ErrorCode.SURVEY_RESULT_NOT_READY,
                presigned_expiration=0,
            )
