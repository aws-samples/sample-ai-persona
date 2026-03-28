"""
FileManagerのS3統合テスト
"""

import pytest

# motoモジュールがない場合はテストをスキップ
moto = pytest.importorskip("moto", reason="moto is required for S3 tests")
mock_aws = moto.mock_aws

import boto3  # noqa: E402
from pathlib import Path  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest.mock import Mock  # noqa: E402

from src.managers.file_manager import FileManager, FileUploadError  # noqa: E402
from src.services.s3_service import S3Service  # noqa: E402


@pytest.fixture
def s3_bucket_name():
    """テスト用S3バケット名"""
    return "test-upload-bucket"


@pytest.fixture
def aws_region():
    """テスト用AWSリージョン"""
    return "us-east-1"


@pytest.fixture
def mock_database_service():
    """モックデータベースサービス"""
    uploaded_files = {}
    mock_db = Mock()

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
        uploaded_files[file_id] = {
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
        return uploaded_files.get(file_id)

    def mock_get_all_files():
        return list(uploaded_files.values())

    def mock_delete_file(file_id):
        if file_id in uploaded_files:
            del uploaded_files[file_id]
            return True
        return False

    mock_db.save_uploaded_file_info.side_effect = mock_save_file
    mock_db.get_uploaded_file_info.side_effect = mock_get_file
    mock_db.get_all_uploaded_files.side_effect = mock_get_all_files
    mock_db.delete_uploaded_file_info.side_effect = mock_delete_file

    return mock_db


@pytest.fixture
def s3_service(s3_bucket_name, aws_region, monkeypatch):
    """モックS3サービス"""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        # バケットを作成
        s3_client = boto3.client("s3", region_name=aws_region)
        s3_client.create_bucket(Bucket=s3_bucket_name)

        # S3Serviceインスタンスを作成
        service = S3Service(s3_bucket_name, aws_region)
        yield service


@pytest.fixture
def file_manager_with_s3(mock_database_service, s3_service):
    """S3を使用するFileManager"""
    return FileManager(db_service=mock_database_service, s3_service=s3_service)


@pytest.fixture
def file_manager_without_s3(mock_database_service):
    """S3を使用しないFileManager（ローカルストレージ）"""
    return FileManager(db_service=mock_database_service, s3_service=None)


def test_upload_file_to_s3(file_manager_with_s3):
    """S3へのファイルアップロードテスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # アップロード
    file_path, file_text, metadata = file_manager_with_s3.upload_interview_file(
        file_content, filename
    )

    # 検証
    assert file_path.startswith("s3://")
    assert "uploads/" in file_path
    assert file_text == "Test interview content for persona generation."
    assert metadata.original_filename == filename
    assert metadata.file_size == len(file_content)


def test_upload_file_to_local(file_manager_without_s3):
    """ローカルストレージへのファイルアップロードテスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # アップロード
    file_path, file_text, metadata = file_manager_without_s3.upload_interview_file(
        file_content, filename
    )

    # 検証
    assert not file_path.startswith("s3://")
    assert "uploads" in file_path
    assert file_text == "Test interview content for persona generation."
    assert metadata.original_filename == filename


def test_read_file_from_s3(file_manager_with_s3):
    """S3からのファイル読み込みテスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # アップロード
    file_path, _, _ = file_manager_with_s3.upload_interview_file(file_content, filename)

    # 読み込み
    content = file_manager_with_s3.get_uploaded_file_content(file_path)

    # 検証
    assert content == "Test interview content for persona generation."


def test_read_file_from_local(file_manager_without_s3):
    """ローカルストレージからのファイル読み込みテスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # アップロード
    file_path, _, _ = file_manager_without_s3.upload_interview_file(
        file_content, filename
    )

    # 読み込み
    content = file_manager_without_s3.get_uploaded_file_content(file_path)

    # 検証
    assert content == "Test interview content for persona generation."


def test_delete_file_from_s3(file_manager_with_s3):
    """S3からのファイル削除テスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # アップロード
    _, _, metadata = file_manager_with_s3.upload_interview_file(file_content, filename)

    # 削除
    result = file_manager_with_s3.delete_uploaded_file(metadata.file_id)

    # 検証
    assert result is True

    # メタデータが削除されていることを確認
    retrieved_metadata = file_manager_with_s3.get_file_metadata(metadata.file_id)
    assert retrieved_metadata is None


def test_delete_file_from_local(file_manager_without_s3):
    """ローカルストレージからのファイル削除テスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # アップロード
    _, _, metadata = file_manager_without_s3.upload_interview_file(
        file_content, filename
    )

    # 削除
    result = file_manager_without_s3.delete_uploaded_file(metadata.file_id)

    # 検証
    assert result is True

    # ファイルが削除されていることを確認
    file_path = Path(metadata.file_path)
    assert not file_path.exists()


def test_verify_file_integrity_s3(file_manager_with_s3):
    """S3ファイルの整合性検証テスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # アップロード
    _, _, metadata = file_manager_with_s3.upload_interview_file(file_content, filename)

    # 整合性検証
    is_valid = file_manager_with_s3.verify_file_integrity(metadata.file_id)

    # 検証
    assert is_valid is True


def test_verify_file_integrity_local(file_manager_without_s3):
    """ローカルファイルの整合性検証テスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # アップロード
    _, _, metadata = file_manager_without_s3.upload_interview_file(
        file_content, filename
    )

    # 整合性検証
    is_valid = file_manager_without_s3.verify_file_integrity(metadata.file_id)

    # 検証
    assert is_valid is True


def test_list_uploaded_files_s3(file_manager_with_s3):
    """S3アップロードファイル一覧取得テスト"""
    # 複数ファイルをアップロード
    for i in range(3):
        file_content = f"Test content {i}".encode()
        filename = f"test_file_{i}.txt"
        file_manager_with_s3.upload_interview_file(file_content, filename)

    # 一覧取得
    files = file_manager_with_s3.list_uploaded_files()

    # 検証
    assert len(files) >= 3
    for file_metadata in files:
        assert file_metadata.file_path.startswith("s3://")


def test_duplicate_file_detection_s3(file_manager_with_s3):
    """S3での重複ファイル検出テスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # 1回目のアップロード
    _, _, metadata1 = file_manager_with_s3.upload_interview_file(
        file_content, filename, allow_duplicates=False
    )

    # 2回目のアップロード（重複）
    _, _, metadata2 = file_manager_with_s3.upload_interview_file(
        file_content, filename, allow_duplicates=False
    )

    # 検証：同じファイルIDが返される
    assert metadata1.file_id == metadata2.file_id
    assert metadata1.file_path == metadata2.file_path


def test_storage_switching(mock_database_service, s3_service):
    """ストレージの切り替えテスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # S3を使用してアップロード
    fm_s3 = FileManager(db_service=mock_database_service, s3_service=s3_service)
    s3_path, _, s3_metadata = fm_s3.upload_interview_file(file_content, filename)
    assert s3_path.startswith("s3://")

    # ローカルストレージを使用してアップロード
    fm_local = FileManager(db_service=mock_database_service, s3_service=None)
    local_path, _, local_metadata = fm_local.upload_interview_file(
        file_content, f"local_{filename}"
    )
    assert not local_path.startswith("s3://")

    # 両方のファイルが一覧に表示される
    files = fm_s3.list_uploaded_files()
    file_paths = [f.file_path for f in files]
    assert any(p.startswith("s3://") for p in file_paths)
    assert any(not p.startswith("s3://") for p in file_paths)


def test_read_s3_file_without_s3_service(file_manager_with_s3, mock_database_service):
    """S3サービスなしでS3ファイルを読もうとするテスト"""
    file_content = b"Test interview content for persona generation."
    filename = "test_interview.txt"

    # S3にアップロード
    s3_path, _, _ = file_manager_with_s3.upload_interview_file(file_content, filename)

    # S3サービスなしのFileManagerを作成
    fm_no_s3 = FileManager(db_service=mock_database_service, s3_service=None)

    # S3ファイルを読もうとするとエラー
    with pytest.raises(FileUploadError) as exc_info:
        fm_no_s3.get_uploaded_file_content(s3_path)
    assert "S3サービスが設定されていません" in str(exc_info.value)
