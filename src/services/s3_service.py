"""
S3サービス
Amazon S3へのファイルアップロード・ダウンロード・削除を管理
"""

import logging

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)


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

    def upload_file(self, file_content: bytes, s3_key: str) -> str:
        """
        S3にファイルをアップロード

        Args:
            file_content: アップロードするファイルの内容（バイト列）
            s3_key: S3オブジェクトキー（例: "uploads/uuid_filename.txt"）

        Returns:
            S3パス（例: "s3://bucket-name/uploads/uuid_filename.txt"）

        Raises:
            Exception: アップロードに失敗した場合
        """
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=file_content,
            )
            s3_path = f"s3://{self.bucket_name}/{s3_key}"
            logger.info(f"File uploaded successfully to {s3_path}")
            return s3_path
        except NoCredentialsError:
            logger.error("AWS credentials not found")
            raise Exception("AWS認証情報が見つかりません")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(f"Failed to upload file to S3: {error_code} - {str(e)}")
            raise Exception(f"S3へのアップロードに失敗しました: {error_code}")

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
            return file_content
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "NoSuchKey":
                logger.error(f"File not found in S3: {s3_path}")
                raise Exception(f"ファイルが見つかりません: {s3_path}")
            logger.error(f"Failed to download file from S3: {error_code} - {str(e)}")
            raise Exception(f"S3からのダウンロードに失敗しました: {error_code}")

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
            raise Exception(f"S3からの削除に失敗しました: {error_code}")

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
        raise ValueError(f"Invalid S3 path format: {s3_path}")

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
            return url
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(f"Failed to generate presigned URL: {error_code} - {str(e)}")
            raise Exception(f"署名付きURL生成に失敗しました: {error_code}")
