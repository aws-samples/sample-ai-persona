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
from src.services.s3_service import (  # noqa: E402
    S3ObjectExistsError,
    S3Service,
    S3ServiceError,
)
from tests.error_helpers import raises_code  # noqa: E402


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


# --- 閉域対応 U4: ストリームでの読み取り（BR2.2、BR2.5、BR2.6、BR4.5） ---------------

CHUNK = 256 * 1024


class _Body:
    """A fake ``StreamingBody`` that records reads and closes."""

    def __init__(self, data: bytes, fail_after: int | None = None):
        self._data = data
        self._pos = 0
        self._reads = 0
        self._fail_after = fail_after
        self.close_calls = 0

    def read(self, size: int = -1) -> bytes:
        if self._fail_after is not None and self._reads >= self._fail_after:
            raise ConnectionResetError("broken")
        self._reads += 1
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.close_calls += 1


def _fake_object(s3_service, monkeypatch, body: _Body) -> None:
    monkeypatch.setattr(
        s3_service.s3_client,
        "get_object",
        lambda **_: {"Body": body, "ContentLength": len(body._data)},
    )


def test_open_stream_returns_the_size_and_256_kib_chunks(s3_service):
    data = bytes(range(256)) * (CHUNK * 2 // 256) + b"tail"
    path = s3_service.upload_file(data, "uploads/sub-a/discussion/f_a.pdf")

    stream = s3_service.open_stream(path)
    chunks = list(stream.chunks)

    assert stream.content_length == len(data)
    assert [len(c) for c in chunks] == [CHUNK, CHUNK, 4]
    assert b"".join(chunks) == data


def test_open_stream_of_a_missing_key_is_not_found(s3_service, s3_bucket_name):
    with raises_code(S3ServiceError, ErrorCode.S3_OBJECT_NOT_FOUND):
        s3_service.open_stream(f"s3://{s3_bucket_name}/uploads/none.png")


def test_open_stream_other_client_errors_are_operation_failures(
    s3_service, monkeypatch
):
    from botocore.exceptions import ClientError

    def deny(**_):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    monkeypatch.setattr(s3_service.s3_client, "get_object", deny)
    with raises_code(S3ServiceError, ErrorCode.S3_OPERATION_FAILED):
        s3_service.open_stream("s3://b/uploads/a.png")


def test_open_stream_connection_failures_are_operation_failures(
    s3_service, monkeypatch
):
    from botocore.exceptions import EndpointConnectionError

    def unreachable(**_):
        raise EndpointConnectionError(endpoint_url="https://s3")

    monkeypatch.setattr(s3_service.s3_client, "get_object", unreachable)
    with raises_code(S3ServiceError, ErrorCode.S3_OPERATION_FAILED):
        s3_service.open_stream("s3://b/uploads/a.png")


def test_close_closes_the_body_and_may_be_called_twice(s3_service, monkeypatch):
    body = _Body(b"x" * (CHUNK * 3))
    _fake_object(s3_service, monkeypatch, body)

    stream = s3_service.open_stream("s3://b/uploads/a.png")
    next(stream.chunks)
    stream.close()
    stream.close()

    assert body.close_calls == 1


def test_reading_to_the_end_closes_the_body(s3_service, monkeypatch):
    body = _Body(b"x" * (CHUNK + 1))
    _fake_object(s3_service, monkeypatch, body)

    stream = s3_service.open_stream("s3://b/uploads/a.png")
    assert sum(len(c) for c in stream.chunks) == CHUNK + 1

    assert body.close_calls == 1


def test_abandoning_the_iterator_closes_the_body(s3_service, monkeypatch):
    body = _Body(b"x" * (CHUNK * 3))
    _fake_object(s3_service, monkeypatch, body)

    stream = s3_service.open_stream("s3://b/uploads/a.png")
    next(stream.chunks)
    stream.chunks.close()  # type: ignore[attr-defined]

    assert body.close_calls == 1


def test_a_failure_while_streaming_closes_the_body(s3_service, monkeypatch):
    body = _Body(b"x" * (CHUNK * 3), fail_after=1)
    _fake_object(s3_service, monkeypatch, body)

    stream = s3_service.open_stream("s3://b/uploads/a.png")
    next(stream.chunks)
    with pytest.raises(ConnectionResetError):
        next(stream.chunks)

    assert body.close_calls == 1


# --- アンケートのオンデマンド推論（functional-spec.md 2 章、reliability 3.1） ------


def _failing_client(monkeypatch, s3_service, operation: str, code: str) -> None:
    from unittest.mock import Mock

    from botocore.exceptions import ClientError

    client = Mock()
    getattr(client, operation).side_effect = ClientError(
        {"Error": {"Code": code, "Message": "m"}}, operation
    )
    monkeypatch.setattr(s3_service, "s3_client", client)


def test_nemotron_parquet_presence_is_checked_by_key(s3_service, s3_bucket_name):
    """Manager が head_object を直接呼んでいた箇所の置き換え（exists + path_of）。"""
    key = "datasets/nemotron/nemotron.parquet"
    assert not s3_service.exists(s3_service.path_of(key))

    s3_service.upload_file(b"PAR1", key)

    assert s3_service.exists(s3_service.path_of(key))


def test_result_csv_presence(s3_service):
    path = s3_service.upload_file(b"a,b\n", "survey-results/s-1/results.csv")

    assert s3_service.exists(path)
    s3_service.delete_file(path)
    assert not s3_service.exists(path)


@pytest.mark.parametrize(
    ("code", "failure_name"),
    [("AccessDenied", "ACCESS"), ("403", "ACCESS"), ("SlowDown", "TRANSIENT")],
)
def test_head_failures_carry_the_failure_class(
    s3_service, monkeypatch, code, failure_name
):
    from src.models.external_failure import FailureClass

    _failing_client(monkeypatch, s3_service, "head_object", code)

    with pytest.raises(S3ServiceError) as info:
        s3_service.exists("s3://test-bucket/k")
    assert info.value.failure is FailureClass[failure_name]
    assert info.value.code is ErrorCode.S3_OPERATION_FAILED


@pytest.mark.parametrize(
    ("code", "failure_name"),
    [("AccessDenied", "ACCESS"), ("NoSuchBucket", "ACCESS"), ("500", "TRANSIENT")],
)
def test_upload_failures_carry_the_failure_class(
    s3_service, monkeypatch, code, failure_name
):
    """完了のまとめで CSV を置けないとき、Manager は分類で扱いを決める（NFR4.8）。"""
    from src.models.external_failure import FailureClass

    _failing_client(monkeypatch, s3_service, "put_object", code)

    with pytest.raises(S3ServiceError) as info:
        s3_service.upload_file(b"x", "survey-results/s-1/results.csv")
    assert info.value.failure is FailureClass[failure_name]


# --- 高度な生成（ベータ）U1: 分割アップロードと先頭の読み取り ---------------------------
# US2.1、US3.1、BR2.3、BR2.4、BR2.7、BR3.1、NFR3.5、NFR3.12（security-design.md §3）

SOURCE_KEY = "sources/sub-a/11111111-2222-3333-4444-555555555555"
MIB = 1024 * 1024


def _query(url: str) -> dict[str, str]:
    from urllib.parse import parse_qsl, urlsplit

    return dict(parse_qsl(urlsplit(url).query))


def _upload_parts(s3_service, upload_id: str, sizes: list[int]) -> list[dict]:
    parts = []
    for number, size in enumerate(sizes, start=1):
        response = s3_service.s3_client.upload_part(
            Bucket=s3_service.bucket_name,
            Key=SOURCE_KEY,
            UploadId=upload_id,
            PartNumber=number,
            Body=b"a" * size,
        )
        parts.append({"part_number": number, "etag": response["ETag"]})
    return parts


class TestClientSigning:
    def test_client_signs_with_sigv4_virtual_hosted_and_regional(self, s3_service):
        client = s3_service.s3_client

        assert client.meta.config.signature_version == "s3v4"
        assert client.meta.config.s3["addressing_style"] == "virtual"
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"

    def test_get_url_is_sigv4_on_the_regional_virtual_host(
        self, s3_service, s3_bucket_name
    ):
        from urllib.parse import urlsplit

        url = s3_service.generate_presigned_url(f"s3://{s3_bucket_name}/a/b.png", 900)

        assert urlsplit(url).netloc == f"{s3_bucket_name}.s3.us-east-1.amazonaws.com"
        query = _query(url)
        assert query["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
        assert query["X-Amz-Expires"] == "900"


class TestMultipartUpload:
    def test_create_returns_an_upload_id(self, s3_service):
        upload_id = s3_service.create_multipart_upload(SOURCE_KEY, "text/csv")

        assert upload_id
        listed = s3_service.s3_client.list_multipart_uploads(
            Bucket=s3_service.bucket_name
        )
        assert [u["Key"] for u in listed["Uploads"]] == [SOURCE_KEY]

    def test_part_urls_sign_only_bucket_key_upload_id_and_part_number(
        self, s3_service, s3_bucket_name
    ):
        from urllib.parse import urlsplit

        upload_id = s3_service.create_multipart_upload(SOURCE_KEY, "text/csv")

        urls = s3_service.generate_upload_part_urls(
            f"s3://{s3_bucket_name}/{SOURCE_KEY}", upload_id, [1, 3]
        )

        assert [u.part_number for u in urls] == [1, 3]
        for part in urls:
            parts = urlsplit(part.url)
            assert parts.netloc == f"{s3_bucket_name}.s3.us-east-1.amazonaws.com"
            assert parts.path == f"/{SOURCE_KEY}"
            query = _query(part.url)
            assert query["partNumber"] == str(part.part_number)
            assert query["uploadId"] == upload_id
            assert query["X-Amz-Expires"] == "3600"
            # Only the host header is signed: no Content-MD5 / checksum headers.
            assert query["X-Amz-SignedHeaders"] == "host"
            assert not any("checksum" in k.lower() for k in query)

    def test_part_urls_take_a_custom_expiration(self, s3_service, s3_bucket_name):
        upload_id = s3_service.create_multipart_upload(SOURCE_KEY, "text/csv")

        (part,) = s3_service.generate_upload_part_urls(
            f"s3://{s3_bucket_name}/{SOURCE_KEY}", upload_id, [2], expiration=60
        )

        assert _query(part.url)["X-Amz-Expires"] == "60"

    def test_list_parts_reports_sizes_and_etags(self, s3_service):
        upload_id = s3_service.create_multipart_upload(SOURCE_KEY, "text/csv")
        sent = _upload_parts(s3_service, upload_id, [5 * MIB, 10])

        parts = s3_service.list_parts(SOURCE_KEY, upload_id)

        assert [(p.part_number, p.size_bytes, p.etag) for p in parts] == [
            (1, 5 * MIB, sent[0]["etag"]),
            (2, 10, sent[1]["etag"]),
        ]

    def test_complete_joins_the_parts(self, s3_service):
        upload_id = s3_service.create_multipart_upload(SOURCE_KEY, "text/csv")
        sent = _upload_parts(s3_service, upload_id, [5 * MIB, 10])

        s3_service.complete_multipart_upload(
            SOURCE_KEY, upload_id, [(p["part_number"], p["etag"]) for p in sent]
        )

        head = s3_service.s3_client.head_object(
            Bucket=s3_service.bucket_name, Key=SOURCE_KEY
        )
        assert head["ContentLength"] == 5 * MIB + 10

    def test_abort_drops_the_parts_and_is_idempotent(self, s3_service):
        upload_id = s3_service.create_multipart_upload(SOURCE_KEY, "text/csv")
        _upload_parts(s3_service, upload_id, [10])

        assert s3_service.abort_multipart_upload(SOURCE_KEY, upload_id) is True
        assert s3_service.abort_multipart_upload(SOURCE_KEY, upload_id) is False
        listed = s3_service.s3_client.list_multipart_uploads(
            Bucket=s3_service.bucket_name
        )
        assert not listed.get("Uploads")

    def test_list_parts_of_an_unknown_upload_is_not_found(self, s3_service):
        with raises_code(S3ServiceError, ErrorCode.S3_OBJECT_NOT_FOUND):
            s3_service.list_parts(SOURCE_KEY, "no-such-upload")

    def test_complete_of_an_unknown_upload_is_not_found(self, s3_service, monkeypatch):
        # moto raises KeyError here; S3 answers NoSuchUpload.
        _failing_client(
            monkeypatch, s3_service, "complete_multipart_upload", "NoSuchUpload"
        )

        with raises_code(S3ServiceError, ErrorCode.S3_OBJECT_NOT_FOUND):
            s3_service.complete_multipart_upload(SOURCE_KEY, "no-such-upload", [])

    @pytest.mark.parametrize(
        "operation",
        [
            "create_multipart_upload",
            "list_parts",
            "complete_multipart_upload",
            "abort_multipart_upload",
        ],
    )
    def test_failures_become_s3_service_errors(
        self, s3_service, monkeypatch, operation
    ):
        _failing_client(monkeypatch, s3_service, operation, "InternalError")
        calls = {
            "create_multipart_upload": lambda: s3_service.create_multipart_upload(
                SOURCE_KEY, "text/csv"
            ),
            "list_parts": lambda: s3_service.list_parts(SOURCE_KEY, "u"),
            "complete_multipart_upload": lambda: (
                s3_service.complete_multipart_upload(SOURCE_KEY, "u", [(1, "e")])
            ),
            "abort_multipart_upload": lambda: s3_service.abort_multipart_upload(
                SOURCE_KEY, "u"
            ),
        }

        with raises_code(S3ServiceError, ErrorCode.S3_OPERATION_FAILED):
            calls[operation]()


class TestHeadReads:
    def test_read_range_reads_only_the_head(self, s3_service):
        s3_service.upload_file(b"0123456789", SOURCE_KEY)

        assert s3_service.read_range(SOURCE_KEY, 4) == b"0123"
        assert s3_service.read_range(SOURCE_KEY, 100) == b"0123456789"

    def test_read_range_sends_a_range_header(self, s3_service, monkeypatch):
        from unittest.mock import Mock

        client = Mock()
        client.get_object.return_value = {"Body": Mock(read=Mock(return_value=b"ab"))}
        monkeypatch.setattr(s3_service, "s3_client", client)

        s3_service.read_range(SOURCE_KEY, 4 * MIB)

        assert client.get_object.call_args.kwargs["Range"] == f"bytes=0-{4 * MIB - 1}"

    def test_read_range_of_a_missing_key_is_not_found(self, s3_service):
        with raises_code(S3ServiceError, ErrorCode.S3_OBJECT_NOT_FOUND):
            s3_service.read_range(SOURCE_KEY, 4)

    # U4 NFR3.25: the tail of a document (the PDF trailer, the ZIP directory)

    def test_read_tail_reads_only_the_end(self, s3_service):
        s3_service.upload_file(b"0123456789", SOURCE_KEY)

        assert s3_service.read_tail(SOURCE_KEY, 3) == b"789"

    def test_read_tail_of_a_short_object_is_the_whole(self, s3_service):
        s3_service.upload_file(b"0123456789", SOURCE_KEY)

        assert s3_service.read_tail(SOURCE_KEY, 100) == b"0123456789"

    def test_read_tail_sends_a_suffix_range(self, s3_service, monkeypatch):
        from unittest.mock import Mock

        client = Mock()
        client.get_object.return_value = {"Body": Mock(read=Mock(return_value=b"ab"))}
        monkeypatch.setattr(s3_service, "s3_client", client)

        s3_service.read_tail(SOURCE_KEY, MIB)

        assert client.get_object.call_args.kwargs["Range"] == f"bytes=-{MIB}"

    def test_read_tail_of_a_missing_key_is_not_found(self, s3_service):
        with raises_code(S3ServiceError, ErrorCode.S3_OBJECT_NOT_FOUND):
            s3_service.read_tail(SOURCE_KEY, 4)

    def test_checksum_reads_crc64nvme_with_checksum_mode(self, s3_service, monkeypatch):
        from unittest.mock import Mock

        client = Mock()
        client.head_object.return_value = {
            "ContentLength": 3,
            "ChecksumCRC64NVME": "AAAAAAAAAAA=",
            "ChecksumType": "FULL_OBJECT",
        }
        monkeypatch.setattr(s3_service, "s3_client", client)

        assert s3_service.get_checksum(SOURCE_KEY) == "AAAAAAAAAAA="
        assert client.head_object.call_args.kwargs["ChecksumMode"] == "ENABLED"

    def test_checksum_is_none_when_s3_has_none(self, s3_service, monkeypatch):
        from unittest.mock import Mock

        client = Mock()
        client.head_object.return_value = {"ContentLength": 3}
        monkeypatch.setattr(s3_service, "s3_client", client)

        assert s3_service.get_checksum(SOURCE_KEY) is None

    def test_checksum_of_a_missing_key_is_not_found(self, s3_service):
        with raises_code(S3ServiceError, ErrorCode.S3_OBJECT_NOT_FOUND):
            s3_service.get_checksum(SOURCE_KEY)


class TestConditionalPut:
    RECORD_KEY = "analysis-records/sub-a/gen-1.json"

    def test_put_new_object_writes_the_pending_tag(self, s3_service):
        s3_service.put_new_object(
            self.RECORD_KEY, b"{}", "application/json", tags={"pending": "true"}
        )

        assert s3_service.get_object_tags(self.RECORD_KEY) == {"pending": "true"}

    def test_put_new_object_refuses_to_overwrite(self, s3_service, monkeypatch):
        from unittest.mock import Mock

        from botocore.exceptions import ClientError

        client = Mock()
        client.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed", "Message": "m"}}, "PutObject"
        )
        monkeypatch.setattr(s3_service, "s3_client", client)

        with pytest.raises(S3ObjectExistsError):
            s3_service.put_new_object(self.RECORD_KEY, b"{}", "application/json")
        assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"

    def test_put_new_object_other_failures(self, s3_service, monkeypatch):
        _failing_client(monkeypatch, s3_service, "put_object", "InternalError")

        with raises_code(S3ServiceError, ErrorCode.S3_OPERATION_FAILED):
            s3_service.put_new_object(self.RECORD_KEY, b"{}", "application/json")


# --- 高度な生成 U2: 全版の削除とコピーし直し（BR13.4、BR14.1、NFR3.14、NFR3.19） ---


def _versioned(s3_bucket_name: str) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    client.put_bucket_versioning(
        Bucket=s3_bucket_name, VersioningConfiguration={"Status": "Enabled"}
    )


def _versions(s3_bucket_name: str) -> list[tuple[str, str]]:
    client = boto3.client("s3", region_name="us-east-1")
    listed = client.list_object_versions(Bucket=s3_bucket_name, Prefix="sources/")
    return sorted(
        (v["Key"], v["VersionId"])
        for v in listed.get("Versions", []) + listed.get("DeleteMarkers", [])
    )


class TestDeleteAllVersions:
    def test_deletes_every_version_and_marker_of_the_key_only(
        self, s3_service, s3_bucket_name
    ):
        _versioned(s3_bucket_name)
        s3_service.upload_file(b"v1", SOURCE_KEY)
        s3_service.upload_file(b"v2", SOURCE_KEY)
        s3_service.delete_file(s3_service.path_of(SOURCE_KEY))  # a delete marker
        s3_service.upload_file(b"other", SOURCE_KEY + "-x")

        removed = s3_service.delete_all_versions(SOURCE_KEY)

        assert removed == 3
        remaining = _versions(s3_bucket_name)
        assert [k for k, _ in remaining] == [SOURCE_KEY + "-x"]

    def test_no_version_is_zero(self, s3_service, s3_bucket_name):
        _versioned(s3_bucket_name)

        assert s3_service.delete_all_versions(SOURCE_KEY) == 0

    def test_more_than_1000_versions_are_deleted_in_batches(
        self, s3_service, monkeypatch
    ):
        from unittest.mock import Mock

        client = Mock()
        page_one = {
            "Versions": [{"Key": SOURCE_KEY, "VersionId": f"v{i}"} for i in range(900)],
            "DeleteMarkers": [
                {"Key": SOURCE_KEY, "VersionId": f"m{i}"} for i in range(200)
            ],
            "IsTruncated": True,
            "NextKeyMarker": SOURCE_KEY,
            "NextVersionIdMarker": "m199",
        }
        page_two = {
            "Versions": [{"Key": SOURCE_KEY, "VersionId": f"w{i}"} for i in range(5)]
            + [{"Key": SOURCE_KEY + "-x", "VersionId": "keep"}],
            "IsTruncated": False,
        }
        client.list_object_versions.side_effect = [page_one, page_two]
        client.delete_objects.return_value = {"Deleted": []}
        monkeypatch.setattr(s3_service, "s3_client", client)

        removed = s3_service.delete_all_versions(SOURCE_KEY)

        assert removed == 1105
        second = client.list_object_versions.call_args_list[1].kwargs
        assert second["KeyMarker"] == SOURCE_KEY
        assert second["VersionIdMarker"] == "m199"
        batches = [
            c.kwargs["Delete"]["Objects"] for c in client.delete_objects.call_args_list
        ]
        assert [len(b) for b in batches] == [1000, 105]
        assert all(o["Key"] == SOURCE_KEY for b in batches for o in b)
        assert all(
            c.kwargs["Delete"]["Quiet"] is True
            for c in client.delete_objects.call_args_list
        )

    def test_errors_in_the_response_are_a_failure(self, s3_service, monkeypatch):
        from unittest.mock import Mock

        client = Mock()
        client.list_object_versions.return_value = {
            "Versions": [{"Key": SOURCE_KEY, "VersionId": "v1"}],
            "IsTruncated": False,
        }
        client.delete_objects.return_value = {
            "Errors": [{"Key": SOURCE_KEY, "VersionId": "v1", "Code": "AccessDenied"}]
        }
        monkeypatch.setattr(s3_service, "s3_client", client)

        with raises_code(S3ServiceError, ErrorCode.S3_OPERATION_FAILED):
            s3_service.delete_all_versions(SOURCE_KEY)

    def test_a_failed_listing_is_a_failure(self, s3_service, monkeypatch):
        _failing_client(monkeypatch, s3_service, "list_object_versions", "AccessDenied")

        with raises_code(S3ServiceError, ErrorCode.S3_OPERATION_FAILED):
            s3_service.delete_all_versions(SOURCE_KEY)


class TestRefreshObject:
    def test_copies_onto_the_same_key_and_returns_the_checksum(
        self, s3_service, monkeypatch
    ):
        from unittest.mock import Mock

        client = Mock()
        client.head_object.return_value = {"ContentType": "text/csv"}
        client.copy_object.return_value = {
            "CopyObjectResult": {"ChecksumCRC64NVME": "AAAAAAAAAAA="}
        }
        monkeypatch.setattr(s3_service, "s3_client", client)

        assert s3_service.refresh_object(SOURCE_KEY) == "AAAAAAAAAAA="

        kwargs = client.copy_object.call_args.kwargs
        assert kwargs["Key"] == SOURCE_KEY
        assert kwargs["CopySource"] == {
            "Bucket": s3_service.bucket_name,
            "Key": SOURCE_KEY,
        }
        assert kwargs["MetadataDirective"] == "REPLACE"
        assert kwargs["ContentType"] == "text/csv"
        assert kwargs["ServerSideEncryption"] == "AES256"
        assert kwargs["ChecksumAlgorithm"] == "CRC64NVME"
        assert kwargs["TaggingDirective"] == "REPLACE"

    def test_no_checksum_in_the_answer_is_none(self, s3_service, monkeypatch):
        from unittest.mock import Mock

        client = Mock()
        client.head_object.return_value = {}
        client.copy_object.return_value = {"CopyObjectResult": {}}
        monkeypatch.setattr(s3_service, "s3_client", client)

        assert s3_service.refresh_object(SOURCE_KEY) is None
        assert (
            client.copy_object.call_args.kwargs["ContentType"]
            == "application/octet-stream"
        )

    def test_the_object_stays_readable(self, s3_service):
        s3_service.upload_file(b"id,age\n1,30\n", SOURCE_KEY, content_type="text/csv")

        s3_service.refresh_object(SOURCE_KEY)

        assert s3_service.read_range(SOURCE_KEY, 100) == b"id,age\n1,30\n"

    def test_a_missing_object_is_not_found(self, s3_service):
        with raises_code(S3ServiceError, ErrorCode.S3_OBJECT_NOT_FOUND):
            s3_service.refresh_object(SOURCE_KEY)

    def test_copy_object_keeps_its_arguments(self, s3_service, monkeypatch):
        from unittest.mock import Mock

        client = Mock()
        monkeypatch.setattr(s3_service, "s3_client", client)

        s3_service.copy_object("candidates/a.csv", "persona_datasets/p/d.csv")

        assert set(client.copy_object.call_args.kwargs) == {
            "Bucket",
            "Key",
            "CopySource",
            "TaggingDirective",
        }
