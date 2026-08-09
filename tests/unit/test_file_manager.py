"""
ファイルマネージャーの単体テスト
"""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
import pytest

from src.managers.file_manager import (
    FileManager,
    FileUploadError,
    FileSecurityError,
)
from src.models.errors import ErrorCode


class TestFileManager:
    """ファイルマネージャーのテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化処理"""
        # テスト用の一時ディレクトリを作成
        self.temp_dir = tempfile.mkdtemp()

        # ファイル情報を保存する辞書（テスト用のインメモリストレージ）
        self.uploaded_files = {}

        # モックデータベースサービスを作成
        self.mock_db_service = Mock()

        # save_uploaded_file_infoのモック実装
        def mock_save_file(
            file_id,
            filename,
            file_path,
            file_size=None,
            file_hash=None,
            mime_type=None,
            uploaded_at=None,
            original_filename=None,
            file_type="persona_interview",
        ):
            self.uploaded_files[file_id] = {
                "id": file_id,
                "filename": filename,
                "original_filename": original_filename or filename,
                "file_path": file_path,
                "file_size": file_size,
                "file_hash": file_hash,
                "mime_type": mime_type,
                "uploaded_at": uploaded_at or datetime.now(),
            }
            return file_id

        # get_uploaded_file_infoのモック実装
        def mock_get_file(file_id):
            return self.uploaded_files.get(file_id)

        # get_all_uploaded_filesのモック実装
        def mock_get_all_files():
            return list(self.uploaded_files.values())

        # delete_uploaded_file_infoのモック実装
        def mock_delete_file(file_id):
            if file_id in self.uploaded_files:
                del self.uploaded_files[file_id]
                return True
            return False

        self.mock_db_service.save_uploaded_file_info.side_effect = mock_save_file
        self.mock_db_service.get_uploaded_file_info.side_effect = mock_get_file
        self.mock_db_service.get_all_uploaded_files.side_effect = mock_get_all_files
        self.mock_db_service.delete_uploaded_file_info.side_effect = mock_delete_file

        # Configをモック化してテスト用ディレクトリを使用
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 1024 * 1024  # 1MB
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda filename: any(
                filename.lower().endswith(ext)
                for ext in mock_config.ALLOWED_FILE_EXTENSIONS
            )

            # FileManagerを作成
            self.file_manager = FileManager(self.mock_db_service)

    def teardown_method(self):
        """各テストメソッドの後に実行されるクリーンアップ処理"""
        # テスト用ディレクトリを削除
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_validate_persona_source_file_txt_ok(self):
        """ペルソナ生成ソース: 対応拡張子のテキストは通過する"""
        self.file_manager.validate_persona_source_file(
            "interview.txt", "十分な長さのインタビュー内容です。".encode("utf-8")
        )

    def test_validate_persona_source_file_pdf_not_rejected(self):
        """ペルソナ生成ソース: PDF(バイナリ)は形式・空でない限り通過する（デグレ防止）"""
        # validate_file_format と違い、デコード不可のバイナリでも弾かれてはならない。
        self.file_manager.validate_persona_source_file(
            "report.pdf", b"%PDF-1.4 binary payload not decodable as text"
        )

    def test_validate_persona_source_file_invalid_extension(self):
        """ペルソナ生成ソース: 非対応拡張子(.exe)は許可リストで弾く"""
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_persona_source_file(
                "malware.exe", b"anything at all here"
            )
        assert exc_info.value.code is ErrorCode.FILE_FORMAT_NOT_ALLOWED

    def test_validate_persona_source_file_empty(self):
        """ペルソナ生成ソース: 空ファイルは弾く"""
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_persona_source_file("empty.txt", b"")
        assert exc_info.value.code is ErrorCode.FILE_EMPTY

    def test_validate_persona_source_file_too_large(self):
        """ペルソナ生成ソース: 生バイト上限超過は弾く"""
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.PERSONA_SOURCE_MAX_BYTES = 100
            with pytest.raises(FileUploadError) as exc_info:
                self.file_manager.validate_persona_source_file("big.txt", b"a" * 101)
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE

    def test_security_check_success(self):
        """セキュリティチェック成功テスト"""
        filename = "normal_file.txt"
        content = "正常なファイル内容です。".encode("utf-8")

        # 例外が発生しないことを確認
        self.file_manager._security_check(filename, content)

    def test_security_check_path_traversal(self):
        """パストラバーサル攻撃のテスト"""
        filename = "../../../etc/passwd"
        content = "悪意のあるファイル".encode("utf-8")

        with pytest.raises(FileSecurityError) as exc_info:
            self.file_manager._security_check(filename, content)

        assert exc_info.value.code is ErrorCode.FILE_NAME_INVALID

    def test_security_check_hidden_file(self):
        """隠しファイルのテスト"""
        filename = ".hidden_file.txt"
        content = "隠しファイル".encode("utf-8")

        with pytest.raises(FileSecurityError) as exc_info:
            self.file_manager._security_check(filename, content)

        assert exc_info.value.code is ErrorCode.FILE_HIDDEN_NOT_ALLOWED

    def test_security_check_binary_file(self):
        """バイナリファイルのテスト"""
        filename = "binary_file.txt"
        content = b"Normal text\x00binary data"

        with pytest.raises(FileSecurityError) as exc_info:
            self.file_manager._security_check(filename, content)

        assert exc_info.value.code is ErrorCode.FILE_BINARY_NOT_ALLOWED

    def test_upload_discussion_document_png(self):
        """議論用ドキュメント（PNG）アップロードテスト (Task 2)"""
        png_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        filename = "test_image.png"

        metadata = self.file_manager.upload_discussion_document(png_content, filename)

        assert metadata.file_id is not None
        assert metadata.original_filename == filename
        assert metadata.mime_type == "image/png"
        assert "discussion_documents" in metadata.file_path
        assert metadata.file_size == len(png_content)

    def test_upload_discussion_document_pdf(self):
        """議論用ドキュメント（PDF）アップロードテスト (Task 2)"""
        pdf_content = b"%PDF-1.4\n" + b"\x00" * 100
        filename = "test_document.pdf"

        metadata = self.file_manager.upload_discussion_document(pdf_content, filename)

        assert metadata.file_id is not None
        assert metadata.original_filename == filename
        assert metadata.mime_type == "application/pdf"
        assert "discussion_documents" in metadata.file_path

    def test_upload_discussion_document_invalid_format(self):
        """議論用ドキュメント無効形式拒否テスト (Task 2)"""
        content = b"test content"
        filename = "test.txt"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_discussion_document(content, filename)

        assert exc_info.value.code is ErrorCode.FILE_FORMAT_NOT_ALLOWED
        assert ".pdf" in str(exc_info.value.context["allowed_formats"])

    def test_upload_discussion_document_oversized(self):
        """議論用ドキュメントサイズ制限テスト (Task 2)"""
        large_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)  # 11MB
        filename = "large.png"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_discussion_document(large_content, filename)

        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 5.0


class TestFileManagerSurveyImage:
    """upload_survey_image のテスト"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_db_service = Mock()
        self.mock_db_service.save_uploaded_file_info.return_value = "f1"
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 10 * 1024 * 1024
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda f: False
            self.file_manager = FileManager(self.mock_db_service)
            self.file_manager.survey_images_dir = Path(self.temp_dir) / "survey_images"
            self.file_manager.survey_images_dir.mkdir(exist_ok=True)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upload_survey_image_success(self):
        # 最小限のPNG (1x1 pixel)
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        result = self.file_manager.upload_survey_image(png_header, "test.png")
        assert result is not None
        assert result.mime_type == "image/png"

    def test_upload_survey_image_invalid_format(self):
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_survey_image(b"content", "test.txt")
        assert exc_info.value.code is ErrorCode.FILE_FORMAT_NOT_ALLOWED

    def test_upload_survey_image_empty(self):
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_survey_image(b"", "test.png")
        assert exc_info.value.code is ErrorCode.FILE_EMPTY

    def test_upload_survey_image_too_large(self):
        large_content = b"\x89PNG" + b"\x00" * (6 * 1024 * 1024)
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_survey_image(large_content, "test.png")
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 5.0


class TestFileManagerKnowledgeFile:
    """upload_knowledge_file のテスト"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_db_service = Mock()
        self.mock_db_service.save_uploaded_file_info.return_value = "f1"
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 10 * 1024 * 1024
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda f: False
            self.file_manager = FileManager(self.mock_db_service)
            self.file_manager.knowledge_files_dir = (
                Path(self.temp_dir) / "knowledge_files"
            )
            self.file_manager.knowledge_files_dir.mkdir(exist_ok=True)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upload_knowledge_file_txt(self):
        content = ("テスト知識ファイルの内容です。" * 10).encode("utf-8")
        metadata, markdown_text = self.file_manager.upload_knowledge_file(
            content, "knowledge.txt"
        )
        assert metadata is not None
        assert "テスト知識ファイル" in markdown_text

    def test_upload_knowledge_file_invalid_format(self):
        with pytest.raises(FileUploadError):
            self.file_manager.upload_knowledge_file(b"content", "test.exe")

    def test_upload_knowledge_file_too_large(self):
        large_content = b"x" * (11 * 1024 * 1024)
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_knowledge_file(large_content, "test.txt")
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 10.0


class TestFileManagerConvertToMarkdown:
    """convert_file_to_markdown のテスト"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_db_service = Mock()
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 10 * 1024 * 1024
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda f: False
            self.file_manager = FileManager(self.mock_db_service)

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
    """cleanup_orphaned_files, get_file_statistics, bulk_delete_files, export_file_metadata, validate_system_health のテスト"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_db_service = Mock()
        self.mock_db_service.get_all_uploaded_files.return_value = []
        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 10 * 1024 * 1024
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda f: False
            self.file_manager = FileManager(self.mock_db_service)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_validate_discussion_document_image_within_5mb(self):
        """画像が5MB以内なら検証を通過する"""
        content = b"\x00" * (4 * 1024 * 1024)  # 4MB
        result = self.file_manager._validate_discussion_document("photo.png", content)
        assert result is True

    def test_validate_discussion_document_image_over_5mb(self):
        """画像が5MBを超えると検証エラー（Bedrock上限に整合）"""
        content = b"\x00" * (5 * 1024 * 1024 + 1)  # 5MB超
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager._validate_discussion_document("photo.png", content)
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 5.0

    def test_validate_discussion_document_pdf_within_10mb(self):
        """PDFは画像より緩く10MBまで許容する"""
        content = b"\x00" * (6 * 1024 * 1024)  # 6MB
        result = self.file_manager._validate_discussion_document("doc.pdf", content)
        assert result is True

    def test_validate_discussion_document_pdf_over_10mb(self):
        """PDFが10MBを超えると検証エラー"""
        content = b"\x00" * (10 * 1024 * 1024 + 1)  # 10MB超
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager._validate_discussion_document("doc.pdf", content)
        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc_info.value.context["max_size_mb"] == 10.0
