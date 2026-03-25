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

    def test_complete_file_management_workflow(self):
        """完全なファイル管理ワークフローのテスト"""
        uploaded_files = []

        # 1. 複数ファイルのアップロード
        print("1. ファイルアップロードテスト")
        for sample in self.sample_files:
            content_bytes = sample["content"].encode("utf-8")
            saved_path, file_text, metadata = self.file_manager.upload_interview_file(
                content_bytes, sample["filename"]
            )
            uploaded_files.append(metadata)

            # アップロード結果の検証
            assert Path(saved_path).exists()
            assert sample["content"] == file_text
            assert metadata.original_filename == sample["filename"]
            assert metadata.file_size == len(content_bytes)

        # 2. ファイル一覧取得と検証
        print("2. ファイル一覧取得テスト")
        file_list = self.file_manager.list_uploaded_files()
        assert len(file_list) == len(self.sample_files)

        # ファイル名の確認
        uploaded_names = {f.original_filename for f in file_list}
        expected_names = {s["filename"] for s in self.sample_files}
        assert uploaded_names == expected_names

        # 3. ファイル検索機能のテスト
        print("3. ファイル検索テスト")

        # "サポート"で検索
        search_results = self.file_manager.search_files_by_content("サポート")
        # サポートという単語が含まれるファイルを確認
        support_files = [
            f
            for f in search_results
            if "サポート" in self.file_manager.get_uploaded_file_content(f.file_path)
        ]
        assert len(support_files) >= 1
        # customer_interview_2.mdが含まれていることを確認
        support_filenames = [f.original_filename for f in support_files]
        assert "customer_interview_2.md" in support_filenames

        # "価格"で検索
        search_results = self.file_manager.search_files_by_content("価格")
        # 価格という単語が含まれるファイルの数を確認（複数のファイルに含まれている）
        assert len(search_results) >= 2

        # 存在しないキーワードで検索
        search_results = self.file_manager.search_files_by_content(
            "存在しないキーワード"
        )
        assert len(search_results) == 0

        # 4. ファイル統計情報の取得
        print("4. ファイル統計情報テスト")
        stats = self.file_manager.get_file_statistics()

        assert stats["total_files"] == len(self.sample_files)
        assert stats["average_size_bytes"] > 0
        assert stats["largest_file_size"] > 0
        assert stats["smallest_file_size"] > 0
        assert ".txt" in stats["file_types"]
        assert ".md" in stats["file_types"]
        assert len(stats["upload_dates"]) == len(self.sample_files)

        # 5. ストレージ使用量の確認
        print("5. ストレージ使用量テスト")
        usage = self.file_manager.get_storage_usage()

        assert usage["total_files"] >= len(self.sample_files)
        assert usage["total_size_bytes"] > 0
        assert usage["total_size_mb"] >= 0
        assert "upload_dir" in usage
        assert "max_file_size_mb" in usage

        # 6. ファイル整合性の検証
        print("6. ファイル整合性テスト")
        for metadata in uploaded_files:
            integrity_result = self.file_manager.verify_file_integrity(metadata.file_id)
            assert integrity_result is True

        # 7. メタデータエクスポート
        print("7. メタデータエクスポートテスト")
        exported_metadata = self.file_manager.export_file_metadata()

        assert len(exported_metadata) == len(self.sample_files)
        for metadata_dict in exported_metadata:
            assert "file_id" in metadata_dict
            assert "original_filename" in metadata_dict
            assert "file_size" in metadata_dict
            assert "uploaded_at" in metadata_dict

        # 8. 一括削除機能のテスト
        print("8. 一括削除テスト")

        # 最初の2つのファイルを削除
        delete_ids = [uploaded_files[0].file_id, uploaded_files[1].file_id]
        delete_results = self.file_manager.bulk_delete_files(delete_ids)

        assert len(delete_results) == 2
        assert all(delete_results.values())  # すべて削除成功

        # 削除後のファイル一覧確認
        remaining_files = self.file_manager.list_uploaded_files()
        assert len(remaining_files) == 1
        assert remaining_files[0].file_id == uploaded_files[2].file_id

        # 9. システム健全性チェック
        print("9. システム健全性チェックテスト")
        health_report = self.file_manager.validate_system_health()

        assert health_report["upload_dir_exists"] is True
        assert health_report["upload_dir_writable"] is True
        assert health_report["database_accessible"] is True
        assert health_report["total_files"] == 1
        assert len(health_report["missing_files"]) == 0
        assert len(health_report["corrupted_files"]) == 0

    def test_error_handling_and_recovery(self):
        """エラーハンドリングと回復機能のテスト"""

        # 1. 無効なファイル形式のテスト
        print("1. 無効ファイル形式テスト")
        invalid_files = [
            ("document.pdf", "PDFファイル"),
            ("image.jpg", "画像ファイル"),
            ("data.xlsx", "Excelファイル"),
        ]

        for filename, content in invalid_files:
            content_bytes = content.encode("utf-8")
            with pytest.raises(FileUploadError):
                self.file_manager.upload_interview_file(content_bytes, filename)

        # 2. セキュリティ脅威のテスト
        print("2. セキュリティ脅威テスト")
        dangerous_files = [
            ("../../../etc/passwd", "悪意のあるファイル"),
            (".hidden_file.txt", "隠しファイル"),
            ("normal_file.exe", "実行ファイル"),
        ]

        for filename, content in dangerous_files:
            content_bytes = content.encode("utf-8")
            with pytest.raises((FileUploadError, FileSecurityError)):
                self.file_manager.upload_interview_file(content_bytes, filename)

        # 3. ファイルサイズ制限のテスト
        print("3. ファイルサイズ制限テスト")
        large_content = "a" * (self.file_manager.max_file_size + 1)
        content_bytes = large_content.encode("utf-8")

        with pytest.raises(FileUploadError) as exc_info:
            self.file_manager.upload_interview_file(content_bytes, "large_file.txt")

        assert "ファイルサイズが制限を超えています" in str(exc_info.value)

        # 4. 存在しないファイルの操作テスト
        print("4. 存在しないファイル操作テスト")

        # 存在しないファイルIDでの削除
        non_existent_id = "non-existent-file-id"
        delete_result = self.file_manager.delete_uploaded_file(non_existent_id)
        assert delete_result is False

        # 存在しないファイルIDでの整合性チェック
        integrity_result = self.file_manager.verify_file_integrity(non_existent_id)
        assert integrity_result is False

        # 存在しないファイルIDでのメタデータ取得
        metadata = self.file_manager.get_file_metadata(non_existent_id)
        assert metadata is None

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

    def test_system_resilience_and_recovery(self):
        """システムの耐障害性と回復機能のテスト"""

        # 1. 正常なファイルをアップロード
        print("1. 正常ファイルアップロード")
        content = (
            "システム耐障害性テスト用のファイル内容です。十分な長さのテキストです。"
        )
        content_bytes = content.encode("utf-8")
        filename = "resilience_test.txt"

        saved_path, file_text, metadata = self.file_manager.upload_interview_file(
            content_bytes, filename
        )

        # 2. ファイルシステムの問題をシミュレート
        print("2. ファイルシステム問題シミュレーション")

        # ファイルを手動で削除（データベースには残る）
        Path(saved_path).unlink()

        # 整合性チェックで問題が検出されることを確認
        integrity_result = self.file_manager.verify_file_integrity(metadata.file_id)
        assert integrity_result is False

        # 3. システム健全性チェックで問題が検出されることを確認
        print("3. システム健全性チェック")
        health_report = self.file_manager.validate_system_health()

        assert metadata.file_id in health_report["missing_files"]

        # 4. 孤立ファイルの作成と検出
        print("4. 孤立ファイル検出テスト")

        # データベースに登録されていないファイルを作成
        orphaned_file_path = self.file_manager.upload_dir / "orphaned_file.txt"
        with open(orphaned_file_path, "w", encoding="utf-8") as f:
            f.write("孤立したファイルです")

        # 孤立ファイルが検出されることを確認
        health_report = self.file_manager.validate_system_health()
        assert health_report["orphaned_files"] > 0

        # 孤立ファイルのクリーンアップ
        deleted_count = self.file_manager.cleanup_orphaned_files()
        assert deleted_count > 0

        # クリーンアップ後は孤立ファイルがないことを確認
        health_report_after = self.file_manager.validate_system_health()
        assert health_report_after["orphaned_files"] == 0

    def test_edge_cases_and_boundary_conditions(self):
        """エッジケースと境界条件のテスト"""

        # 1. 最小サイズのファイル
        print("1. 最小サイズファイルテスト")
        min_content = "最小サイズテストです。十分な長さのテキストです。"  # 10文字以上
        content_bytes = min_content.encode("utf-8")

        saved_path, file_text, metadata = self.file_manager.upload_interview_file(
            content_bytes, "min_size.txt"
        )
        assert file_text == min_content

        # 2. 最大サイズ近くのファイル
        print("2. 最大サイズ近くファイルテスト")
        max_content = "a" * (
            self.file_manager.max_file_size - 100
        )  # 制限より少し小さい
        content_bytes = max_content.encode("utf-8")

        saved_path, file_text, metadata = self.file_manager.upload_interview_file(
            content_bytes, "max_size.txt"
        )
        assert len(file_text) == len(max_content)

        # 3. 特殊文字を含むファイル名
        print("3. 特殊文字ファイル名テスト")
        special_filenames = [
            "ファイル名_日本語.txt",
            "file-with-dashes.txt",
            "file_with_underscores.txt",
            "file.with.dots.txt",
        ]

        for filename in special_filenames:
            content = f"{filename}の内容です。十分な長さのテキストです。"
            content_bytes = content.encode("utf-8")

            saved_path, file_text, metadata = self.file_manager.upload_interview_file(
                content_bytes, filename
            )
            assert metadata.original_filename == filename

        # 4. 異なるエンコーディングのテスト
        print("4. 異なるエンコーディングテスト")
        japanese_content = (
            "これは日本語のテキストファイルです。様々な文字が含まれています。"
        )

        encodings = ["utf-8", "shift_jis", "euc-jp"]
        for encoding in encodings:
            content_bytes = japanese_content.encode(encoding)
            filename = f"encoding_{encoding}.txt"

            saved_path, file_text, metadata = self.file_manager.upload_interview_file(
                content_bytes, filename
            )
            assert file_text == japanese_content

        # 5. 空の検索結果のテスト
        print("5. 空検索結果テスト")

        # 存在しないキーワードで検索
        search_results = self.file_manager.search_files_by_content(
            "絶対に存在しないキーワード12345"
        )
        assert len(search_results) == 0

        # 6. 統計情報の境界条件テスト
        print("6. 統計情報境界条件テスト")

        # すべてのファイルを削除
        all_files = self.file_manager.list_uploaded_files()
        for file_metadata in all_files:
            self.file_manager.delete_uploaded_file(file_metadata.file_id)

        # 空の状態での統計情報
        stats = self.file_manager.get_file_statistics()
        assert stats["total_files"] == 0
        assert stats["average_size_bytes"] == 0
        assert stats["largest_file_size"] == 0
        assert stats["smallest_file_size"] == 0
        assert stats["file_types"] == {}
        assert stats["upload_dates"] == []
