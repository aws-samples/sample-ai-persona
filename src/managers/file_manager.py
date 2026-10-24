"""
ファイル管理マネージャー
ファイルアップロード、検証、保存機能を提供

v1.0.0 U5（BR5.1〜BR5.9）:

- アップロード（議論の資料・テンプレートの画像）は利用者を受け取り、S3 のキーを
  ``uploads/{owner_sub}/{file_type}/{file_id}_{元の名前}`` にして ``pending=true`` の
  タグを付けて置き、メタデータ（``UploadedFiles``）に ``owner_sub`` と 24 時間の
  ``expires_at`` を書く
- 親への紐付けと配信の判定は Component ``FileAttachments`` が持つ
- 記憶への取り込みのファイルは保存しない（``convert_file_to_markdown`` が確認と変換だけ）
- ``preview_url_for_path`` は形・メタデータの一致・``can_read_file`` を満たすパスにだけ
  配信 URL を出す（任意の S3 パスを受け付けない、BR5.8）
"""

import hashlib
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from ..config import config
from ..models.authz import Permission, Principal
from ..models.errors import CodedError, ErrorCode
from ..models.uploaded_file import PENDING_TAG, UploadedFile, file_id_from_key
from ..services.service_factory import service_factory
from .components.access_policy import AccessPolicy
from .components.file_attachments import FileAttachments
from .components.sharing_runtime import build_access_parts, shared_access_parts

if TYPE_CHECKING:
    from ..services.s3_service import S3Service
    from ..services.uploaded_file_store import UploadedFileStore

logger = logging.getLogger(__name__)

#: Lifetime of the presigned URL of a template image preview
PREVIEW_URL_EXPIRATION_SECONDS = 3600


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

    # ペルソナ生成ソースの許可形式（PDF/DOCX 等のバイナリ形式を含む）
    PERSONA_SOURCE_FORMATS = {".txt", ".md", ".pdf", ".docx", ".doc", ".csv"}

    # アンケート画像の許可形式
    SURVEY_IMAGE_FORMATS = {".png", ".jpg", ".jpeg"}
    # 画像1枚の上限はClaude(Bedrock)モデルの制約。configを single source of truth とする
    SURVEY_IMAGE_MAX_SIZE = config.MAX_IMAGE_SIZE  # 5MB per file

    def __init__(
        self,
        s3_service: Optional["S3Service"] = None,
        file_store: Optional["UploadedFileStore"] = None,
        attachments: Optional[FileAttachments] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        """ファイルマネージャーの初期化

        Args:
            s3_service: S3（未設定ならローカルに保存する）
            file_store: Upload metadata (optional, built on first use)
            attachments: The per-task ``FileAttachments`` (optional)
            clock: Aware-UTC clock (tests)
        """
        self.upload_dir = config.upload_dir
        self.discussion_doc_dir = Path("discussion_documents")
        self.survey_images_dir = Path("survey_images")
        if s3_service is not None:
            self.s3_service = s3_service
        else:
            try:
                self.s3_service = service_factory.get_s3_service()
            except RuntimeError:
                self.s3_service = None  # type: ignore[assignment]

        self._file_store = file_store
        self._attachments = attachments
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        # アップロードディレクトリの作成（ローカルストレージ使用時のみ）
        if not self.s3_service:
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            self.discussion_doc_dir.mkdir(parents=True, exist_ok=True)
            self.survey_images_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _store(self) -> "UploadedFileStore":
        if self._file_store is None:
            self._file_store = service_factory.get_uploaded_file_store()
        return self._file_store

    @property
    def _file_attachments(self) -> FileAttachments:
        if self._attachments is None:
            self._attachments = shared_access_parts(
                lambda: build_access_parts(
                    service_factory.get_authz_lookup_service(),
                    service_factory.get_uploaded_file_store(),
                    service_factory.get_optional_s3_service(),
                )
            ).files
        return self._attachments

    def validate_persona_source_file(self, filename: str, file_content: bytes) -> None:
        """Validate a persona-generation source file before text extraction.

        Only format / size / empty are checked here, since binary formats
        (PDF/DOCX) are accepted and byte length says nothing about the text a
        binary document yields. Content sufficiency and total character count
        are enforced after extraction in PersonaGenerationManager.

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
        知識ファイルを安全確認のうえマークダウンに変換する（何も保存しない、BR5.9）

        Args:
            file_content: ファイル内容（バイト）
            filename: ファイル名

        Returns:
            str: マークダウン化されたテキスト

        Raises:
            FileSecurityError: ファイル名・内容の安全確認に通らない場合
            FileUploadError: 形式・大きさ・空、または変換に失敗した場合
        """
        from markitdown import MarkItDown
        import io

        self._security_check(filename, file_content, allow_binary=True)

        # ファイル拡張子チェック
        file_ext = Path(filename).suffix.lower()
        if file_ext not in self.KNOWLEDGE_FILE_FORMATS:
            raise FileUploadError(
                f"extension {file_ext!r} not in knowledge file formats",
                code=ErrorCode.FILE_FORMAT_NOT_ALLOWED,
                context={"allowed_formats": ", ".join(self.KNOWLEDGE_FILE_FORMATS)},
            )

        if len(file_content) > self.KNOWLEDGE_FILE_MAX_SIZE:
            raise FileUploadError(
                f"file size {len(file_content)} exceeds limit "
                f"{self.KNOWLEDGE_FILE_MAX_SIZE}",
                code=ErrorCode.FILE_TOO_LARGE,
                context={"max_size_mb": self.KNOWLEDGE_FILE_MAX_SIZE / (1024 * 1024)},
            )
        if len(file_content) == 0:
            raise FileUploadError("file is empty", code=ErrorCode.FILE_EMPTY)

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
        self, principal: Principal, file_content: bytes, filename: str
    ) -> FileMetadata:
        """
        議論用ドキュメントをアップロードする（本人のキー、24 時間の期限、BR5.1、BR5.2）

        Args:
            principal: アップロードする人（resource:create が要る）
            file_content: ファイル内容（バイト）
            filename: 元のファイル名

        Returns:
            FileMetadata: ファイルメタデータ

        Raises:
            AccessDeniedError: resource:create がない
            FileUploadError: アップロード処理でエラーが発生した場合
        """
        AccessPolicy.require(principal, Permission.RESOURCE_CREATE)
        try:
            # セキュリティチェック（バイナリファイルを許可）
            self._security_check(filename, file_content, allow_binary=True)

            # 議論用ドキュメント形式検証
            self._validate_discussion_document(filename, file_content)

            return self._store_upload(
                principal, file_content, filename, file_type="discussion_document"
            )

        except (FileUploadError, FileSecurityError):
            raise
        except Exception as e:
            raise FileUploadError(
                f"discussion document upload failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def upload_interview_attachment(
        self,
        principal: Principal,
        file_content: bytes,
        filename: str,
        mime_type: Optional[str] = None,
    ) -> FileMetadata:
        """
        インタビューの添付をアップロードする（議論の資料と同じ仕組み、U6 BR3.6）

        本人のキー・``pending`` のタグ・24 時間の期限で置き、セッションには ID だけを持つ。
        種類と大きさの検査はインタビューのこれまでの検査（画像は ``MAX_IMAGE_SIZE``、
        それ以外は ``MAX_FILE_SIZE``）のまま。

        Args:
            principal: アップロードする人（resource:create が要る）
            file_content: ファイル内容（バイト）
            filename: 元のファイル名
            mime_type: ブラウザが送った種類（ファイル名から判らないときに使う）

        Returns:
            FileMetadata: ファイルメタデータ

        Raises:
            AccessDeniedError: resource:create がない
            FileUploadError: ``FILE_MIME_UNSUPPORTED``・``FILE_TOO_LARGE``・
                ``FILE_OPERATION_FAILED``
        """
        from .shared.document_loader import is_image_type, is_supported_mime_type

        AccessPolicy.require(principal, Permission.RESOURCE_CREATE)
        kind = mimetypes.guess_type(filename)[0] or mime_type or ""
        if not is_supported_mime_type(kind):
            raise FileUploadError(
                f"mime type {kind!r} not supported for interview attachments",
                code=ErrorCode.FILE_MIME_UNSUPPORTED,
            )
        size_limit = (
            config.MAX_IMAGE_SIZE if is_image_type(kind) else config.MAX_FILE_SIZE
        )
        if len(file_content) > size_limit:
            raise FileUploadError(
                f"attachment size {len(file_content)} exceeds limit {size_limit}",
                code=ErrorCode.FILE_TOO_LARGE,
                context={"max_size_mb": size_limit / (1024 * 1024)},
            )
        try:
            self._security_check(filename, file_content, allow_binary=True)
            return self._store_upload(
                principal,
                file_content,
                filename,
                file_type="discussion_document",
                mime_type=kind,
            )
        except (FileUploadError, FileSecurityError):
            raise
        except Exception as e:
            raise FileUploadError(
                f"interview attachment upload failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def upload_survey_image(
        self, principal: Principal, file_content: bytes, filename: str
    ) -> FileMetadata:
        """
        アンケート用画像をアップロードする（本人のキー、24 時間の期限、BR5.1、BR5.2）

        Args:
            principal: アップロードする人（resource:create が要る）
            file_content: ファイル内容（バイト）
            filename: 元のファイル名

        Returns:
            FileMetadata: ファイルメタデータ

        Raises:
            AccessDeniedError: resource:create がない
            FileUploadError: アップロード処理でエラーが発生した場合
        """
        AccessPolicy.require(principal, Permission.RESOURCE_CREATE)
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

            return self._store_upload(
                principal, file_content, filename, file_type="survey_image"
            )
        except (FileUploadError, FileSecurityError):
            raise
        except Exception as e:
            raise FileUploadError(
                f"survey image upload failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def preview_url_for_path(self, principal: Principal, file_path: str) -> str:
        """A presigned URL of a template image the user may read (BR5.8).

        Only ``s3://{bucket}/uploads/{owner}/survey_image/{file_id}_{name}`` is
        accepted, where ``file_id`` is a UUID whose metadata holds exactly that
        key and ``FileAttachments.can_read_file`` passes. Anything else gets
        ``FILE_NOT_FOUND`` and no URL is made.

        Raises:
            FileUploadError: ``FILE_NOT_FOUND`` (or ``FILE_OPERATION_FAILED``
                when the URL cannot be signed)
        """
        not_found = FileUploadError(
            "preview path is not a readable upload", code=ErrorCode.FILE_NOT_FOUND
        )
        if self.s3_service is None:
            raise not_found
        key = self.s3_service.key_of(file_path or "")
        file_id = file_id_from_key(key or "")
        if not key or not file_id or key.split("/")[2] != "survey_image":
            raise not_found
        try:
            uploaded = self._file_attachments.readable_file(principal, file_id)
        except CodedError as e:
            if e.code is ErrorCode.FILE_NOT_FOUND:
                raise not_found from e
            raise
        if uploaded.s3_key != key:
            raise not_found
        try:
            return self.s3_service.generate_presigned_url(
                file_path, PREVIEW_URL_EXPIRATION_SECONDS
            )
        except Exception as e:
            logger.warning("Preview URL could not be signed: %s", type(e).__name__)
            raise FileUploadError(
                f"preview url failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    # プライベートメソッド

    def _store_upload(
        self,
        principal: Principal,
        file_content: bytes,
        filename: str,
        *,
        file_type: str,
        mime_type: Optional[str] = None,
    ) -> FileMetadata:
        """Place the bytes and write the metadata (owner, 24-hour expiry)."""
        uploaded = UploadedFile.create_new(
            owner_sub=principal.sub,
            file_type=file_type,
            original_filename=filename,
            file_size=len(file_content),
            file_hash=hashlib.sha256(file_content).hexdigest(),
            mime_type=mime_type or mimetypes.guess_type(filename)[0] or "text/plain",
            now=self._clock(),
        )
        path = self._save_file_securely(file_content, uploaded)
        uploaded = uploaded.with_path(path)
        try:
            self._store.put(uploaded)
        except Exception as e:
            logger.error("Failed to write upload metadata", exc_info=True)
            self._remove_object(path)
            raise FileUploadError(
                f"file metadata save failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e
        return FileMetadata(
            file_id=uploaded.id,
            original_filename=uploaded.original_filename,
            saved_filename=f"{uploaded.id}_{uploaded.original_filename}",
            file_path=path,
            file_size=uploaded.file_size,
            file_hash=uploaded.file_hash,
            mime_type=uploaded.mime_type,
            uploaded_at=uploaded.uploaded_at,
            file_type=file_type,
        )

    def _remove_object(self, path: str) -> None:
        try:
            if self.s3_service is not None and path.startswith("s3://"):
                self.s3_service.delete_file(path)
            elif path:
                Path(path).unlink(missing_ok=True)
        except Exception:
            logger.warning("Uploaded object left in place", exc_info=True)

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

    def _save_file_securely(self, file_content: bytes, uploaded: UploadedFile) -> str:
        """
        ファイルを安全に保存する

        S3 では所有者ごとのキー（``uploaded.s3_key``）に ``pending=true`` のタグを付けて
        置く。S3 がない手元の開発では、種類ごとのディレクトリに保存する（タグも
        ライフサイクルもないため、期限はメタデータの ``expires_at`` だけで効く）。

        Returns:
            str: 保存されたファイルのパス（ローカルパスまたはS3パス）
        """
        if self.s3_service:
            key, value = PENDING_TAG
            return self.s3_service.upload_file(
                file_content,
                uploaded.s3_key,
                content_type=uploaded.mime_type,
                tags={key: value},
            )

        directory = (
            self.discussion_doc_dir
            if uploaded.file_type == "discussion_document"
            else self.survey_images_dir
        )
        file_path = directory / f"{uploaded.id}_{uploaded.original_filename}"

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
