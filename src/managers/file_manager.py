"""
ファイル管理マネージャー
ファイルアップロード、検証、保存機能を提供
"""

import uuid
import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, TYPE_CHECKING
from datetime import datetime

from ..config import config
from ..models.errors import CodedError, ErrorCode
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory

if TYPE_CHECKING:
    from ..services.s3_service import S3Service


class FileUploadError(CodedError):
    """ファイルアップロード関連のエラー。

    アップロード時のバリデーション失敗と、ファイル操作そのものの失敗の
    両方を表すため、code は raise 箇所ごとに指定する。
    """


class FileSecurityError(CodedError):
    """ファイルセキュリティ関連のエラー"""


class FileMetadata:
    """ファイルメタデータクラス"""

    def __init__(
        self,
        file_id: str,
        original_filename: str,
        saved_filename: str,
        file_path: str,
        file_size: int,
        file_hash: str,
        mime_type: str,
        uploaded_at: datetime,
        file_type: str = "persona_interview",
    ):
        self.file_id = file_id
        self.original_filename = original_filename
        self.saved_filename = saved_filename
        self.file_path = file_path
        self.file_size = file_size
        self.file_hash = file_hash
        self.mime_type = mime_type
        self.uploaded_at = uploaded_at
        self.file_type = file_type

    def to_dict(self) -> Dict[str, Any]:
        """辞書形式に変換"""
        return {
            "file_id": self.file_id,
            "original_filename": self.original_filename,
            "saved_filename": self.saved_filename,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "file_hash": self.file_hash,
            "mime_type": self.mime_type,
            "uploaded_at": self.uploaded_at.isoformat(),
        }


class FileManager:
    """ファイル管理クラス"""

    # 議論用ドキュメントの許可形式
    DISCUSSION_DOCUMENT_FORMATS = {".png", ".jpg", ".jpeg", ".pdf"}
    DISCUSSION_DOCUMENT_MAX_SIZE = 10 * 1024 * 1024  # 10MB per file（アプリ運用上限）
    # 画像1枚の上限はClaude(Bedrock)モデルの制約。configを single source of truth とする
    DISCUSSION_IMAGE_MAX_SIZE = config.MAX_IMAGE_SIZE  # 5MB per image
    # リクエストペイロード全体の上限（PDF含む全コンテンツ合算）。configを single source of truth とする
    DISCUSSION_DOCUMENT_TOTAL_MAX_SIZE = config.MAX_REQUEST_PAYLOAD_SIZE  # 32MB total
    DISCUSSION_IMAGE_MIMES = {"image/png", "image/jpeg"}

    # 知識ファイルの許可形式
    KNOWLEDGE_FILE_FORMATS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md"}
    KNOWLEDGE_FILE_MAX_SIZE = 10 * 1024 * 1024  # 10MB

    # ペルソナ生成ソースの許可形式（バイナリ形式を含むため validate_file_format は流用不可）
    PERSONA_SOURCE_FORMATS = {".txt", ".md", ".pdf", ".docx", ".doc", ".csv"}

    # アンケート画像の許可形式
    SURVEY_IMAGE_FORMATS = {".png", ".jpg", ".jpeg"}
    # 画像1枚の上限はClaude(Bedrock)モデルの制約。configを single source of truth とする
    SURVEY_IMAGE_MAX_SIZE = config.MAX_IMAGE_SIZE  # 5MB per file

    def __init__(
        self,
        db_service: Optional[DatabaseService] = None,
        s3_service: Optional["S3Service"] = None,
    ):
        """ファイルマネージャーの初期化"""
        self.upload_dir = config.upload_dir
        self.discussion_doc_dir = Path("discussion_documents")
        self.knowledge_files_dir = Path("knowledge_files")
        self.survey_images_dir = Path("survey_images")
        if s3_service is not None:
            self.s3_service = s3_service
        else:
            try:
                self.s3_service = service_factory.get_s3_service()
            except RuntimeError:
                self.s3_service = None  # type: ignore[assignment]

        # Use singleton database service if not provided
        self.db_service = db_service or service_factory.get_database_service()

        # アップロードディレクトリの作成（ローカルストレージ使用時のみ）
        if not self.s3_service:
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            self.discussion_doc_dir.mkdir(parents=True, exist_ok=True)
            self.knowledge_files_dir.mkdir(parents=True, exist_ok=True)
            self.survey_images_dir.mkdir(parents=True, exist_ok=True)

    def validate_persona_source_file(self, filename: str, file_content: bytes) -> None:
        """Validate a persona-generation source file before text extraction.

        Binary formats (PDF/DOCX) are accepted here, so validate_file_format
        cannot be reused (it requires the payload to be text-decodable). Content
        sufficiency and total size are enforced after extraction in
        PersonaGenerationManager, since byte length says nothing about the text
        yielded by a binary document.

        Raises:
            FileUploadError: format / size / empty violations.
        """
        ext = Path(filename).suffix.lower()
        if ext not in self.PERSONA_SOURCE_FORMATS:
            raise FileUploadError(
                f"extension of {filename!r} not in persona source formats",
                code=ErrorCode.FILE_FORMAT_NOT_ALLOWED,
                context={
                    "allowed_formats": ", ".join(sorted(self.PERSONA_SOURCE_FORMATS))
                },
            )

        if len(file_content) > config.PERSONA_SOURCE_MAX_BYTES:
            raise FileUploadError(
                f"file size {len(file_content)} exceeds limit "
                f"{config.PERSONA_SOURCE_MAX_BYTES}",
                code=ErrorCode.FILE_TOO_LARGE,
                context={
                    "max_size_mb": config.PERSONA_SOURCE_MAX_BYTES / (1024 * 1024)
                },
            )

        if len(file_content) == 0:
            raise FileUploadError("file is empty", code=ErrorCode.FILE_EMPTY)

    def _validate_discussion_document(self, filename: str, file_content: bytes) -> bool:
        """
        議論用ドキュメントの形式を検証する

        Args:
            filename: ファイル名
            file_content: ファイル内容（バイト）

        Returns:
            bool: 検証結果（True: 有効）

        Raises:
            FileUploadError: ファイル形式が無効な場合
        """
        # ファイル拡張子チェック
        file_ext = Path(filename).suffix.lower()
        if file_ext not in self.DISCUSSION_DOCUMENT_FORMATS:
            raise FileUploadError(
                f"extension {file_ext!r} not in discussion document formats",
                code=ErrorCode.FILE_FORMAT_NOT_ALLOWED,
                context={
                    "allowed_formats": ", ".join(self.DISCUSSION_DOCUMENT_FORMATS)
                },
            )

        # MIMEタイプチェック（簡易）
        mime_type = mimetypes.guess_type(filename)[0]
        allowed_mimes = {"image/png", "image/jpeg", "application/pdf"}
        if mime_type not in allowed_mimes:
            raise FileUploadError(
                f"mime type {mime_type!r} not allowed for discussion documents",
                code=ErrorCode.FILE_MIME_UNSUPPORTED,
            )

        # ファイルサイズチェック（画像はBedrockの上限に合わせて5MB）
        is_image = mime_type in self.DISCUSSION_IMAGE_MIMES
        size_limit = (
            self.DISCUSSION_IMAGE_MAX_SIZE
            if is_image
            else self.DISCUSSION_DOCUMENT_MAX_SIZE
        )
        if len(file_content) > size_limit:
            raise FileUploadError(
                f"file size {len(file_content)} exceeds limit {size_limit}",
                code=ErrorCode.FILE_TOO_LARGE,
                context={"max_size_mb": size_limit / (1024 * 1024)},
            )

        # ファイル内容が空でないかチェック
        if len(file_content) == 0:
            raise FileUploadError("file is empty", code=ErrorCode.FILE_EMPTY)

        return True

    def convert_file_to_markdown(self, file_content: bytes, filename: str) -> str:
        """
        ファイルをマークダウンに変換する

        Args:
            file_content: ファイル内容（バイト）
            filename: ファイル名

        Returns:
            str: マークダウン化されたテキスト

        Raises:
            FileUploadError: 変換に失敗した場合
        """
        from markitdown import MarkItDown
        import io

        # ファイル拡張子チェック
        file_ext = Path(filename).suffix.lower()
        if file_ext not in self.KNOWLEDGE_FILE_FORMATS:
            raise FileUploadError(
                f"extension {file_ext!r} not in knowledge file formats",
                code=ErrorCode.FILE_FORMAT_NOT_ALLOWED,
                context={"allowed_formats": ", ".join(self.KNOWLEDGE_FILE_FORMATS)},
            )

        try:
            md = MarkItDown()
            # BytesIOオブジェクトを作成してファイル名を設定
            file_stream = io.BytesIO(file_content)
            file_stream.name = filename

            # マークダウンに変換
            result = md.convert_stream(file_stream)
            return result.text_content

        except Exception as e:
            raise FileUploadError(
                f"markdown conversion failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def upload_discussion_document(
        self, file_content: bytes, filename: str
    ) -> FileMetadata:
        """
        議論用ドキュメントをアップロードする

        Args:
            file_content: ファイル内容（バイト）
            filename: 元のファイル名

        Returns:
            FileMetadata: ファイルメタデータ

        Raises:
            FileUploadError: アップロード処理でエラーが発生した場合
        """
        try:
            # セキュリティチェック（バイナリファイルを許可）
            self._security_check(filename, file_content, allow_binary=True)

            # 議論用ドキュメント形式検証
            self._validate_discussion_document(filename, file_content)

            # ファイルメタデータを生成
            file_metadata = self._create_file_metadata(
                filename, file_content, file_type="discussion_document"
            )

            # ファイルを安全に保存
            saved_file_path = self._save_file_securely(
                file_content,
                file_metadata.saved_filename,
                file_type="discussion_document",
            )
            file_metadata.file_path = saved_file_path

            # メタデータをデータベースに保存
            self._save_file_metadata(file_metadata, file_type="discussion_document")

            return file_metadata

        except (FileUploadError, FileSecurityError):
            raise
        except Exception as e:
            raise FileUploadError(
                f"discussion document upload failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def upload_survey_image(self, file_content: bytes, filename: str) -> FileMetadata:
        """
        アンケート用画像をアップロードする

        Args:
            file_content: ファイル内容（バイト）
            filename: 元のファイル名

        Returns:
            FileMetadata: ファイルメタデータ

        Raises:
            FileUploadError: アップロード処理でエラーが発生した場合
        """
        try:
            self._security_check(filename, file_content, allow_binary=True)
            # 形式・サイズ検証
            file_ext = Path(filename).suffix.lower()
            if file_ext not in self.SURVEY_IMAGE_FORMATS:
                raise FileUploadError(
                    f"extension {file_ext!r} not in survey image formats",
                    code=ErrorCode.FILE_FORMAT_NOT_ALLOWED,
                    context={"allowed_formats": ", ".join(self.SURVEY_IMAGE_FORMATS)},
                )
            if len(file_content) > self.SURVEY_IMAGE_MAX_SIZE:
                raise FileUploadError(
                    f"file size {len(file_content)} exceeds limit "
                    f"{self.SURVEY_IMAGE_MAX_SIZE}",
                    code=ErrorCode.FILE_TOO_LARGE,
                    context={"max_size_mb": self.SURVEY_IMAGE_MAX_SIZE / (1024 * 1024)},
                )
            if len(file_content) == 0:
                raise FileUploadError("file is empty", code=ErrorCode.FILE_EMPTY)

            file_metadata = self._create_file_metadata(
                filename, file_content, file_type="survey_image"
            )
            saved_file_path = self._save_file_securely(
                file_content, file_metadata.saved_filename, file_type="survey_image"
            )
            file_metadata.file_path = saved_file_path
            self._save_file_metadata(file_metadata, file_type="survey_image")
            return file_metadata
        except (FileUploadError, FileSecurityError):
            raise
        except Exception as e:
            raise FileUploadError(
                f"survey image upload failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def upload_knowledge_file(
        self, file_content: bytes, filename: str
    ) -> Tuple[FileMetadata, str]:
        """
        知識ファイルをアップロードしてマークダウンに変換する

        Args:
            file_content: ファイル内容（バイト）
            filename: 元のファイル名

        Returns:
            Tuple[FileMetadata, str]: (ファイルメタデータ, マークダウン化した内容)

        Raises:
            FileUploadError: アップロード処理でエラーが発生した場合
        """
        try:
            # セキュリティチェック（バイナリファイルを許可）
            self._security_check(filename, file_content, allow_binary=True)

            # ファイルサイズチェック
            if len(file_content) > self.KNOWLEDGE_FILE_MAX_SIZE:
                max_size_mb = self.KNOWLEDGE_FILE_MAX_SIZE / (1024 * 1024)
                raise FileUploadError(
                    f"file size {len(file_content)} exceeds limit "
                    f"{self.KNOWLEDGE_FILE_MAX_SIZE}",
                    code=ErrorCode.FILE_TOO_LARGE,
                    context={"max_size_mb": max_size_mb},
                )

            # ファイル内容が空でないかチェック
            if len(file_content) == 0:
                raise FileUploadError("file is empty", code=ErrorCode.FILE_EMPTY)

            # マークダウンに変換
            markdown_content = self.convert_file_to_markdown(file_content, filename)

            # ファイルメタデータを生成
            file_metadata = self._create_file_metadata(
                filename, file_content, file_type="knowledge_file"
            )

            # ファイルを安全に保存
            saved_file_path = self._save_file_securely(
                file_content, file_metadata.saved_filename, file_type="knowledge_file"
            )
            file_metadata.file_path = saved_file_path

            # メタデータをデータベースに保存
            self._save_file_metadata(file_metadata, file_type="knowledge_file")

            return file_metadata, markdown_content

        except (FileUploadError, FileSecurityError):
            raise
        except Exception as e:
            raise FileUploadError(
                f"knowledge file upload failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    # プライベートメソッド

    def _security_check(
        self, filename: str, file_content: bytes, allow_binary: bool = False
    ) -> None:
        """
        ファイルのセキュリティチェックを実行する

        Args:
            filename: ファイル名
            file_content: ファイル内容
            allow_binary: バイナリファイルを許可するかどうか

        Raises:
            FileSecurityError: セキュリティ上の問題がある場合
        """
        # ファイル名の安全性チェック
        if ".." in filename or "/" in filename or "\\" in filename:
            raise FileSecurityError(
                "filename contains a path traversal or invalid character",
                code=ErrorCode.FILE_NAME_INVALID,
            )

        # ファイル名の長さチェック
        if len(filename) > 255:
            raise FileSecurityError(
                f"filename length {len(filename)} exceeds 255",
                code=ErrorCode.FILE_NAME_TOO_LONG,
            )

        # 隠しファイルのチェック
        if filename.startswith("."):
            raise FileSecurityError(
                "filename starts with a dot", code=ErrorCode.FILE_HIDDEN_NOT_ALLOWED
            )

        # バイナリファイルの簡易チェック（NULL文字の存在）
        # allow_binaryがTrueの場合はスキップ
        if not allow_binary and b"\x00" in file_content[:1024]:  # 最初の1KBをチェック
            raise FileSecurityError(
                "NUL byte found in the first 1KB",
                code=ErrorCode.FILE_BINARY_NOT_ALLOWED,
            )

    def _create_file_metadata(
        self,
        original_filename: str,
        file_content: bytes,
        file_type: str = "persona_interview",
    ) -> FileMetadata:
        """
        ファイルメタデータを作成する

        Args:
            original_filename: 元のファイル名
            file_content: ファイル内容
            file_type: ファイルタイプ ('persona_interview' or 'discussion_document')

        Returns:
            FileMetadata: ファイルメタデータ
        """
        file_id = str(uuid.uuid4())
        saved_filename = f"{file_id}_{original_filename}"
        file_size = len(file_content)
        file_hash = hashlib.sha256(file_content).hexdigest()
        mime_type = mimetypes.guess_type(original_filename)[0] or "text/plain"
        uploaded_at = datetime.now()

        metadata = FileMetadata(
            file_id=file_id,
            original_filename=original_filename,
            saved_filename=saved_filename,
            file_path="",  # 後で設定
            file_size=file_size,
            file_hash=file_hash,
            mime_type=mime_type,
            uploaded_at=uploaded_at,
            file_type=file_type,
        )
        return metadata

    def _save_file_securely(
        self, file_content: bytes, filename: str, file_type: str = "persona_interview"
    ) -> str:
        """
        ファイルを安全に保存する

        Args:
            file_content: ファイル内容
            filename: 保存するファイル名
            file_type: ファイルタイプ ('persona_interview', 'discussion_document', 'knowledge_file')

        Returns:
            str: 保存されたファイルのパス（ローカルパスまたはS3パス）
        """
        # S3を使用する場合
        if self.s3_service:
            if file_type == "discussion_document":
                s3_key = f"discussion_documents/{filename}"
            elif file_type == "knowledge_file":
                s3_key = f"knowledge_files/{filename}"
            elif file_type == "survey_image":
                s3_key = f"survey_images/{filename}"
            else:
                s3_key = f"uploads/{filename}"
            return self.s3_service.upload_file(file_content, s3_key)

        # ローカルストレージを使用する場合
        if file_type == "discussion_document":
            file_path = self.discussion_doc_dir / filename
        elif file_type == "knowledge_file":
            file_path = self.knowledge_files_dir / filename
        elif file_type == "survey_image":
            file_path = self.survey_images_dir / filename
        else:
            file_path = self.upload_dir / filename

        # 一時ファイルに書き込み後、アトミックに移動
        temp_path = file_path.with_suffix(file_path.suffix + ".tmp")

        try:
            with open(temp_path, "wb") as f:
                f.write(file_content)

            # アトミックな移動
            temp_path.rename(file_path)

            # ファイル権限を設定（読み取り専用）
            file_path.chmod(0o644)

            return str(file_path.absolute())

        except Exception as e:
            # 一時ファイルが残っている場合は削除
            if temp_path.exists():
                temp_path.unlink()
            raise FileUploadError(
                f"file save failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def _save_file_metadata(
        self, metadata: FileMetadata, file_type: str = "persona_interview"
    ) -> None:
        """
        ファイルメタデータをデータベースに保存する

        Args:
            metadata: ファイルメタデータ
            file_type: ファイルタイプ ('persona_interview' or 'discussion_document')
        """
        try:
            self.db_service.save_uploaded_file_info(
                file_id=metadata.file_id,
                filename=metadata.saved_filename,
                file_path=metadata.file_path,
                file_size=metadata.file_size,
                file_hash=metadata.file_hash,
                mime_type=metadata.mime_type,
                uploaded_at=metadata.uploaded_at,
                original_filename=metadata.original_filename,
                file_type=file_type,
            )
        except Exception as e:
            raise DatabaseError(
                f"file metadata save failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e
