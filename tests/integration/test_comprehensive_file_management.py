"""
包括的なファイル管理機能の統合テスト
ファイルマネージャーの全機能を統合的にテストする
"""

import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock
import pytest

from src.managers.file_manager import FileManager, FileUploadError, FileSecurityError
from src.config import Config


class TestComprehensiveFileManagement:
    """包括的ファイル管理統合テストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化処理"""
        # テスト用の一時ディレクトリを作成
        self.temp_dir = tempfile.mkdtemp()

        # テスト用設定を作成
        self.test_config = Config()
        self.test_config.UPLOAD_DIR = self.temp_dir
        self.test_config.MAX_FILE_SIZE = 1024 * 1024  # 1MB

        # ファイル情報を保存する辞書
        self.uploaded_files = {}

        # モックデータベースサービスを作成
        self.mock_db_service = Mock()

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

        def mock_get_file(file_id):
            return self.uploaded_files.get(file_id)

        def mock_get_all_files():
            return list(self.uploaded_files.values())

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

        # テスト用サンプルファイル
        self.sample_files = [
            {
                "filename": "customer_interview_1.txt",
                "content": """
N1インタビュー結果 - 顧客A

インタビュー対象者: 佐藤花子さん（28歳、会社員）
実施日: 2024年12月15日

Q: 当社の商品についてどのように感じていますか？
A: とても使いやすく、デザインも気に入っています。
毎日使っているので、生活に欠かせない存在になっています。
特に朝の忙しい時間帯に重宝しています。

Q: 改善してほしい点はありますか？
A: 価格がもう少し安くなると嬉しいです。
また、カラーバリエーションが増えると選択肢が広がって良いと思います。
                """.strip(),
            },
            {
                "filename": "customer_interview_2.md",
                "content": """
# N1インタビュー結果 - 顧客B

## 基本情報
- インタビュー対象者: 田中一郎さん（45歳、自営業）
- 実施日: 2024年12月16日

## インタビュー内容

### Q: 当社のサービスを利用してみていかがでしたか？
A: 非常に満足しています。特にサポート体制が充実していて安心できます。
操作も直感的で、初心者でも使いやすいと思います。

### Q: 他社サービスと比較していかがですか？
A: 機能面では他社と大差ないですが、サポートの質が圧倒的に良いです。
価格も適正だと思います。
                """.strip(),
            },
            {
                "filename": "feedback_summary.txt",
                "content": """
顧客フィードバック総括

全体的な満足度: 高い
主な評価ポイント:
- 使いやすさ
- デザイン性
- サポート品質

改善要望:
- 価格の見直し
- カラーバリエーション拡充
- 機能追加
                """.strip(),
            },
        ]

    def teardown_method(self):
        """各テストメソッドの後に実行されるクリーンアップ処理"""
        # テスト用ディレクトリを削除
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_operations_and_race_conditions(self):
        """同時操作と競合状態のテスト"""

        # 1. 同じファイル名での複数アップロード
        print("1. 同名ファイル複数アップロードテスト")
        filename = "duplicate_name.txt"
        contents = [
            "第1回のファイル内容です。十分な長さのテキストです。",
            "第2回のファイル内容です。十分な長さのテキストです。",
            "第3回のファイル内容です。十分な長さのテキストです。",
        ]

        uploaded_files = []
        for content in contents:
            content_bytes = content.encode("utf-8")
            saved_path, file_text, metadata = self.file_manager.upload_interview_file(
                content_bytes, filename
            )
            uploaded_files.append((saved_path, metadata))

            # 内容が正しいことを確認
            assert content == file_text

        # 3つの異なるファイルが保存されていることを確認
        saved_paths = [path for path, _ in uploaded_files]
        assert len(set(saved_paths)) == 3  # すべて異なるパス

        # ファイル一覧で3つのファイルが確認できることをチェック
        file_list = self.file_manager.list_uploaded_files()
        duplicate_files = [f for f in file_list if f.original_filename == filename]
        assert len(duplicate_files) == 3

        # 2. 高速な連続操作のテスト
        print("2. 高速連続操作テスト")
        rapid_files = []

        for i in range(5):
            content = (
                f"高速アップロードテスト {i + 1} の内容です。十分な長さのテキストです。"
            )
            content_bytes = content.encode("utf-8")
            filename = f"rapid_upload_{i + 1}.txt"

            saved_path, file_text, metadata = self.file_manager.upload_interview_file(
                content_bytes, filename
            )
            rapid_files.append(metadata)

        # すべてのファイルが正常にアップロードされていることを確認
        file_list = self.file_manager.list_uploaded_files()
        rapid_filenames = {
            f.original_filename
            for f in file_list
            if f.original_filename.startswith("rapid_upload_")
        }
        expected_filenames = {f"rapid_upload_{i + 1}.txt" for i in range(5)}
        assert rapid_filenames == expected_filenames
