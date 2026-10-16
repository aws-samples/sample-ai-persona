"""
S3サービス
Amazon S3へのファイルアップロード・ダウンロード・削除を管理
"""

import logging
from typing import Any, Mapping
from urllib.parse import urlencode

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from ..models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)


class S3ServiceError(CodedError):
    """S3サービス関連のエラー"""


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
        self.s3_client = boto3.client("s3", region_name=region_name)
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
            ) from e
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(f"Failed to upload file to S3: {error_code} - {str(e)}")
            raise S3ServiceError(
                f"s3 upload failed ({error_code})",
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

    def _object_call(self, operation: str, s3_key: str, **kwargs: Any) -> Any:
        try:
            return getattr(self.s3_client, operation)(
                Bucket=self.bucket_name, Key=s3_key, **kwargs
            )
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
