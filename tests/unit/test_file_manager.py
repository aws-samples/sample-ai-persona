"""Unit tests for FileManager."""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
import pytest

from src.managers.file_manager import (
    FileManager,
    FileUploadError,
    FileSecurityError,
)
from src.config import config
from src.models.asset_delivery import DeliveryKind, SizeLimit
from src.models.errors import ErrorCode
from tests.conftest import MEMBER_A

#: The uploader (uploads take the user and need resource:create)
PRINCIPAL = MEMBER_A.principal()


class TestFileManager:
    """Test class for FileManager."""

    def setup_method(self):
        """Runs before each test method."""
        # Create a temporary directory for tests
        self.temp_dir = tempfile.mkdtemp()

        # Mock Config to use the test directory
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 1024 * 1024  # 1MB
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda filename: any(
                filename.lower().endswith(ext)
                for ext in mock_config.ALLOWED_FILE_EXTENSIONS
            )

            # Create the FileManager
            self.file_manager = FileManager(file_store=Mock(), attachments=Mock())

    def teardown_method(self):
        """Runs after each test method."""
        # Remove the test directory
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_validate_persona_source_file_txt_ok(self):
        """Persona generation source: text with a supported extension passes."""
        self.file_manager.validate_persona_source_file(
            "interview.txt", "十分な長さのインタビュー内容です。".encode("utf-8")
        )

    def test_validate_persona_source_file_pdf_not_rejected(self):
        """Persona generation source: a PDF (binary) passes as long as its format is valid and it is non-empty (regression guard)."""
        # Unlike validate_file_format, undecodable binary must not be rejected.
        self.file_manager.validate_persona_source_file(
            "report.pdf", b"%PDF-1.4 binary payload not decodable as text"
        )

    def test_validate_persona_source_file_invalid_extension(self):
        """Persona generation source: an unsupported extension (.exe) is rejected by the allowlist."""
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_persona_source_file(
                "malware.exe", b"anything at all here"
            )
        assert exc_info.value.code is ErrorCode.FILE_FORMAT_NOT_ALLOWED

    def test_validate_persona_source_file_empty(self):
        """Persona generation source: an empty file is rejected."""
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_persona_source_file("empty.txt", b"")
        assert exc_info.value.code is ErrorCode.FILE_EMPTY

    def test_validate_persona_source_file_too_large(self):
        """Persona generation source: exceeding the raw byte limit is rejected."""
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.PERSONA_SOURCE_MAX_BYTES = 100
            with pytest.raises(FileUploadError) as exc_info:
                self.file_manager.validate_persona_source_file("big.txt", b"a" * 101)
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE

    def test_security_check_success(self):
        """Security check succeeds."""
        filename = "normal_file.txt"
        content = "正常なファイル内容です。".encode("utf-8")

        # Verify no exception is raised
        self.file_manager._security_check(filename, content)

    def test_security_check_path_traversal(self):
        """Path traversal attack test."""
        filename = "../../../etc/passwd"
        content = "悪意のあるファイル".encode("utf-8")

        with pytest.raises(FileSecurityError) as exc_info:
            self.file_manager._security_check(filename, content)

        assert exc_info.value.code is ErrorCode.FILE_NAME_INVALID

    def test_security_check_hidden_file(self):
        """Hidden file test."""
        filename = ".hidden_file.txt"
        content = "隠しファイル".encode("utf-8")

        with pytest.raises(FileSecurityError) as exc_info:
            self.file_manager._security_check(filename, content)

        assert exc_info.value.code is ErrorCode.FILE_HIDDEN_NOT_ALLOWED

    def test_security_check_binary_file(self):
        """Binary file test."""
        filename = "binary_file.txt"
        content = b"Normal text\x00binary data"

        with pytest.raises(FileSecurityError) as exc_info:
            self.file_manager._security_check(filename, content)

        assert exc_info.value.code is ErrorCode.FILE_BINARY_NOT_ALLOWED

    def test_upload_discussion_document_png(self):
        """Discussion document (PNG) upload test."""
        png_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        filename = "test_image.png"

        metadata = self.file_manager.upload_discussion_document(
            PRINCIPAL, png_content, filename
        )

        assert metadata.file_id is not None
        assert metadata.original_filename == filename
        assert metadata.mime_type == "image/png"
        assert "discussion_documents" in metadata.file_path
        assert metadata.file_size == len(png_content)

    def test_upload_discussion_document_pdf(self):
        """Discussion document (PDF) upload test."""
        pdf_content = b"%PDF-1.4\n" + b"\x00" * 100
        filename = "test_document.pdf"

        metadata = self.file_manager.upload_discussion_document(
            PRINCIPAL, pdf_content, filename
        )

        assert metadata.file_id is not None
        assert metadata.original_filename == filename
        assert metadata.mime_type == "application/pdf"
        assert "discussion_documents" in metadata.file_path

    def test_upload_discussion_document_invalid_format(self):
        """Discussion document rejects an invalid format."""
        content = b"test content"
        filename = "test.txt"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_discussion_document(PRINCIPAL, content, filename)

        assert exc_info.value.code is ErrorCode.FILE_FORMAT_NOT_ALLOWED
        assert ".pdf" in str(exc_info.value.context["allowed_formats"])

    def test_upload_discussion_document_oversized(self):
        """Discussion document size limit test."""
        large_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)  # 11MB
        filename = "large.png"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_discussion_document(
                PRINCIPAL, large_content, filename
            )

        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 5.0


class TestFileManagerSurveyImage:
    """Tests for upload_survey_image."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 10 * 1024 * 1024
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda f: False
            self.file_manager = FileManager(file_store=Mock(), attachments=Mock())
            self.file_manager.survey_images_dir = Path(self.temp_dir) / "survey_images"
            self.file_manager.survey_images_dir.mkdir(exist_ok=True)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upload_survey_image_success(self):
        # Minimal PNG (1x1 pixel)
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        result = self.file_manager.upload_survey_image(
            PRINCIPAL, png_header, "test.png"
        )
        assert result is not None
        assert result.mime_type == "image/png"

    def test_upload_survey_image_invalid_format(self):
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_survey_image(PRINCIPAL, b"content", "test.txt")
        assert exc_info.value.code is ErrorCode.FILE_FORMAT_NOT_ALLOWED

    def test_upload_survey_image_empty(self):
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_survey_image(PRINCIPAL, b"", "test.png")
        assert exc_info.value.code is ErrorCode.FILE_EMPTY

    def test_upload_survey_image_too_large(self):
        large_content = b"\x89PNG" + b"\x00" * (6 * 1024 * 1024)
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_survey_image(PRINCIPAL, large_content, "test.png")
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 5.0


class TestFileManagerKnowledgeFile:
    """Files imported into memory are not stored.

    upload_knowledge_file is gone; the preview only converts via
    convert_file_to_markdown, after the safety and size checks.
    """

    def setup_method(self):
        self.file_store = Mock()
        self.s3 = Mock()
        self.file_manager = FileManager(
            s3_service=self.s3,
            file_store=self.file_store,
            attachments=Mock(),
        )

    def test_conversion_saves_nothing(self):
        content = ("テスト知識ファイルの内容です。" * 10).encode("utf-8")
        markdown_text = self.file_manager.convert_file_to_markdown(
            content, "knowledge.txt"
        )
        assert "テスト知識ファイル" in markdown_text
        self.s3.upload_file.assert_not_called()
        self.file_store.put.assert_not_called()

    def test_invalid_format(self):
        with pytest.raises(FileUploadError):
            self.file_manager.convert_file_to_markdown(b"content", "test.exe")

    def test_too_large(self):
        large_content = b"x" * (11 * 1024 * 1024)
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.convert_file_to_markdown(large_content, "test.txt")
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 10.0

    def test_empty(self):
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.convert_file_to_markdown(b"", "empty.txt")
        assert exc_info.value.code is ErrorCode.FILE_EMPTY

    def test_path_traversal_name(self):
        with pytest.raises(FileSecurityError):
            self.file_manager.convert_file_to_markdown(b"x", "../a.txt")


class TestFileManagerConvertToMarkdown:
    """Tests for convert_file_to_markdown."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 10 * 1024 * 1024
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda f: False
            self.file_manager = FileManager(file_store=Mock(), attachments=Mock())

    def test_convert_txt_to_markdown(self):
        content = "テストテキスト内容です。".encode("utf-8")
        result = self.file_manager.convert_file_to_markdown(content, "test.txt")
        assert "テストテキスト" in result

    def test_convert_invalid_format(self):
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.convert_file_to_markdown(b"content", "test.exe")
        assert exc_info.value.code is ErrorCode.FILE_FORMAT_NOT_ALLOWED

    def test_convert_pdf_with_markitdown(self):
        with patch("markitdown.MarkItDown") as mock_md_cls:
            mock_instance = MagicMock()
            mock_result = MagicMock()
            mock_result.text_content = "PDF変換結果テキスト" * 5
            mock_instance.convert_stream.return_value = mock_result
            mock_md_cls.return_value = mock_instance

            result = self.file_manager.convert_file_to_markdown(b"%PDF-1.4", "test.pdf")
            assert "PDF変換結果" in result


class TestFileManagerUtilities:
    """Tests for cleanup_orphaned_files, get_file_statistics, bulk_delete_files, export_file_metadata and validate_system_health."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 10 * 1024 * 1024
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda f: False
            self.file_manager = FileManager(file_store=Mock(), attachments=Mock())

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_validate_discussion_document_image_within_5mb(self):
        """An image up to 5MB passes validation."""
        content = b"\x00" * (4 * 1024 * 1024)  # 4MB
        result = self.file_manager._validate_discussion_document("photo.png", content)
        assert result is True

    def test_validate_discussion_document_image_over_5mb(self):
        """An image over 5MB fails validation (matches the Bedrock limit)."""
        content = b"\x00" * (5 * 1024 * 1024 + 1)  # over 5MB
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager._validate_discussion_document("photo.png", content)
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 5.0

    def test_validate_discussion_document_pdf_within_10mb(self):
        """A PDF is allowed up to 10MB, looser than images."""
        content = b"\x00" * (6 * 1024 * 1024)  # 6MB
        result = self.file_manager._validate_discussion_document("doc.pdf", content)
        assert result is True

    def test_validate_discussion_document_pdf_over_10mb(self):
        """A PDF over 10MB fails validation."""
        content = b"\x00" * (10 * 1024 * 1024 + 1)  # over 10MB
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager._validate_discussion_document("doc.pdf", content)
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 10.0


# =============================================================================
# Owner-prefixed keys, 24-hour expiry, and delivery decisions
# =============================================================================

_U5_REGION = "us-east-1"
_U5_BUCKET = "u5-file-manager-bucket"
_U5_TABLE = "UploadedFilesFM"
_U5_NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def u5_files(monkeypatch):
    """FileManager on moto S3 / DynamoDB with the real FileAttachments."""
    moto = pytest.importorskip("moto")
    import boto3

    from src.managers.components.file_attachments import FileAttachments
    from src.services.s3_service import S3Service
    from src.services.stores.uploaded_file_store import UploadedFileStore

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        ddb = boto3.client("dynamodb", region_name=_U5_REGION)
        ddb.create_table(
            TableName=_U5_TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name=_U5_REGION).create_bucket(Bucket=_U5_BUCKET)
        s3 = S3Service(_U5_BUCKET, _U5_REGION)
        store = UploadedFileStore(region=_U5_REGION, table_name=_U5_TABLE, client=ddb)
        clock = {"now": _U5_NOW.timestamp()}
        attachments = FileAttachments(store, s3, clock=lambda: clock["now"])
        manager = FileManager(
            s3_service=s3,
            file_store=store,
            attachments=attachments,
            clock=lambda: _U5_NOW,
        )
        yield manager, store, s3, attachments, clock


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class TestFileManagerU5Uploads:
    def test_upload_key_has_the_owner_and_a_pending_tag(self, u5_files):
        manager, store, s3, _, _ = u5_files

        metadata = manager.upload_discussion_document(PRINCIPAL, _PNG, "資料.png")

        stored = store.get(metadata.file_id)
        assert stored.owner_sub == PRINCIPAL.sub
        assert stored.s3_key == (
            f"uploads/{PRINCIPAL.sub}/discussion_document/{metadata.file_id}_資料.png"
        )
        assert stored.expires_at == int(_U5_NOW.timestamp()) + 24 * 3600
        assert metadata.file_path == f"s3://{_U5_BUCKET}/{stored.s3_key}"
        assert s3.get_object_tags(stored.s3_key) == {"pending": "true"}

    def test_interview_attachment_is_an_owned_pending_upload(self, u5_files):
        """Interview attachments are stored by the same upload as discussions."""
        manager, store, s3, _, _ = u5_files

        metadata = manager.upload_interview_attachment(PRINCIPAL, _PNG, "画像.png")

        stored = store.get(metadata.file_id)
        assert stored.owner_sub == PRINCIPAL.sub
        assert stored.s3_key.startswith(f"uploads/{PRINCIPAL.sub}/")
        assert stored.expires_at == int(_U5_NOW.timestamp()) + 24 * 3600
        assert stored.mime_type == "image/png"
        assert s3.get_object_tags(stored.s3_key) == {"pending": "true"}

    @pytest.mark.parametrize(
        "content, name, code",
        [
            (b"data", "file.zip", ErrorCode.FILE_MIME_UNSUPPORTED),
            (b"\x89PNG" * (6 * 1024 * 1024), "big.png", ErrorCode.FILE_TOO_LARGE),
            (b"%PDF" * (11 * 1024 * 1024), "big.pdf", ErrorCode.FILE_TOO_LARGE),
        ],
        ids=["unsupported", "image-too-large", "document-too-large"],
    )
    def test_interview_attachment_kind_and_size(self, u5_files, content, name, code):
        manager, store, _, _, _ = u5_files
        with pytest.raises(FileUploadError) as exc_info:
            manager.upload_interview_attachment(PRINCIPAL, content, name)
        assert exc_info.value.code is code

    def test_interview_attachment_needs_resource_create(self, u5_files):
        from src.models.errors import AccessDeniedError
        from tests.conftest import NO_ROLE

        manager, _, _, _, _ = u5_files
        with pytest.raises(AccessDeniedError):
            manager.upload_interview_attachment(NO_ROLE.principal(), _PNG, "a.png")

    def test_two_users_with_the_same_name_keep_both_files(self, u5_files):
        from tests.conftest import MEMBER_B

        manager, store, _, _, _ = u5_files
        first = manager.upload_survey_image(PRINCIPAL, _PNG, "same.png")
        second = manager.upload_survey_image(MEMBER_B.principal(), _PNG, "same.png")

        assert first.file_path != second.file_path
        assert store.get(first.file_id).owner_sub == PRINCIPAL.sub
        assert store.get(second.file_id).owner_sub == MEMBER_B.sub

    def test_uploads_need_resource_create(self, u5_files):
        from src.models.errors import AccessDeniedError
        from tests.conftest import NO_ROLE

        manager, _, _, _, _ = u5_files
        with pytest.raises(AccessDeniedError):
            manager.upload_survey_image(NO_ROLE.principal(), _PNG, "a.png")

    def test_a_failed_metadata_write_removes_the_object(self, u5_files, monkeypatch):
        manager, store, s3, _, _ = u5_files
        monkeypatch.setattr(store, "put", Mock(side_effect=RuntimeError("down")))
        deleted = Mock(wraps=s3.delete_file)
        monkeypatch.setattr(s3, "delete_file", deleted)

        with pytest.raises(FileUploadError) as exc_info:
            manager.upload_survey_image(PRINCIPAL, _PNG, "a.png")
        assert exc_info.value.code is ErrorCode.FILE_OPERATION_FAILED
        deleted.assert_called_once()


class TestFileManagerU5Preview:
    """The preview path check (check_preview_path) and delivery after the check."""

    def test_owner_passes_the_check_and_nothing_is_signed(self, u5_files, monkeypatch):
        manager, _, s3, _, _ = u5_files
        metadata = manager.upload_survey_image(PRINCIPAL, _PNG, "a.png")
        sign = Mock(wraps=s3.generate_presigned_url)
        monkeypatch.setattr(s3, "generate_presigned_url", sign)

        assert manager.check_preview_path(PRINCIPAL, metadata.file_path) is None
        sign.assert_not_called()

    def test_deliver_preview_hands_the_upload_handle(self, u5_files):
        manager, _, _, attachments, _ = u5_files
        metadata = manager.upload_survey_image(PRINCIPAL, _PNG, "商品.png")
        delivery = Mock()
        delivering = FileManager(
            s3_service=manager.s3_service,
            file_store=manager._file_store,
            attachments=attachments,
            asset_delivery=delivery,
        )

        result = delivering.deliver_preview(PRINCIPAL, metadata.file_path)

        assert result is delivery.deliver.return_value
        handle = delivery.deliver.call_args.args[0]
        assert handle.s3_path == metadata.file_path
        assert handle.content_type == "image/png"
        assert handle.download_name == "商品.png"
        assert handle.size_limit is SizeLimit.UPLOAD
        assert handle.not_found_code is ErrorCode.FILE_NOT_FOUND
        assert handle.presigned_expiration == 3600

    def test_deliver_preview_streams_in_proxy_mode(self, u5_files, monkeypatch):
        manager, _, _, _, _ = u5_files
        monkeypatch.setattr(config, "ASSET_DELIVERY", "proxy")
        metadata = manager.upload_survey_image(PRINCIPAL, _PNG, "a.png")

        result = manager.deliver_preview(PRINCIPAL, metadata.file_path)

        assert result.kind is DeliveryKind.STREAM
        assert b"".join(result.asset.chunks) == _PNG

    def test_deliver_preview_of_someone_elses_image_never_delivers(self, u5_files):
        from tests.conftest import MEMBER_B

        manager, _, _, attachments, _ = u5_files
        metadata = manager.upload_survey_image(PRINCIPAL, _PNG, "a.png")
        delivery = Mock()
        delivering = FileManager(
            s3_service=manager.s3_service,
            file_store=manager._file_store,
            attachments=attachments,
            asset_delivery=delivery,
        )
        with pytest.raises(FileUploadError) as exc_info:
            delivering.deliver_preview(MEMBER_B.principal(), metadata.file_path)
        assert exc_info.value.code is ErrorCode.FILE_NOT_FOUND
        delivery.deliver.assert_not_called()

    def test_signing_failure_is_a_file_operation_failure(self, u5_files, monkeypatch):
        manager, _, s3, _, _ = u5_files
        metadata = manager.upload_survey_image(PRINCIPAL, _PNG, "a.png")
        monkeypatch.setattr(
            s3, "generate_presigned_url", Mock(side_effect=RuntimeError("boom"))
        )
        with pytest.raises(FileUploadError) as exc_info:
            manager.deliver_preview(PRINCIPAL, metadata.file_path)
        assert exc_info.value.code is ErrorCode.FILE_OPERATION_FAILED

    @pytest.mark.parametrize(
        "path",
        [
            "s3://another-bucket/uploads/x/survey_image/00000000-0000-4000-8000-000000000000_a.png",
            f"s3://{_U5_BUCKET}/survey_images/a.png",
            f"s3://{_U5_BUCKET}/uploads/x/survey_image/not-a-uuid_a.png",
            f"s3://{_U5_BUCKET}/persona_avatars/p/x.webp",
            "/etc/passwd",
            "",
        ],
    )
    def test_any_other_path_is_not_found(self, u5_files, path):
        manager, _, _, _, _ = u5_files
        with pytest.raises(FileUploadError) as exc_info:
            manager.check_preview_path(PRINCIPAL, path)
        assert exc_info.value.code is ErrorCode.FILE_NOT_FOUND

    def test_someone_elses_image_is_not_found(self, u5_files):
        from tests.conftest import MEMBER_B

        manager, _, _, _, _ = u5_files
        metadata = manager.upload_survey_image(PRINCIPAL, _PNG, "a.png")
        with pytest.raises(FileUploadError) as exc_info:
            manager.check_preview_path(MEMBER_B.principal(), metadata.file_path)
        assert exc_info.value.code is ErrorCode.FILE_NOT_FOUND

    def test_a_path_that_does_not_match_the_metadata_is_not_found(self, u5_files):
        manager, _, _, _, _ = u5_files
        metadata = manager.upload_survey_image(PRINCIPAL, _PNG, "a.png")
        forged = metadata.file_path.replace("_a.png", "_other.png")
        with pytest.raises(FileUploadError) as exc_info:
            manager.check_preview_path(PRINCIPAL, forged)
        assert exc_info.value.code is ErrorCode.FILE_NOT_FOUND

    def test_expired_image_is_not_found(self, u5_files):
        manager, _, _, _, clock = u5_files
        metadata = manager.upload_survey_image(PRINCIPAL, _PNG, "a.png")
        clock["now"] = _U5_NOW.timestamp() + 24 * 3600
        with pytest.raises(FileUploadError) as exc_info:
            manager.check_preview_path(PRINCIPAL, metadata.file_path)
        assert exc_info.value.code is ErrorCode.FILE_NOT_FOUND
