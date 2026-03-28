"""
ファイルマネージャーの単体テスト
"""

import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
import pytest

from src.managers.file_manager import (
    FileManager,
    FileUploadError,
    FileSecurityError,
    FileMetadata,
)


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

    def test_validate_file_format_success(self):
        """正常なファイル形式の検証テスト"""
        filename = "test.txt"
        content = (
            "これはテストファイルです。N1インタビューの内容を含んでいます。".encode(
                "utf-8"
            )
        )

        result = self.file_manager.validate_file_format(filename, content)
        assert result is True

    def test_validate_file_format_invalid_extension(self):
        """無効なファイル拡張子のテスト"""
        filename = "test.pdf"
        content = "テスト内容".encode("utf-8")

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_file_format(filename, content)

        assert "許可されていないファイル形式" in str(exc_info.value)

    def test_validate_file_format_file_too_large(self):
        """ファイルサイズ制限超過のテスト"""
        filename = "test.txt"
        # 制限サイズを超える内容を作成
        content = "a" * (self.file_manager.max_file_size + 1)
        content = content.encode("utf-8")

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_file_format(filename, content)

        assert "ファイルサイズが制限を超えています" in str(exc_info.value)

    def test_validate_file_format_empty_file(self):
        """空ファイルのテスト"""
        filename = "test.txt"
        content = b""

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_file_format(filename, content)

        assert "ファイルが空です" in str(exc_info.value)

    def test_validate_file_format_content_too_short(self):
        """内容が短すぎるファイルのテスト"""
        filename = "test.txt"
        content = "短い".encode("utf-8")

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_file_format(filename, content)

        assert "ファイル内容が短すぎます" in str(exc_info.value)

    def test_validate_file_format_invalid_encoding(self):
        """無効なエンコーディングのテスト"""
        filename = "test.txt"
        # バイナリデータ（テキストではない）
        content = b"\x80\x81\x82\x83\x84\x85\x86\x87\x88\x89"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.validate_file_format(filename, content)

        assert "テキストファイルとして読み取れません" in str(exc_info.value)

    def test_validate_file_format_shift_jis_encoding(self):
        """Shift_JISエンコーディングのテスト"""
        filename = "test.txt"
        content = "これはShift_JISでエンコードされたテストファイルです。".encode(
            "shift_jis"
        )

        result = self.file_manager.validate_file_format(filename, content)
        assert result is True

    def test_upload_interview_file_success(self):
        """正常なファイルアップロードのテスト"""
        filename = "interview.txt"
        content = (
            "これはN1インタビューの内容です。詳細な顧客の声が含まれています。".encode(
                "utf-8"
            )
        )

        saved_path, file_text, metadata = self.file_manager.upload_interview_file(
            content, filename
        )

        # ファイルが保存されていることを確認
        assert Path(saved_path).exists()
        assert "これはN1インタビューの内容です" in file_text

        # メタデータの確認
        assert isinstance(metadata, FileMetadata)
        assert metadata.original_filename == filename
        assert metadata.file_size == len(content)
        assert metadata.file_hash is not None

        # 保存されたファイル名にUUIDが含まれていることを確認
        saved_filename = Path(saved_path).name
        assert filename in saved_filename
        assert len(saved_filename) > len(filename)  # UUIDが追加されている

    def test_upload_interview_file_duplicate_prevention(self):
        """重複ファイルアップロード防止のテスト"""
        content = "これは重複チェック用のN1インタビューテスト内容です。詳細な顧客の声が含まれています。".encode(
            "utf-8"
        )
        filename = "duplicate_test.txt"

        # 初回アップロード
        saved_path1, file_text1, metadata1 = self.file_manager.upload_interview_file(
            content, filename, allow_duplicates=False
        )

        # 同じファイルを再度アップロード（重複チェック有効）
        saved_path2, file_text2, metadata2 = self.file_manager.upload_interview_file(
            content, filename, allow_duplicates=False
        )

        # 同じファイルが返されることを確認
        assert metadata1.file_id == metadata2.file_id
        assert saved_path1 == saved_path2
        assert file_text1 == file_text2

        # データベース内のファイル数が1つであることを確認
        all_files = self.file_manager.list_uploaded_files()
        duplicate_files = [f for f in all_files if f.original_filename == filename]
        assert len(duplicate_files) == 1

    def test_upload_interview_file_allow_duplicates(self):
        """重複ファイル許可のテスト"""
        content = "これは重複許可用のN1インタビューテスト内容です。詳細な顧客の声が含まれています。".encode(
            "utf-8"
        )
        filename = "allow_duplicate_test.txt"

        # 初回アップロード
        saved_path1, file_text1, metadata1 = self.file_manager.upload_interview_file(
            content, filename, allow_duplicates=True
        )

        # 同じファイルを再度アップロード（重複許可）
        saved_path2, file_text2, metadata2 = self.file_manager.upload_interview_file(
            content, filename, allow_duplicates=True
        )

        # 異なるファイルIDが生成されることを確認
        assert metadata1.file_id != metadata2.file_id
        assert saved_path1 != saved_path2
        assert file_text1 == file_text2  # 内容は同じ

        # データベース内に2つのファイルが存在することを確認
        all_files = self.file_manager.list_uploaded_files()
        duplicate_files = [f for f in all_files if f.original_filename == filename]
        assert len(duplicate_files) == 2

    def test_upload_interview_file_invalid_format(self):
        """無効な形式のファイルアップロードテスト"""
        filename = "interview.pdf"
        content = "テスト内容".encode("utf-8")

        with pytest.raises(FileUploadError):
            self.file_manager.upload_interview_file(content, filename)

    def test_save_uploaded_file_success(self):
        """ファイル保存の成功テスト"""
        filename = "test_file.txt"
        content = "テストファイルの内容".encode("utf-8")

        saved_path = self.file_manager.save_uploaded_file(content, filename)

        # ファイルが保存されていることを確認
        assert Path(saved_path).exists()

        # 保存された内容を確認
        with open(saved_path, "rb") as f:
            saved_content = f.read()
        assert saved_content == content

    def test_get_uploaded_file_content_success(self):
        """保存されたファイル内容取得の成功テスト"""
        filename = "test_file.txt"
        original_content = "テストファイルの内容です。"
        content_bytes = original_content.encode("utf-8")

        # ファイルを保存
        saved_path = self.file_manager.save_uploaded_file(content_bytes, filename)

        # 内容を取得
        retrieved_content = self.file_manager.get_uploaded_file_content(saved_path)

        assert retrieved_content == original_content

    def test_get_uploaded_file_content_file_not_found(self):
        """存在しないファイルの内容取得テスト"""
        non_existent_path = "/path/to/non/existent/file.txt"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.get_uploaded_file_content(non_existent_path)

        assert "指定されたファイルが見つかりません" in str(exc_info.value)

    def test_delete_uploaded_file_success(self):
        """ファイル削除の成功テスト"""
        filename = "test_file.txt"
        content = "テストファイルの内容です。十分な長さのテキストです。".encode("utf-8")

        # ファイルをアップロード（メタデータ付きで）
        saved_path, file_text, metadata = self.file_manager.upload_interview_file(
            content, filename
        )
        assert Path(saved_path).exists()

        # ファイルを削除（IDで）
        result = self.file_manager.delete_uploaded_file(metadata.file_id)

        assert result is True
        assert not Path(saved_path).exists()

    def test_delete_uploaded_file_not_exists(self):
        """存在しないファイルの削除テスト"""
        non_existent_id = str(uuid.uuid4())

        result = self.file_manager.delete_uploaded_file(non_existent_id)
        assert result is False

    def test_list_uploaded_files_success(self):
        """アップロードファイル一覧取得の成功テスト"""
        # 複数のファイルを作成
        files_data = [
            ("file1.txt", "ファイル1の内容です。十分な長さのテキストです。"),
            ("file2.txt", "ファイル2の内容です。十分な長さのテキストです。"),
        ]

        for filename, content in files_data:
            content_bytes = content.encode("utf-8")
            self.file_manager.upload_interview_file(content_bytes, filename)

        # ファイル一覧を取得
        file_list = self.file_manager.list_uploaded_files()

        assert len(file_list) == 2

        # 各ファイル情報を確認
        for metadata in file_list:
            assert isinstance(metadata, FileMetadata)
            assert metadata.file_size > 0
            assert metadata.original_filename in ["file1.txt", "file2.txt"]

    def test_list_uploaded_files_empty_directory(self):
        """空のディレクトリでのファイル一覧取得テスト"""
        file_list = self.file_manager.list_uploaded_files()
        assert file_list == []

    def test_decode_file_content_utf8(self):
        """UTF-8エンコーディングのデコードテスト"""
        content = "UTF-8でエンコードされたテキスト".encode("utf-8")

        decoded = self.file_manager._decode_file_content(content)
        assert decoded == "UTF-8でエンコードされたテキスト"

    def test_decode_file_content_shift_jis(self):
        """Shift_JISエンコーディングのデコードテスト"""
        content = "Shift_JISでエンコードされたテキスト".encode("shift_jis")

        decoded = self.file_manager._decode_file_content(content)
        assert decoded == "Shift_JISでエンコードされたテキスト"

    def test_decode_file_content_invalid(self):
        """無効なエンコーディングのデコードテスト"""
        # バイナリデータ
        content = b"\x80\x81\x82\x83\x84\x85\x86\x87\x88\x89"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager._decode_file_content(content)

        assert "テキストファイルとして読み取れません" in str(exc_info.value)

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

        assert "ファイル名に不正な文字が含まれています" in str(exc_info.value)

    def test_security_check_hidden_file(self):
        """隠しファイルのテスト"""
        filename = ".hidden_file.txt"
        content = "隠しファイル".encode("utf-8")

        with pytest.raises(FileSecurityError) as exc_info:
            self.file_manager._security_check(filename, content)

        assert "隠しファイルはアップロードできません" in str(exc_info.value)

    def test_security_check_binary_file(self):
        """バイナリファイルのテスト"""
        filename = "binary_file.txt"
        content = b"Normal text\x00binary data"

        with pytest.raises(FileSecurityError) as exc_info:
            self.file_manager._security_check(filename, content)

        assert "バイナリファイルはアップロードできません" in str(exc_info.value)

    def test_verify_file_integrity_success(self):
        """ファイル整合性検証成功テスト"""
        filename = "integrity_test.txt"
        content = "整合性テスト用のファイル内容です。十分な長さのテキストです。".encode(
            "utf-8"
        )

        # ファイルをアップロード
        saved_path, file_text, metadata = self.file_manager.upload_interview_file(
            content, filename
        )

        # 整合性を検証
        result = self.file_manager.verify_file_integrity(metadata.file_id)
        assert result is True

    def test_verify_file_integrity_file_not_found(self):
        """ファイルが見つからない場合の整合性検証テスト"""
        non_existent_id = str(uuid.uuid4())

        result = self.file_manager.verify_file_integrity(non_existent_id)
        assert result is False

    def test_get_file_metadata_success(self):
        """ファイルメタデータ取得成功テスト"""
        filename = "metadata_test.txt"
        content = (
            "メタデータテスト用のファイル内容です。十分な長さのテキストです。".encode(
                "utf-8"
            )
        )

        # ファイルをアップロード
        saved_path, file_text, metadata = self.file_manager.upload_interview_file(
            content, filename
        )

        # メタデータを取得
        retrieved_metadata = self.file_manager.get_file_metadata(metadata.file_id)

        assert retrieved_metadata is not None
        assert retrieved_metadata.file_id == metadata.file_id
        assert retrieved_metadata.original_filename == filename
        assert retrieved_metadata.file_size == len(content)

    def test_get_storage_usage(self):
        """ストレージ使用量取得テスト"""
        filename = "storage_test.txt"
        content = (
            "ストレージテスト用のファイル内容です。十分な長さのテキストです。".encode(
                "utf-8"
            )
        )

        # ファイルをアップロード
        self.file_manager.upload_interview_file(content, filename)

        # ストレージ使用量を取得
        usage = self.file_manager.get_storage_usage()

        assert usage["total_files"] >= 1
        assert usage["total_size_bytes"] >= len(content)
        assert usage["total_size_mb"] >= 0
        assert "upload_dir" in usage
        assert "max_file_size_mb" in usage

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

        with pytest.raises(Exception) as exc_info:
            self.file_manager.upload_discussion_document(content, filename)

        assert "許可されていないファイル形式" in str(exc_info.value)

    def test_upload_discussion_document_oversized(self):
        """議論用ドキュメントサイズ制限テスト (Task 2)"""
        large_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)  # 11MB
        filename = "large.png"

        with pytest.raises(Exception) as exc_info:
            self.file_manager.upload_discussion_document(large_content, filename)

        assert "ファイルサイズが制限を超えています" in str(exc_info.value)


class TestMarketReportTextExtraction:
    """市場調査レポートテキスト抽出のテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化処理"""
        import sys

        self.temp_dir = tempfile.mkdtemp()
        self.mock_db_service = Mock()

        # markitdownモジュールをモック
        self.mock_markitdown_module = MagicMock()
        self.mock_md_instance = MagicMock()
        self.mock_markitdown_module.MarkItDown.return_value = self.mock_md_instance
        sys.modules["markitdown"] = self.mock_markitdown_module

        with patch("src.managers.file_manager.config") as mock_config:
            mock_config.upload_dir = Path(self.temp_dir)
            mock_config.MAX_FILE_SIZE = 10 * 1024 * 1024
            mock_config.ALLOWED_FILE_EXTENSIONS = (".txt", ".md")
            mock_config.is_allowed_file_extension = lambda filename: any(
                filename.lower().endswith(ext)
                for ext in mock_config.ALLOWED_FILE_EXTENSIONS
            )
            self.file_manager = FileManager(self.mock_db_service)

    def teardown_method(self):
        """各テストメソッドの後に実行されるクリーンアップ処理"""
        import sys
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)
        # モジュールモックをクリーンアップ
        if "markitdown" in sys.modules:
            del sys.modules["markitdown"]

    def test_extract_text_from_txt_file(self):
        """テキストファイルからのテキスト抽出テスト"""
        content = ("これは市場調査レポートのテストです。" * 10).encode("utf-8")
        filename = "report.txt"

        result = self.file_manager.extract_text_from_file(content, filename)

        assert "市場調査レポート" in result
        assert len(result) >= 100

    def test_extract_text_from_md_file(self):
        """マークダウンファイルからのテキスト抽出テスト"""
        content = ("# 市場調査レポート\n\nこれはテスト内容です。" * 10).encode("utf-8")
        filename = "report.md"

        result = self.file_manager.extract_text_from_file(content, filename)

        assert "市場調査レポート" in result

    def test_extract_text_invalid_format(self):
        """無効なファイル形式でエラーを返すテスト"""
        content = b"test content"
        filename = "report.xlsx"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.extract_text_from_file(content, filename)

        assert "許可されていないファイル形式" in str(exc_info.value)

    def test_extract_text_file_too_large(self):
        """ファイルサイズ制限超過テスト"""
        content = b"a" * (11 * 1024 * 1024)  # 11MB
        filename = "large_report.txt"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.extract_text_from_file(content, filename)

        assert "ファイルサイズが制限を超えています" in str(exc_info.value)

    def test_extract_text_empty_file(self):
        """空ファイルでエラーを返すテスト"""
        content = b""
        filename = "empty.txt"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.extract_text_from_file(content, filename)

        assert "ファイルが空です" in str(exc_info.value)

    def test_extract_text_content_too_short(self):
        """内容が短すぎるファイルでエラーを返すテスト"""
        content = "短い".encode("utf-8")
        filename = "short.txt"

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.extract_text_from_file(content, filename)

        assert "ファイル内容が短すぎます" in str(exc_info.value)

    def test_extract_text_shift_jis_encoding(self):
        """Shift_JISエンコーディングのテキスト抽出テスト"""
        content = (
            "これはShift_JISでエンコードされた市場調査レポートです。" * 10
        ).encode("shift_jis")
        filename = "report_sjis.txt"

        result = self.file_manager.extract_text_from_file(content, filename)

        assert "市場調査レポート" in result

    def test_extract_text_from_pdf(self):
        """PDFファイルからのテキスト抽出テスト（markitdownモック）"""
        mock_result = MagicMock()
        mock_result.text_content = (
            "これはPDFから抽出された市場調査レポートの内容です。" * 10
        )
        self.mock_md_instance.convert_stream.return_value = mock_result

        content = b"%PDF-1.4\nfake pdf content"
        filename = "report.pdf"

        result = self.file_manager.extract_text_from_file(content, filename)

        assert "市場調査レポート" in result
        self.mock_md_instance.convert_stream.assert_called_once()

    def test_extract_text_from_docx(self):
        """Wordファイルからのテキスト抽出テスト（markitdownモック）"""
        mock_result = MagicMock()
        mock_result.text_content = (
            "これはWordから抽出された市場調査レポートの内容です。" * 10
        )
        self.mock_md_instance.convert_stream.return_value = mock_result

        content = b"PK\x03\x04fake docx content"
        filename = "report.docx"

        result = self.file_manager.extract_text_from_file(content, filename)

        assert "市場調査レポート" in result


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
        with pytest.raises(FileUploadError, match="許可されていないファイル形式"):
            self.file_manager.upload_survey_image(b"content", "test.txt")

    def test_upload_survey_image_empty(self):
        with pytest.raises(FileUploadError, match="ファイルが空"):
            self.file_manager.upload_survey_image(b"", "test.png")

    def test_upload_survey_image_too_large(self):
        large_content = b"\x89PNG" + b"\x00" * (6 * 1024 * 1024)
        with pytest.raises(FileUploadError, match="ファイルサイズが制限"):
            self.file_manager.upload_survey_image(large_content, "test.png")


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
            self.file_manager.knowledge_files_dir = Path(self.temp_dir) / "knowledge_files"
            self.file_manager.knowledge_files_dir.mkdir(exist_ok=True)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upload_knowledge_file_txt(self):
        content = ("テスト知識ファイルの内容です。" * 10).encode("utf-8")
        metadata, markdown_text = self.file_manager.upload_knowledge_file(content, "knowledge.txt")
        assert metadata is not None
        assert "テスト知識ファイル" in markdown_text

    def test_upload_knowledge_file_invalid_format(self):
        with pytest.raises(FileUploadError):
            self.file_manager.upload_knowledge_file(b"content", "test.exe")

    def test_upload_knowledge_file_too_large(self):
        large_content = b"x" * (11 * 1024 * 1024)
        with pytest.raises(FileUploadError, match="ファイルサイズ"):
            self.file_manager.upload_knowledge_file(large_content, "test.txt")


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
        with pytest.raises(FileUploadError, match="許可されていないファイル形式"):
            self.file_manager.convert_file_to_markdown(b"content", "test.exe")

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

    def test_get_file_statistics_empty(self):
        result = self.file_manager.get_file_statistics()
        assert isinstance(result, dict)
        assert result["total_files"] == 0

    def test_bulk_delete_files(self):
        self.mock_db_service.get_uploaded_file_info.return_value = None
        result = self.file_manager.bulk_delete_files(["f1", "f2"])
        assert isinstance(result, dict)
        assert "f1" in result
        assert "f2" in result

    def test_export_file_metadata_empty(self):
        result = self.file_manager.export_file_metadata()
        assert isinstance(result, list)
        assert len(result) == 0

    def test_validate_system_health(self):
        result = self.file_manager.validate_system_health()
        assert isinstance(result, dict)
        assert "upload_dir_exists" in result

    def test_cleanup_orphaned_files(self):
        result = self.file_manager.cleanup_orphaned_files()
        assert isinstance(result, int)
        assert result >= 0
