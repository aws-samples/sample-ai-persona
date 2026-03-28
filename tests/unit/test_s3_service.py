"""
S3Serviceの単体テスト
motoを使用してS3をモック
"""

import pytest

# motoモジュールがない場合はテストをスキップ
moto = pytest.importorskip("moto", reason="moto is required for S3 tests")
mock_aws = moto.mock_aws

import boto3  # noqa: E402

from src.services.s3_service import S3Service  # noqa: E402


@pytest.fixture
def s3_bucket_name():
    """テスト用S3バケット名"""
    return "test-bucket"


@pytest.fixture
def aws_region():
    """テスト用AWSリージョン"""
    return "us-east-1"


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


def test_upload_file(s3_service, s3_bucket_name):
    """ファイルアップロードのテスト"""
    file_content = b"Test file content"
    s3_key = "uploads/test_file.txt"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # 検証
    assert s3_path == f"s3://{s3_bucket_name}/{s3_key}"


def test_download_file(s3_service, s3_bucket_name):
    """ファイルダウンロードのテスト"""
    file_content = b"Test file content"
    s3_key = "uploads/test_file.txt"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # ダウンロード
    downloaded_content = s3_service.download_file(s3_path)

    # 検証
    assert downloaded_content == file_content


def test_delete_file(s3_service, s3_bucket_name):
    """ファイル削除のテスト"""
    file_content = b"Test file content"
    s3_key = "uploads/test_file.txt"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # 削除
    result = s3_service.delete_file(s3_path)

    # 検証
    assert result is True

    # 削除後のダウンロードは失敗する
    with pytest.raises(Exception) as exc_info:
        s3_service.download_file(s3_path)
    assert "ファイルが見つかりません" in str(exc_info.value)


def test_download_nonexistent_file(s3_service):
    """存在しないファイルのダウンロードテスト"""
    s3_path = f"s3://{s3_service.bucket_name}/uploads/nonexistent.txt"

    with pytest.raises(Exception) as exc_info:
        s3_service.download_file(s3_path)
    assert "ファイルが見つかりません" in str(exc_info.value)


def test_extract_key_from_path(s3_service):
    """S3パスからキーを抽出するテスト"""
    s3_path = f"s3://{s3_service.bucket_name}/uploads/test_file.txt"

    key = s3_service._extract_key_from_path(s3_path)

    assert key == "uploads/test_file.txt"


def test_extract_key_from_invalid_path(s3_service):
    """無効なS3パスからのキー抽出テスト"""
    invalid_path = "invalid/path/format"

    with pytest.raises(ValueError) as exc_info:
        s3_service._extract_key_from_path(invalid_path)
    assert "Invalid S3 path format" in str(exc_info.value)


def test_upload_large_file(s3_service, s3_bucket_name):
    """大きなファイルのアップロードテスト"""
    # 1MBのファイル
    file_content = b"x" * (1024 * 1024)
    s3_key = "uploads/large_file.txt"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # ダウンロードして検証
    downloaded_content = s3_service.download_file(s3_path)
    assert len(downloaded_content) == len(file_content)
    assert downloaded_content == file_content


def test_upload_empty_file(s3_service, s3_bucket_name):
    """空ファイルのアップロードテスト"""
    file_content = b""
    s3_key = "uploads/empty_file.txt"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # ダウンロードして検証
    downloaded_content = s3_service.download_file(s3_path)
    assert downloaded_content == file_content


def test_upload_with_special_characters(s3_service, s3_bucket_name):
    """特殊文字を含むファイル名のアップロードテスト"""
    file_content = b"Test content"
    s3_key = "uploads/test_file_with_特殊文字.txt"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # ダウンロードして検証
    downloaded_content = s3_service.download_file(s3_path)
    assert downloaded_content == file_content


def test_generate_presigned_url(s3_service, s3_bucket_name):
    """署名付きURL生成のテスト"""
    file_content = b"Test file content for presigned URL"
    s3_key = "discussion_documents/test_image.png"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # 署名付きURL生成
    presigned_url = s3_service.generate_presigned_url(s3_path)

    # 検証
    assert presigned_url is not None
    assert s3_bucket_name in presigned_url
    assert s3_key in presigned_url


def test_generate_presigned_url_with_custom_expiration(s3_service, s3_bucket_name):
    """カスタム有効期限付き署名付きURL生成のテスト"""
    file_content = b"Test file content"
    s3_key = "discussion_documents/test_doc.pdf"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # 署名付きURL生成（30分有効）
    presigned_url = s3_service.generate_presigned_url(s3_path, expiration=1800)

    # 検証
    assert presigned_url is not None
    assert s3_bucket_name in presigned_url


def test_generate_presigned_url_for_nonexistent_file(s3_service, s3_bucket_name):
    """存在しないファイルの署名付きURL生成テスト（URLは生成されるが、アクセス時にエラー）"""
    s3_path = f"s3://{s3_bucket_name}/discussion_documents/nonexistent.png"

    # 署名付きURLは生成される（ファイルの存在チェックは行われない）
    presigned_url = s3_service.generate_presigned_url(s3_path)

    # URLは生成される
    assert presigned_url is not None
    assert s3_bucket_name in presigned_url


def test_upload_discussion_document_to_s3(s3_service, s3_bucket_name):
    """議論用ドキュメントのS3アップロードテスト"""
    # 画像ファイルのシミュレーション
    file_content = b"\x89PNG\r\n\x1a\n" + b"x" * 1000  # PNG magic bytes + content
    s3_key = "discussion_documents/uuid_test_image.png"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # 検証
    assert s3_path == f"s3://{s3_bucket_name}/{s3_key}"

    # ダウンロードして検証
    downloaded_content = s3_service.download_file(s3_path)
    assert downloaded_content == file_content


def test_upload_pdf_document_to_s3(s3_service, s3_bucket_name):
    """PDFドキュメントのS3アップロードテスト"""
    # PDFファイルのシミュレーション
    file_content = b"%PDF-1.4" + b"x" * 1000  # PDF magic bytes + content
    s3_key = "discussion_documents/uuid_test_document.pdf"

    # アップロード
    s3_path = s3_service.upload_file(file_content, s3_key)

    # 検証
    assert s3_path == f"s3://{s3_bucket_name}/{s3_key}"

    # ダウンロードして検証
    downloaded_content = s3_service.download_file(s3_path)
    assert downloaded_content == file_content
