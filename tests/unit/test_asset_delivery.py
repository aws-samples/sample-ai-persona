"""U4 の配信の部品 AssetDelivery（BR2.1〜BR2.7）。S3Service はモック。"""

from unittest.mock import Mock

import pytest

from src.config import config
from src.managers.components.asset_delivery import AssetDelivery
from src.models.asset_delivery import (
    SURVEY_CSV_MAX_BYTES,
    AssetHandle,
    AssetStream,
    DeliveryKind,
    SizeLimit,
)
from src.models.errors import CodedError, ErrorCode
from src.services.s3_service import S3ServiceError
from tests.error_helpers import raises_code

pytestmark = pytest.mark.unit

PATH = "s3://bucket/uploads/sub-a/discussion/f_a.pdf"


class _MyError(CodedError):
    """A Manager's own exception type."""


def _handle(**overrides) -> AssetHandle:
    values = dict(
        s3_path=PATH,
        content_type="application/pdf",
        download_name="資料.pdf",
        size_limit=SizeLimit.UPLOAD,
        not_found_code=ErrorCode.FILE_NOT_FOUND,
        presigned_expiration=1800,
    )
    values.update(overrides)
    return AssetHandle(**values)


def _stream(length: int) -> AssetStream:
    return AssetStream(chunks=iter([b"%PDF"]), content_length=length, close=Mock())


@pytest.fixture
def s3() -> Mock:
    mock = Mock()
    mock.generate_presigned_url.return_value = "https://signed.example/a"
    mock.exists.return_value = True
    mock.open_stream.return_value = _stream(4)
    return mock


class TestPresigned:
    def test_signs_with_the_given_expiration_and_never_opens(self, s3):
        result = AssetDelivery(s3, "presigned").deliver(_handle())

        assert result.kind is DeliveryKind.REDIRECT
        assert result.presigned_url == "https://signed.example/a"
        s3.generate_presigned_url.assert_called_once_with(PATH, 1800)
        s3.open_stream.assert_not_called()
        s3.exists.assert_not_called()

    def test_check_exists_stops_with_the_callers_code_before_signing(self, s3):
        s3.exists.return_value = False

        with raises_code(_MyError, ErrorCode.FILE_NOT_FOUND):
            AssetDelivery(s3, "presigned").deliver(
                _handle(check_exists=True), error_type=_MyError
            )
        s3.generate_presigned_url.assert_not_called()

    def test_check_exists_signs_when_the_object_is_there(self, s3):
        result = AssetDelivery(s3, "presigned").deliver(_handle(check_exists=True))

        assert result.presigned_url == "https://signed.example/a"
        s3.exists.assert_called_once_with(PATH)

    def test_signing_failure_is_an_s3_operation_failure(self, s3):
        s3.generate_presigned_url.side_effect = S3ServiceError(
            "boom", code=ErrorCode.S3_OPERATION_FAILED
        )
        with raises_code(_MyError, ErrorCode.S3_OPERATION_FAILED):
            AssetDelivery(s3, "presigned").deliver(_handle(), error_type=_MyError)


class TestProxy:
    def test_opens_and_returns_a_stream_with_type_disposition_and_size(self, s3):
        body = _stream(4)
        s3.open_stream.return_value = body

        result = AssetDelivery(s3, "proxy").deliver(_handle())

        assert result.kind is DeliveryKind.STREAM
        assert result.asset is body
        assert result.content_type == "application/pdf"
        assert result.disposition == "inline"
        assert result.download_name == "資料.pdf"
        assert result.content_length == 4
        s3.open_stream.assert_called_once_with(PATH)
        s3.generate_presigned_url.assert_not_called()
        body.close.assert_not_called()

    def test_types_outside_the_inline_list_are_attachments(self, s3):
        result = AssetDelivery(s3, "proxy").deliver(
            _handle(content_type="text/html", download_name="x.html")
        )
        assert result.disposition == "attachment"

    @pytest.mark.parametrize(
        ("limit", "max_size"),
        [
            (SizeLimit.UPLOAD, config.MAX_FILE_SIZE),
            (SizeLimit.SURVEY_CSV, SURVEY_CSV_MAX_BYTES),
        ],
    )
    def test_exactly_the_limit_is_streamed(self, s3, limit, max_size):
        body = _stream(max_size)
        s3.open_stream.return_value = body

        result = AssetDelivery(s3, "proxy").deliver(_handle(size_limit=limit))

        assert result.content_length == max_size
        body.close.assert_not_called()

    @pytest.mark.parametrize(
        ("limit", "max_size"),
        [
            (SizeLimit.UPLOAD, config.MAX_FILE_SIZE),
            (SizeLimit.SURVEY_CSV, SURVEY_CSV_MAX_BYTES),
        ],
    )
    def test_one_byte_over_the_limit_closes_and_refuses(self, s3, limit, max_size):
        body = _stream(max_size + 1)
        s3.open_stream.return_value = body

        with raises_code(_MyError, ErrorCode.ASSET_TOO_LARGE):
            AssetDelivery(s3, "proxy").deliver(
                _handle(size_limit=limit), error_type=_MyError
            )
        body.close.assert_called_once()

    def test_a_missing_object_gets_the_callers_code(self, s3):
        s3.open_stream.side_effect = S3ServiceError(
            "gone", code=ErrorCode.S3_OBJECT_NOT_FOUND
        )
        with raises_code(_MyError, ErrorCode.PERSONA_AVATAR_NOT_FOUND):
            AssetDelivery(s3, "proxy").deliver(
                _handle(not_found_code=ErrorCode.PERSONA_AVATAR_NOT_FOUND),
                error_type=_MyError,
            )

    def test_other_s3_failures_stay_s3_operation_failures(self, s3):
        s3.open_stream.side_effect = S3ServiceError(
            "denied", code=ErrorCode.S3_OPERATION_FAILED
        )
        with raises_code(CodedError, ErrorCode.S3_OPERATION_FAILED):
            AssetDelivery(s3, "proxy").deliver(_handle())

    def test_mode_is_case_insensitive_and_validated(self, s3):
        assert AssetDelivery(s3, "PROXY").deliver(_handle()).kind is DeliveryKind.STREAM
        with pytest.raises(ValueError):
            AssetDelivery(s3, "direct")


class TestWithoutStorage:
    def test_no_s3_service_means_not_found(self):
        with raises_code(_MyError, ErrorCode.FILE_NOT_FOUND):
            AssetDelivery(None, "proxy").deliver(_handle(), error_type=_MyError)
