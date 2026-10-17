"""
S3Serviceの単体テスト
motoを使用してS3をモック
"""

import pytest

# motoモジュールがない場合はテストをスキップ
moto = pytest.importorskip("moto", reason="moto is required for S3 tests")
mock_aws = moto.mock_aws

import boto3  # noqa: E402

from src.models.errors import ErrorCode  # noqa: E402
from src.services.s3_service import S3Service, S3ServiceError  # noqa: E402


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
    with pytest.raises(S3ServiceError) as exc_info:
        s3_service.download_file(s3_path)
    assert exc_info.value.code is ErrorCode.S3_OBJECT_NOT_FOUND


def test_download_nonexistent_file(s3_service):
    """存在しないファイルのダウンロードテスト"""
    s3_path = f"s3://{s3_service.bucket_name}/uploads/nonexistent.txt"

    with pytest.raises(S3ServiceError) as exc_info:
        s3_service.download_file(s3_path)
    assert exc_info.value.code is ErrorCode.S3_OBJECT_NOT_FOUND


def test_extract_key_from_path(s3_service):
    """S3パスからキーを抽出するテスト"""
    s3_path = f"s3://{s3_service.bucket_name}/uploads/test_file.txt"

    key = s3_service._extract_key_from_path(s3_path)

    assert key == "uploads/test_file.txt"


def test_extract_key_from_invalid_path(s3_service):
    """無効なS3パスからのキー抽出テスト"""
    invalid_path = "invalid/path/format"

    with pytest.raises(S3ServiceError) as exc_info:
        s3_service._extract_key_from_path(invalid_path)
    assert exc_info.value.code is ErrorCode.S3_OPERATION_FAILED


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


# =============================================================================
# U5: タグと写し（BR5.2、BR5.3、BR6.1、NFR1.30、infrastructure-specification.md）
# =============================================================================


def _tags(s3_bucket_name: str, key: str) -> dict[str, str]:
    client = boto3.client("s3", region_name="us-east-1")
    tag_set = client.get_object_tagging(Bucket=s3_bucket_name, Key=key)["TagSet"]
    return {t["Key"]: t["Value"] for t in tag_set}


def test_upload_with_tags_places_the_pending_tag(s3_service, s3_bucket_name):
    key = "uploads/sub-a/discussion_document/f_資料.pdf"
    s3_service.upload_file(b"x", key, tags={"pending": "true"})
    assert _tags(s3_bucket_name, key) == {"pending": "true"}


def test_tags_are_removed_put_back_and_read(s3_service, s3_bucket_name):
    key = "uploads/sub-a/survey_image/f_a.png"
    s3_service.upload_file(b"x", key, tags={"pending": "true"})

    s3_service.delete_object_tagging(key)
    assert s3_service.get_object_tags(key) == {}
    s3_service.put_object_tags(key, {"pending": "true"})
    assert s3_service.get_object_tags(key) == {"pending": "true"}


def test_tag_calls_on_a_missing_object_fail(s3_service):
    # moto 5.1 の delete_object_tagging は存在しないキーで内部エラーになるため、
    # 失敗の変換は put / get で確かめる（実 S3 はどれも NoSuchKey を返す）
    with pytest.raises(S3ServiceError):
        s3_service.put_object_tags("uploads/none", {"pending": "true"})
    with pytest.raises(S3ServiceError):
        s3_service.get_object_tags("uploads/none")


def test_copy_object_replaces_tags_with_none(s3_service, s3_bucket_name):
    source = "candidates/sub-a/c/t.csv"
    s3_service.upload_file(b"a,b\n1,2\n", source, tags={"pending": "true"})

    path = s3_service.copy_object(source, "persona_datasets/p1/d1.csv")

    assert path == f"s3://{s3_bucket_name}/persona_datasets/p1/d1.csv"
    assert _tags(s3_bucket_name, "persona_datasets/p1/d1.csv") == {}
    assert s3_service.download_file(path) == b"a,b\n1,2\n"


def test_copy_object_of_a_missing_source_fails(s3_service):
    with pytest.raises(S3ServiceError):
        s3_service.copy_object("candidates/none.csv", "persona_datasets/p/d.csv")


def test_presigned_url_has_an_expiry(s3_service):
    path = s3_service.upload_file(b"x", "uploads/sub-a/survey_image/f_a.png")
    url = s3_service.generate_presigned_url(path, 300)
    assert "Expires=" in url or "X-Amz-Expires=300" in url


def test_paths_and_keys_convert_both_ways(s3_service, s3_bucket_name):
    path = s3_service.path_of("uploads/a")
    assert path == f"s3://{s3_bucket_name}/uploads/a"
    assert s3_service.key_of(path) == "uploads/a"
    assert s3_service.key_of("s3://another-bucket/uploads/a") is None
    assert s3_service.key_of("/tmp/local") is None


def test_exists_tells_whether_an_object_is_there(s3_service):
    """U7 BR2.2・BR2.8: 実行に要るファイルと写しの画像が残っているか。"""
    path = s3_service.upload_file(b"x", "persona-dataset/custom/x.parquet")
    assert s3_service.exists(path) is True
    s3_service.delete_file(path)
    assert s3_service.exists(path) is False
