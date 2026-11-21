"""
S3サービス
Amazon S3へのファイルアップロード・ダウンロード・削除を管理
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from ..models.asset_delivery import AssetStream
from ..models.errors import CodedError, ErrorCode
from ..models.external_failure import FailureClass, classify_aws_error
from ..models.source_file import PartUrl

logger = logging.getLogger(__name__)

#: S3 error codes that mean "the object or the multipart upload is not there".
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchUpload", "404", "NotFound"})
#: ``ListParts`` returns at most this many parts per page.
_LIST_PARTS_PAGE = 1000
#: ``DeleteObjects`` takes at most this many keys per request.
_DELETE_BATCH = 1000


def _s3_client(region_name: str) -> Any:
    """An S3 client that signs with SigV4 on the regional virtual host.

    Presigned URLs (the existing GET and the part PUT of advanced generation)
    then look like ``https://<bucket>.s3.<region>.amazonaws.com/<key>?X-Amz-...``
    (security-design.md §3, NFR3.12): SigV4 only, and no redirect from the
    global endpoint that a browser PUT could not follow.
    """
    return boto3.client(
        "s3",
        region_name=region_name,
        endpoint_url=f"https://s3.{region_name}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


@dataclass(frozen=True)
class UploadedPart:
    """One part S3 holds for a multipart upload (``ListParts``)."""

    part_number: int
    size_bytes: int
    etag: str


class S3ServiceError(CodedError):
    """S3サービス関連のエラー

    Attributes:
        failure: How a background caller should handle it (reliability 3.1).
    """

    def __init__(
        self,
        message: str = "",
        *,
        code: ErrorCode | None = None,
        failure: FailureClass = FailureClass.TRANSIENT,
    ) -> None:
        super().__init__(message, code=code)
        self.failure = failure


class S3ObjectExistsError(S3ServiceError):
    """A conditional put found an object already at the key (``IfNoneMatch``)."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message, code=ErrorCode.S3_OPERATION_FAILED)


#: Size of one streamed read (closed network U4, BR2.2).
STREAM_CHUNK_SIZE = 256 * 1024


def _closer(body: Any, s3_key: str) -> Callable[[], None]:
    """An idempotent close of an S3 body (BR4.5)."""
    closed = False

    def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            body.close()
        except Exception as e:
            logger.warning("Closing S3 body of %s failed: %s", s3_key, type(e).__name__)

    return close


def _read_chunks(body: Any, s3_key: str, close: Callable[[], None]) -> Iterator[bytes]:
    """Read ``body`` in chunks; always ``close`` it when the reading stops (BR4.5)."""
    try:
        while True:
            chunk = body.read(STREAM_CHUNK_SIZE)
            if not chunk:
                return
            yield bytes(chunk)
    except Exception as e:
        # Headers are already sent: only the key and the type are logged.
        logger.warning("S3 stream of %s broke: %s", s3_key, type(e).__name__)
        raise
    finally:
        close()


class S3Service:
    """S3操作を管理するサービスクラス"""

    def __init__(self, bucket_name: str, region_name: str = "us-east-1"):
        """
        S3サービスを初期化

        Args:
            bucket_name: S3バケット名
            region_name: AWSリージョン名
        """
        self.bucket_name = bucket_name
        self.region_name = region_name
        self.s3_client = _s3_client(region_name)
        logger.info(f"S3Service initialized with bucket: {bucket_name}")

    def upload_file(
        self,
        file_content: bytes,
        s3_key: str,
        content_type: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """
        S3にファイルをアップロード

        Args:
            file_content: アップロードするファイルの内容（バイト列）
            s3_key: S3オブジェクトキー（例: "uploads/uuid_filename.txt"）
            content_type: オブジェクトのContent-Type（未指定時はS3の既定値）
            tags: Object tags to place with the object (U5: ``pending=true``
                on unattached uploads, BR5.2)

        Returns:
            S3パス（例: "s3://bucket-name/uploads/uuid_filename.txt"）

        Raises:
            Exception: アップロードに失敗した場合
        """
        try:
            put_kwargs: dict[str, Any] = {
                "Bucket": self.bucket_name,
                "Key": s3_key,
                "Body": file_content,
            }
            if content_type:
                put_kwargs["ContentType"] = content_type
            if tags:
                put_kwargs["Tagging"] = urlencode(dict(tags))
            self.s3_client.put_object(**put_kwargs)
            s3_path = f"s3://{self.bucket_name}/{s3_key}"
            logger.info(f"File uploaded successfully to {s3_path}")
            return s3_path
        except NoCredentialsError as e:
            logger.error("AWS credentials not found")
            raise S3ServiceError(
                "AWS credentials not found",
                code=ErrorCode.S3_OPERATION_FAILED,
                failure=FailureClass.ACCESS,
            ) from e
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(f"Failed to upload file to S3: {error_code} - {str(e)}")
            raise S3ServiceError(
                f"s3 upload failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
                failure=classify_aws_error(error_code),
            ) from e
        except BotoCoreError as e:
            logger.error("S3 upload unreachable: %s", type(e).__name__)
            raise S3ServiceError(
                f"s3 upload unreachable ({type(e).__name__})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e

    def download_file(self, s3_path: str) -> bytes:
        """
        S3からファイルをダウンロード

        Args:
            s3_path: S3パス（例: "s3://bucket-name/uploads/uuid_filename.txt"）

        Returns:
            ファイル内容（バイト列）

        Raises:
            Exception: ダウンロードに失敗した場合
        """
        try:
            # S3パスからバケット名とキーを抽出
            s3_key = self._extract_key_from_path(s3_path)

            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=s3_key)
            file_content = response["Body"].read()
            logger.info(f"File downloaded successfully from {s3_path}")
            return bytes(file_content)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "NoSuchKey":
                logger.error(f"File not found in S3: {s3_path}")
                raise S3ServiceError(
                    "s3 object not found",
                    code=ErrorCode.S3_OBJECT_NOT_FOUND,
                ) from e
            logger.error(f"Failed to download file from S3: {error_code} - {str(e)}")
            raise S3ServiceError(
                f"s3 download failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e

    def open_stream(self, s3_path: str) -> AssetStream:
        """Open an object to stream it in 256 KiB chunks (closed network U4).

        The object is opened before anything is returned, so a missing or
        unreadable object is an error code, never a half-sent response
        (BR2.2, BR2.5, BR2.6). The body is closed when the chunks are read to
        the end, when reading fails, when the iterator is discarded, or when
        ``close`` is called; ``close`` may be called any number of times (BR4.5).

        Raises:
            S3ServiceError: ``S3_OBJECT_NOT_FOUND`` (no such key) or
                ``S3_OPERATION_FAILED`` (any other client or connection error).
        """
        s3_key = self._extract_key_from_path(s3_path)
        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=s3_key)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code in ("NoSuchKey", "404", "NotFound"):
                raise S3ServiceError(
                    "s3 object not found", code=ErrorCode.S3_OBJECT_NOT_FOUND
                ) from e
            logger.warning("S3 get_object (stream) failed: %s", error_code)
            raise S3ServiceError(
                f"s3 stream open failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e
        except BotoCoreError as e:
            logger.warning("S3 get_object (stream) unreachable: %s", type(e).__name__)
            raise S3ServiceError(
                f"s3 stream open unreachable ({type(e).__name__})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e
        body = response["Body"]
        close = _closer(body, s3_key)
        return AssetStream(
            chunks=_read_chunks(body, s3_key, close),
            content_length=int(response.get("ContentLength", 0)),
            close=close,
        )

    def delete_file(self, s3_path: str) -> bool:
        """
        S3からファイルを削除

        Args:
            s3_path: S3パス（例: "s3://bucket-name/uploads/uuid_filename.txt"）

        Returns:
            削除成功時True、失敗時False

        Raises:
            Exception: 削除に失敗した場合
        """
        try:
            # S3パスからバケット名とキーを抽出
            s3_key = self._extract_key_from_path(s3_path)

            self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
            logger.info(f"File deleted successfully from {s3_path}")
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(f"Failed to delete file from S3: {error_code} - {str(e)}")
            raise S3ServiceError(
                f"s3 delete failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e

    def exists(self, s3_path: str) -> bool:
        """Whether an object is at ``s3_path`` (U7 BR2.2, BR2.8).

        Raises:
            S3ServiceError: The check itself failed (not a missing object).
        """
        s3_key = self._extract_key_from_path(s3_path)
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code in ("404", "NoSuchKey", "NotFound"):
                return False
            logger.error("Failed to check an S3 object: %s", error_code)
            raise S3ServiceError(
                f"s3 head failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
                failure=classify_aws_error(error_code),
            ) from e
        except BotoCoreError as e:
            logger.error("S3 head unreachable: %s", type(e).__name__)
            raise S3ServiceError(
                f"s3 head unreachable ({type(e).__name__})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e

    # --- U5: keys, tags and copies (BR5.2, BR5.3, BR6.1) ---------------------------

    def path_of(self, s3_key: str) -> str:
        """The ``s3://bucket/key`` path of a key in this bucket."""
        return f"s3://{self.bucket_name}/{s3_key}"

    def key_of(self, s3_path: str) -> str | None:
        """The key of an ``s3://`` path in *this* bucket, or ``None``."""
        prefix = f"s3://{self.bucket_name}/"
        if not s3_path or not s3_path.startswith(prefix):
            return None
        return s3_path[len(prefix) :] or None

    def delete_object_tagging(self, s3_key: str) -> None:
        """Remove every tag of an object (idempotent).

        Raises:
            S3ServiceError: The call failed (``S3_OPERATION_FAILED``).
        """
        self._object_call("delete_object_tagging", s3_key)

    def put_object_tags(self, s3_key: str, tags: Mapping[str, str]) -> None:
        """Replace the tags of an object.

        Raises:
            S3ServiceError: The call failed (``S3_OPERATION_FAILED``).
        """
        self._object_call(
            "put_object_tagging",
            s3_key,
            Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]},
        )

    def get_object_tags(self, s3_key: str) -> dict[str, str]:
        """The tags of an object.

        Raises:
            S3ServiceError: The call failed (``S3_OPERATION_FAILED``).
        """
        response = self._object_call("get_object_tagging", s3_key)
        return {t["Key"]: t["Value"] for t in response.get("TagSet", [])}

    def copy_object(self, source_key: str, dest_key: str) -> str:
        """Copy an object inside the bucket, without tags.

        ``TaggingDirective="REPLACE"`` with no tags means the copy needs no
        tagging permission on the destination (infrastructure-specification.md).

        Returns:
            The ``s3://`` path of the copy.

        Raises:
            S3ServiceError: The copy failed.
        """
        self._object_call(
            "copy_object",
            dest_key,
            CopySource={"Bucket": self.bucket_name, "Key": source_key},
            TaggingDirective="REPLACE",
        )
        return self.path_of(dest_key)

    # --- Advanced generation U1: multipart uploads and head reads ------------------

    def create_multipart_upload(self, s3_key: str, content_type: str) -> str:
        """Start a multipart upload at ``s3_key`` (BR2.3).

        Returns:
            The upload ID.

        Raises:
            S3ServiceError: ``S3_OPERATION_FAILED``.
        """
        response = self._object_call(
            "create_multipart_upload", s3_key, ContentType=content_type
        )
        return str(response["UploadId"])

    def generate_upload_part_urls(
        self,
        s3_path: str,
        upload_id: str,
        part_numbers: Sequence[int],
        expiration: int = 3600,
    ) -> list[PartUrl]:
        """Presigned ``UploadPart`` URLs for the given part numbers (BR2.3, BR2.4).

        Only ``Bucket``, ``Key``, ``UploadId`` and ``PartNumber`` are signed;
        no Content-MD5 or checksum header is part of the signature, because the
        browser sends none (PoC Q6). A URL writes to this one key only.

        Raises:
            S3ServiceError: ``S3_OPERATION_FAILED``.
        """
        s3_key = self._extract_key_from_path(s3_path)
        urls: list[PartUrl] = []
        try:
            for number in part_numbers:
                url = self.s3_client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": self.bucket_name,
                        "Key": s3_key,
                        "UploadId": upload_id,
                        "PartNumber": int(number),
                    },
                    ExpiresIn=expiration,
                )
                urls.append(PartUrl(part_number=int(number), url=str(url)))
        except (ClientError, BotoCoreError) as e:
            logger.warning("S3 part URL signing failed: %s", type(e).__name__)
            raise S3ServiceError(
                "part url generation failed", code=ErrorCode.S3_OPERATION_FAILED
            ) from e
        # The URLs themselves are never logged (security-design.md §3).
        logger.info("Generated %d part URLs", len(urls))
        return urls

    def list_parts(self, s3_key: str, upload_id: str) -> list[UploadedPart]:
        """Every part S3 holds for the upload, in part-number order (BR2.7).

        Raises:
            S3ServiceError: ``S3_OBJECT_NOT_FOUND`` (no such upload) or
                ``S3_OPERATION_FAILED``.
        """
        parts: list[UploadedPart] = []
        marker = 0
        while True:
            response = self._object_call(
                "list_parts",
                s3_key,
                UploadId=upload_id,
                MaxParts=_LIST_PARTS_PAGE,
                PartNumberMarker=marker,
            )
            for part in response.get("Parts", []):
                parts.append(
                    UploadedPart(
                        part_number=int(part["PartNumber"]),
                        size_bytes=int(part["Size"]),
                        etag=str(part["ETag"]),
                    )
                )
            if not response.get("IsTruncated"):
                return parts
            marker = int(response["NextPartNumberMarker"])

    def complete_multipart_upload(
        self, s3_key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> None:
        """Join the parts ``[(part_number, etag)]`` into the object.

        Raises:
            S3ServiceError: ``S3_OBJECT_NOT_FOUND`` (no such upload) or
                ``S3_OPERATION_FAILED`` (including a wrong part list).
        """
        self._object_call(
            "complete_multipart_upload",
            s3_key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": int(number), "ETag": etag}
                    for number, etag in sorted(parts)
                ]
            },
        )

    def abort_multipart_upload(self, s3_key: str, upload_id: str) -> bool:
        """Drop an upload and its parts (BR2.5, BR2.7). Idempotent.

        Returns:
            ``False`` when the upload was already gone.

        Raises:
            S3ServiceError: ``S3_OPERATION_FAILED``.
        """
        try:
            self._object_call("abort_multipart_upload", s3_key, UploadId=upload_id)
        except S3ServiceError as e:
            if e.code is ErrorCode.S3_OBJECT_NOT_FOUND:
                return False
            raise
        return True

    def read_range(self, s3_key: str, length: int) -> bytes:
        """The first ``length`` bytes of an object (the whole object if shorter, BR3.1).

        Raises:
            S3ServiceError: ``S3_OBJECT_NOT_FOUND`` or ``S3_OPERATION_FAILED``.
        """
        response = self._object_call(
            "get_object", s3_key, Range=f"bytes=0-{int(length) - 1}"
        )
        return bytes(response["Body"].read())

    def get_checksum(self, s3_key: str) -> str | None:
        """The whole-object ``ChecksumCRC64NVME`` of an object (BR3.3, contract C4).

        Returns:
            The checksum, or ``None`` when S3 holds none for the object.

        Raises:
            S3ServiceError: ``S3_OBJECT_NOT_FOUND`` or ``S3_OPERATION_FAILED``.
        """
        response = self._object_call("head_object", s3_key, ChecksumMode="ENABLED")
        checksum = response.get("ChecksumCRC64NVME")
        return str(checksum) if checksum else None

    # --- Advanced generation U2: deletion of every version and refresh ------------

    def delete_all_versions(self, s3_key: str) -> int:
        """Delete every version and delete marker of exactly ``s3_key`` (BR14.1).

        The listing is by prefix, so only entries whose key equals ``s3_key``
        are deleted (``<key>-x`` stays). Deletions go 1,000 per request.

        Returns:
            How many versions and markers were deleted (0 when none).

        Raises:
            S3ServiceError: ``S3_OPERATION_FAILED`` when the listing failed or
                the answer reports any ``Errors``.
        """
        targets: list[dict[str, str]] = []
        markers: dict[str, str] = {}
        while True:
            response = self._bucket_call(
                "list_object_versions", Prefix=s3_key, **markers
            )
            for entry in response.get("Versions", []) + response.get(
                "DeleteMarkers", []
            ):
                if entry.get("Key") == s3_key:
                    targets.append(
                        {"Key": s3_key, "VersionId": str(entry["VersionId"])}
                    )
            if not response.get("IsTruncated"):
                break
            markers = {
                "KeyMarker": str(response.get("NextKeyMarker", "")),
                "VersionIdMarker": str(response.get("NextVersionIdMarker", "")),
            }
        for start in range(0, len(targets), _DELETE_BATCH):
            batch = targets[start : start + _DELETE_BATCH]
            answer = self._bucket_call(
                "delete_objects", Delete={"Objects": batch, "Quiet": True}
            )
            if answer.get("Errors"):
                codes = sorted({str(e.get("Code", "")) for e in answer["Errors"]})
                logger.warning("S3 delete_objects reported errors: %s", codes)
                raise S3ServiceError(
                    "s3 delete_objects reported errors",
                    code=ErrorCode.S3_OPERATION_FAILED,
                )
        return len(targets)

    def refresh_object(self, s3_key: str) -> str | None:
        """Copy an object onto its own key so its creation date is new (BR13.4).

        The copy replaces the metadata (keeping the content type), keeps
        SSE-S3, asks S3 for a new ``CRC64NVME`` checksum and writes no tags
        (no tagging permission is needed, NFR3.19). The bytes never pass
        through the application.

        Returns:
            The checksum of the copy, or ``None`` when S3 returned none.

        Raises:
            S3ServiceError: ``S3_OBJECT_NOT_FOUND`` or ``S3_OPERATION_FAILED``.
        """
        head = self._object_call("head_object", s3_key)
        response = self._object_call(
            "copy_object",
            s3_key,
            CopySource={"Bucket": self.bucket_name, "Key": s3_key},
            MetadataDirective="REPLACE",
            ContentType=str(head.get("ContentType") or "application/octet-stream"),
            ServerSideEncryption="AES256",
            ChecksumAlgorithm="CRC64NVME",
            TaggingDirective="REPLACE",
        )
        checksum = (response.get("CopyObjectResult") or {}).get("ChecksumCRC64NVME")
        return str(checksum) if checksum else None

    def _bucket_call(self, operation: str, **kwargs: Any) -> Any:
        """A bucket-level call; failures are ``S3_OPERATION_FAILED``."""
        try:
            return getattr(self.s3_client, operation)(Bucket=self.bucket_name, **kwargs)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.warning("S3 %s failed: %s", operation, error_code)
            raise S3ServiceError(
                f"s3 {operation} failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e
        except BotoCoreError as e:
            logger.warning("S3 %s unreachable: %s", operation, type(e).__name__)
            raise S3ServiceError(
                f"s3 {operation} unreachable ({type(e).__name__})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e

    def put_new_object(
        self,
        s3_key: str,
        body: bytes,
        content_type: str,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        """Write an object only when nothing is at the key (``IfNoneMatch: *``).

        Used for the analysis record, written with ``pending=true`` until a
        persona is saved (BR9.1).

        Raises:
            S3ObjectExistsError: An object is already at the key.
            S3ServiceError: ``S3_OPERATION_FAILED``.
        """
        kwargs: dict[str, Any] = {
            "Body": body,
            "ContentType": content_type,
            "IfNoneMatch": "*",
        }
        if tags:
            kwargs["Tagging"] = urlencode(dict(tags))
        try:
            self.s3_client.put_object(Bucket=self.bucket_name, Key=s3_key, **kwargs)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code in ("PreconditionFailed", "412"):
                raise S3ObjectExistsError("object already exists") from e
            logger.warning("S3 put_object (new) failed: %s", error_code)
            raise S3ServiceError(
                f"s3 put_object failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
                failure=classify_aws_error(error_code),
            ) from e
        except BotoCoreError as e:
            logger.warning("S3 put_object (new) unreachable: %s", type(e).__name__)
            raise S3ServiceError(
                f"s3 put_object unreachable ({type(e).__name__})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e

    def _object_call(self, operation: str, s3_key: str, **kwargs: Any) -> Any:
        try:
            return getattr(self.s3_client, operation)(
                Bucket=self.bucket_name, Key=s3_key, **kwargs
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.warning("S3 %s failed: %s", operation, error_code)
            if error_code in _NOT_FOUND_CODES:
                raise S3ServiceError(
                    f"s3 {operation}: not found ({error_code})",
                    code=ErrorCode.S3_OBJECT_NOT_FOUND,
                ) from e
            raise S3ServiceError(
                f"s3 {operation} failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e
        except BotoCoreError as e:
            logger.warning("S3 %s unreachable: %s", operation, type(e).__name__)
            raise S3ServiceError(
                f"s3 {operation} unreachable ({type(e).__name__})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e

    def _extract_key_from_path(self, s3_path: str) -> str:
        """
        S3パスからオブジェクトキーを抽出

        Args:
            s3_path: S3パス（例: "s3://bucket-name/uploads/uuid_filename.txt"）

        Returns:
            S3オブジェクトキー（例: "uploads/uuid_filename.txt"）
        """
        # "s3://bucket-name/" の部分を削除
        if s3_path.startswith("s3://"):
            # "s3://" を削除
            path_without_protocol = s3_path[5:]
            # 最初の "/" までがバケット名、それ以降がキー
            parts = path_without_protocol.split("/", 1)
            if len(parts) == 2:
                return parts[1]
        raise S3ServiceError(
            "invalid s3 path format",
            code=ErrorCode.S3_OPERATION_FAILED,
        )

    def generate_presigned_url(self, s3_path: str, expiration: int = 3600) -> str:
        """
        S3オブジェクトの署名付きURLを生成

        Args:
            s3_path: S3パス（例: "s3://bucket-name/discussion_documents/uuid_filename.png"）
            expiration: URL有効期限（秒）、デフォルト1時間

        Returns:
            署名付きURL

        Raises:
            Exception: URL生成に失敗した場合
        """
        try:
            s3_key = self._extract_key_from_path(s3_path)
            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": s3_key},
                ExpiresIn=expiration,
            )
            logger.info(f"Generated presigned URL for {s3_path}")
            return str(url)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(f"Failed to generate presigned URL: {error_code} - {str(e)}")
            raise S3ServiceError(
                f"presigned url generation failed ({error_code})",
                code=ErrorCode.S3_OPERATION_FAILED,
            ) from e
