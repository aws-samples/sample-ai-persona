"""
ファイル操作の統合テスト
実際のファイルシステムとの連携をテスト
"""

import os
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock
import pytest

from src.managers.file_manager import FileManager, FileUploadError
from src.config import Config


class TestFileOperationsIntegration:
    """ファイル操作統合テストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化処理"""
        # テスト用の一時ディレクトリを作成
        self.temp_dir = tempfile.mkdtemp()

        # テスト用設定を作成
        self.test_config = Config()
        self.test_config.UPLOAD_DIR = self.temp_dir
        self.test_config.MAX_FILE_SIZE = 1024 * 1024  # 1MB

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

        # ファイルマネージャーを初期化
        self.file_manager = FileManager(self.mock_db_service)
        self.file_manager.upload_dir = Path(self.temp_dir)
        self.file_manager.max_file_size = self.test_config.MAX_FILE_SIZE

    def teardown_method(self):
        """各テストメソッドの後に実行されるクリーンアップ処理"""
        # テスト用ディレクトリを削除
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_full_file_upload_workflow(self):
        """完全なファイルアップロードワークフローのテスト"""
        # テストファイルの内容を準備
        filename = "n1_interview.txt"
        interview_content = """
        N1インタビュー結果
        
        インタビュー対象者: 田中太郎さん（35歳、会社員）
        
        Q: 当社の商品についてどのように感じていますか？
        A: とても使いやすく、日常生活に欠かせない存在になっています。
        特に朝の忙しい時間帯に重宝しています。
        
        Q: 改善してほしい点はありますか？
        A: もう少し価格が安くなると嬉しいです。
        また、カラーバリエーションが増えると選択肢が広がって良いと思います。
        
        Q: 今後も継続して使用したいですか？
        A: はい、ぜひ継続して使用したいと思います。
        友人にも勧めたいと考えています。
        """
        content_bytes = interview_content.encode("utf-8")

        # 1. ファイルアップロード
        saved_path, file_text, metadata = self.file_manager.upload_interview_file(
            content_bytes, filename
        )

        # アップロード結果の検証
        assert Path(saved_path).exists()
        assert "N1インタビュー結果" in file_text
        assert "田中太郎さん" in file_text

        # 2. 保存されたファイル内容の取得
        retrieved_content = self.file_manager.get_uploaded_file_content(saved_path)
        assert retrieved_content == interview_content

        # 3. ファイル一覧の取得
        file_list = self.file_manager.list_uploaded_files()
        assert len(file_list) == 1
        assert file_list[0].saved_filename in saved_path
        assert file_list[0].file_size == len(content_bytes)

        # 4. ファイル削除
        delete_result = self.file_manager.delete_uploaded_file(metadata.file_id)
        assert delete_result is True
        assert not Path(saved_path).exists()

        # 5. 削除後のファイル一覧確認
        file_list_after_delete = self.file_manager.list_uploaded_files()
        assert len(file_list_after_delete) == 0

    def test_multiple_file_upload_and_management(self):
        """複数ファイルのアップロードと管理のテスト"""
        # 複数のテストファイルを準備
        files_data = [
            (
                "interview1.txt",
                "第1回N1インタビュー結果です。顧客Aの詳細な意見が含まれています。",
            ),
            (
                "interview2.txt",
                "第2回N1インタビュー結果です。顧客Bの貴重なフィードバックです。",
            ),
            (
                "interview3.md",
                "# 第3回インタビュー\n\n顧客Cからの重要な洞察が得られました。",
            ),
        ]

        saved_paths = []

        # 各ファイルをアップロード
        metadata_list = []
        for filename, content in files_data:
            content_bytes = content.encode("utf-8")
            saved_path, file_text, metadata = self.file_manager.upload_interview_file(
                content_bytes, filename
            )
            saved_paths.append(saved_path)
            metadata_list.append(metadata)

            # 各ファイルの内容確認
            assert content in file_text

        # ファイル一覧の確認
        file_list = self.file_manager.list_uploaded_files()
        assert len(file_list) == 3

        # 各ファイルが一覧に含まれていることを確認
        saved_filenames = [Path(path).name for path in saved_paths]
        list_filenames = [metadata.saved_filename for metadata in file_list]

        for saved_filename in saved_filenames:
            assert saved_filename in list_filenames

        # 一部のファイルを削除
        self.file_manager.delete_uploaded_file(metadata_list[0].file_id)
        self.file_manager.delete_uploaded_file(metadata_list[2].file_id)

        # 削除後の一覧確認
        file_list_after_delete = self.file_manager.list_uploaded_files()
        assert len(file_list_after_delete) == 1
        assert Path(saved_paths[1]).name in file_list_after_delete[0].saved_filename

    def test_file_encoding_handling(self):
        """異なるエンコーディングのファイル処理テスト"""
        filename = "encoded_interview.txt"
        content_text = (
            "これは日本語のN1インタビュー内容です。様々な文字が含まれています。"
        )

        # 異なるエンコーディングでテスト
        encodings = ["utf-8", "shift_jis", "euc-jp"]

        for encoding in encodings:
            content_bytes = content_text.encode(encoding)

            # ファイルアップロード
            saved_path, file_text, metadata = self.file_manager.upload_interview_file(
                content_bytes, f"{encoding}_{filename}"
            )

            # 内容が正しくデコードされていることを確認
            assert file_text == content_text

            # 保存されたファイルから内容を再取得
            retrieved_content = self.file_manager.get_uploaded_file_content(saved_path)
            assert retrieved_content == content_text

            # クリーンアップ
            self.file_manager.delete_uploaded_file(metadata.file_id)

    def test_file_size_limit_enforcement(self):
        """ファイルサイズ制限の実施テスト"""
        filename = "large_file.txt"

        # 制限サイズを超える内容を作成
        large_content = "a" * (self.file_manager.max_file_size + 1)
        content_bytes = large_content.encode("utf-8")

        # ファイルアップロードが失敗することを確認
        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_interview_file(content_bytes, filename)

        assert "ファイルサイズが制限を超えています" in str(exc_info.value)

        # ファイルが保存されていないことを確認
        file_list = self.file_manager.list_uploaded_files()
        assert len(file_list) == 0

    def test_invalid_file_extension_handling(self):
        """無効なファイル拡張子の処理テスト"""
        invalid_files = [
            ("document.pdf", "PDFファイルの内容"),
            ("image.jpg", "画像ファイルの内容"),
            ("data.xlsx", "Excelファイルの内容"),
            ("archive.zip", "ZIPファイルの内容"),
        ]

        for filename, content in invalid_files:
            content_bytes = content.encode("utf-8")

            # ファイルアップロードが失敗することを確認
            with pytest.raises(FileUploadError) as exc_info:
                self.file_manager.upload_interview_file(content_bytes, filename)

            assert "許可されていないファイル形式" in str(exc_info.value)

        # ファイルが保存されていないことを確認
        file_list = self.file_manager.list_uploaded_files()
        assert len(file_list) == 0

    def test_concurrent_file_operations(self):
        """同時ファイル操作のテスト"""
        # 同じファイル名で複数回アップロード
        filename = "duplicate_name.txt"
        contents = [
            "第1回のファイル内容です。",
            "第2回のファイル内容です。",
            "第3回のファイル内容です。",
        ]

        saved_paths = []

        for i, content in enumerate(contents):
            content_bytes = content.encode("utf-8")
            saved_path, file_text, metadata = self.file_manager.upload_interview_file(
                content_bytes, filename
            )
            saved_paths.append(saved_path)

            # 内容が正しいことを確認
            assert content in file_text

        # 3つの異なるファイルが保存されていることを確認
        assert len(set(saved_paths)) == 3  # すべて異なるパス

        file_list = self.file_manager.list_uploaded_files()
        assert len(file_list) == 3

        # 各ファイルの内容が正しいことを確認
        for i, saved_path in enumerate(saved_paths):
            retrieved_content = self.file_manager.get_uploaded_file_content(saved_path)
            assert retrieved_content == contents[i]

    def test_error_recovery_and_cleanup(self):
        """エラー回復とクリーンアップのテスト"""
        filename = "test_file.txt"
        content = "テストファイルの内容です。"
        content_bytes = content.encode("utf-8")

        # 正常なファイルアップロード
        saved_path, _, metadata = self.file_manager.upload_interview_file(
            content_bytes, filename
        )
        assert Path(saved_path).exists()

        # ファイルシステムエラーをシミュレート（権限変更）
        try:
            os.chmod(saved_path, 0o000)  # 読み取り権限を削除

            # ファイル読み取りエラーが発生することを確認
            with pytest.raises(FileUploadError):
                self.file_manager.get_uploaded_file_content(saved_path)

        finally:
            # 権限を復元してクリーンアップ
            os.chmod(saved_path, 0o644)
            self.file_manager.delete_uploaded_file(metadata.file_id)

        # クリーンアップ後の状態確認
        file_list = self.file_manager.list_uploaded_files()
        assert len(file_list) == 0
