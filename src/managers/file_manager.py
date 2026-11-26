"""
File management manager.
Provides file upload, validation and storage.

Rules:

- Uploads (discussion documents, template images) take the user and put the
  S3 key at ``uploads/{owner_sub}/{file_type}/{file_id}_{original_name}`` with a
  ``pending=true`` tag, and write ``owner_sub`` and a 24-hour
  ``expires_at`` to the metadata (``UploadedFiles``)
- Attaching to a parent and the delivery decision belong to the ``FileAttachments`` component
- Files imported into memory are not stored (``convert_file_to_markdown`` only checks and converts)
- ``check_preview_path`` / ``deliver_preview`` pass and serve only paths that satisfy
  the shape, metadata match and ``can_read_file`` (arbitrary S3 paths are not
  accepted; delivery goes through ``AssetDelivery``)
"""

import hashlib
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from ..config import config
from ..models.asset_delivery import AssetHandle, DeliveryResult, SizeLimit
from ..models.authz import Permission, Principal
from ..models.errors import CodedError, ErrorCode
from ..models.uploaded_file import PENDING_TAG, UploadedFile, file_id_from_key
from ..services.service_factory import service_factory
from .components.access_policy import AccessPolicy
from .components.asset_delivery import AssetDelivery
from .components.file_attachments import FileAttachments
from .components.sharing_runtime import build_access_parts, shared_access_parts

if TYPE_CHECKING:
    from ..services.s3_service import S3Service
    from ..services.stores.uploaded_file_store import UploadedFileStore

logger = logging.getLogger(__name__)

#: Lifetime of the presigned URL of a template image preview
PREVIEW_URL_EXPIRATION_SECONDS = 3600


class FileUploadError(CodedError):
    """
    File upload errors.

    Represents both validation failures at upload and failures of the file
    operation itself, so the code is given at each raise site.
    """


class FileSecurityError(CodedError):
    """File security error"""


class FileMetadata:
    """File metadata class"""

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
        """Convert to a dict"""
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
    """File management class"""

    # Allowed formats for discussion documents
    DISCUSSION_DOCUMENT_FORMATS = {".png", ".jpg", ".jpeg", ".pdf"}
    DISCUSSION_DOCUMENT_MAX_SIZE = 10 * 1024 * 1024  # 10MB per file
    # The per-image limit comes from the Claude (Bedrock) model constraint. config is the single source of truth
    DISCUSSION_IMAGE_MAX_SIZE = config.MAX_IMAGE_SIZE  # 5MB per image
    # Limit on the whole request payload (all content including PDFs). config is the single source of truth
    DISCUSSION_DOCUMENT_TOTAL_MAX_SIZE = config.MAX_REQUEST_PAYLOAD_SIZE  # 32MB total
    DISCUSSION_IMAGE_MIMES = {"image/png", "image/jpeg"}

    # Allowed formats for knowledge files
    KNOWLEDGE_FILE_FORMATS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md"}
    KNOWLEDGE_FILE_MAX_SIZE = 10 * 1024 * 1024  # 10MB

    # Allowed formats for persona generation sources (including binary formats such as PDF/DOCX)
    PERSONA_SOURCE_FORMATS = {".txt", ".md", ".pdf", ".docx", ".doc", ".csv"}

    # Allowed formats for survey images
    SURVEY_IMAGE_FORMATS = {".png", ".jpg", ".jpeg"}
    # The per-image limit comes from the Claude (Bedrock) model constraint. config is the single source of truth
    SURVEY_IMAGE_MAX_SIZE = config.MAX_IMAGE_SIZE  # 5MB per file

    def __init__(
        self,
        s3_service: Optional["S3Service"] = None,
        file_store: Optional["UploadedFileStore"] = None,
        attachments: Optional[FileAttachments] = None,
        clock: Optional[Callable[[], datetime]] = None,
        asset_delivery: Optional[AssetDelivery] = None,
    ):
        """
        Initialize the file manager.

        Args:
            s3_service: S3 (saves locally when not configured)
            file_store: Upload metadata (optional, built on first use)
            attachments: The per-task ``FileAttachments`` (optional)
            clock: Aware-UTC clock (tests)
            asset_delivery: Serves template images (built
                from ``s3_service`` and ``Config.ASSET_DELIVERY`` when omitted)
        """
        self._delivery = asset_delivery
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

        # Create the upload directory (only when using local storage)
        if not self.s3_service:
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            self.discussion_doc_dir.mkdir(parents=True, exist_ok=True)
            self.survey_images_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _asset_delivery(self) -> AssetDelivery:
        if self._delivery is not None:
            return self._delivery
        return AssetDelivery(self.s3_service, config.ASSET_DELIVERY)

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
        Validate the format of a discussion document.

        Args:
            filename: File name
            file_content: File content (bytes)

        Returns:
            bool: Validation result (True: valid)

        Raises:
            FileUploadError: If the file format is invalid
        """
        # File extension check
        file_ext = Path(filename).suffix.lower()
        if file_ext not in self.DISCUSSION_DOCUMENT_FORMATS:
            raise FileUploadError(
                f"extension {file_ext!r} not in discussion document formats",
                code=ErrorCode.FILE_FORMAT_NOT_ALLOWED,
                context={
                    "allowed_formats": ", ".join(self.DISCUSSION_DOCUMENT_FORMATS)
                },
            )

        # MIME type check (simple)
        mime_type = mimetypes.guess_type(filename)[0]
        allowed_mimes = {"image/png", "image/jpeg", "application/pdf"}
        if mime_type not in allowed_mimes:
            raise FileUploadError(
                f"mime type {mime_type!r} not allowed for discussion documents",
                code=ErrorCode.FILE_MIME_UNSUPPORTED,
            )

        # File size check (images are 5MB to match Bedrock's limit)
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

        # Check that the file content is not empty
        if len(file_content) == 0:
            raise FileUploadError("file is empty", code=ErrorCode.FILE_EMPTY)

        return True

    def convert_file_to_markdown(self, file_content: bytes, filename: str) -> str:
        """
        Convert a knowledge file to Markdown after a safety check (nothing is stored).

        Args:
            file_content: File content (bytes)
            filename: File name

        Returns:
            str: Text converted to Markdown

        Raises:
            FileSecurityError: If the file name or content fails the safety check
            FileUploadError: If the format, size or emptiness is invalid, or conversion fails
        """
        from markitdown import MarkItDown
        import io

        self._security_check(filename, file_content, allow_binary=True)

        # File extension check
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
            # Create a BytesIO object and set the file name
            file_stream = io.BytesIO(file_content)
            file_stream.name = filename

            # Convert to Markdown
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
        Upload a discussion document (own key, 24-hour expiry).

        Args:
            principal: The uploader (needs resource:create)
            file_content: File content (bytes)
            filename: Original file name

        Returns:
            FileMetadata: File metadata

        Raises:
            AccessDeniedError: No resource:create
            FileUploadError: If an error occurs during upload
        """
        AccessPolicy.require(principal, Permission.RESOURCE_CREATE)
        try:
            # Security check (binary files allowed)
            self._security_check(filename, file_content, allow_binary=True)

            # Discussion document format validation
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
        Upload an interview attachment (same mechanism as discussion documents).

        It is stored under the user's own key, with the ``pending`` tag and a 24-hour
        expiry, and the session holds only the ID. Type and size checks stay as
        the interview has always done them (images use ``MAX_IMAGE_SIZE``, everything
        else ``MAX_FILE_SIZE``).

        Args:
            principal: The uploader (needs resource:create)
            file_content: File content (bytes)
            filename: Original file name
            mime_type: The type the browser sent (used when it cannot be told from the file name)

        Returns:
            FileMetadata: File metadata

        Raises:
            AccessDeniedError: No resource:create
            FileUploadError: ``FILE_MIME_UNSUPPORTED``, ``FILE_TOO_LARGE``,
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
        Upload a survey image (own key, 24-hour expiry).

        Args:
            principal: The uploader (needs resource:create)
            file_content: File content (bytes)
            filename: Original file name

        Returns:
            FileMetadata: File metadata

        Raises:
            AccessDeniedError: No resource:create
            FileUploadError: If an error occurs during upload
        """
        AccessPolicy.require(principal, Permission.RESOURCE_CREATE)
        try:
            self._security_check(filename, file_content, allow_binary=True)
            # Format and size validation
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

    def check_preview_path(self, principal: Principal, file_path: str) -> None:
        """Check that ``file_path`` is a template image the user may read.

        Nothing is signed or opened; the router then embeds the app's own
        preview route for this path.

        Raises:
            FileUploadError: ``FILE_NOT_FOUND``
        """
        self._readable_preview(principal, file_path)

    def deliver_preview(self, principal: Principal, file_path: str) -> DeliveryResult:
        """Serve a template image after the preview path check.

        Raises:
            FileUploadError: ``FILE_NOT_FOUND`` (failed check or the object is
                gone), ``ASSET_TOO_LARGE``, or ``FILE_OPERATION_FAILED`` when
                S3 fails
        """
        uploaded = self._readable_preview(principal, file_path)
        handle = AssetHandle(
            s3_path=file_path,
            content_type=uploaded.mime_type,
            download_name=uploaded.original_filename,
            size_limit=SizeLimit.UPLOAD,
            not_found_code=ErrorCode.FILE_NOT_FOUND,
            presigned_expiration=PREVIEW_URL_EXPIRATION_SECONDS,
        )
        try:
            return self._asset_delivery.deliver(handle, error_type=FileUploadError)
        except FileUploadError as e:
            if e.code is not ErrorCode.S3_OPERATION_FAILED:
                raise
            logger.warning("Preview could not be served: S3 failed")
            raise FileUploadError(
                "preview delivery failed", code=ErrorCode.FILE_OPERATION_FAILED
            ) from e
        except Exception as e:
            logger.warning("Preview could not be served: %s", type(e).__name__)
            raise FileUploadError(
                f"preview delivery failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def _readable_preview(self, principal: Principal, file_path: str) -> UploadedFile:
        """The upload behind a template image path the user may read.

        Only ``s3://{bucket}/uploads/{owner}/survey_image/{file_id}_{name}`` is
        accepted, where ``file_id`` is a UUID whose metadata holds exactly that
        key and ``FileAttachments.can_read_file`` passes. Anything else gets
        ``FILE_NOT_FOUND``.

        Raises:
            FileUploadError: ``FILE_NOT_FOUND``
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
        return uploaded

    # Private methods

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
        Run security checks on a file.

        Args:
            filename: File name
            file_content: File content
            allow_binary: Whether binary files are allowed

        Raises:
            FileSecurityError: If there is a security problem
        """
        # File name safety check
        if ".." in filename or "/" in filename or "\\" in filename:
            raise FileSecurityError(
                "filename contains a path traversal or invalid character",
                code=ErrorCode.FILE_NAME_INVALID,
            )

        # File name length check
        if len(filename) > 255:
            raise FileSecurityError(
                f"filename length {len(filename)} exceeds 255",
                code=ErrorCode.FILE_NAME_TOO_LONG,
            )

        # Hidden file check
        if filename.startswith("."):
            raise FileSecurityError(
                "filename starts with a dot", code=ErrorCode.FILE_HIDDEN_NOT_ALLOWED
            )

        # Simple binary file check (presence of NUL characters)
        # Skipped when allow_binary is True
        if not allow_binary and b"\x00" in file_content[:1024]:  # Check the first 1KB
            raise FileSecurityError(
                "NUL byte found in the first 1KB",
                code=ErrorCode.FILE_BINARY_NOT_ALLOWED,
            )

    def _save_file_securely(self, file_content: bytes, uploaded: UploadedFile) -> str:
        """
        Store a file safely.

        On S3 it is placed at the per-owner key (``uploaded.s3_key``) with a ``pending=true``
        tag. In local development without S3 it is saved in a per-type directory
        (there is no tag or lifecycle, so only the metadata's ``expires_at``
        enforces expiry).

        Returns:
            str: Path of the stored file (local path or S3 path)
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

        # Write to a temp file, then move atomically
        temp_path = file_path.with_suffix(file_path.suffix + ".tmp")

        try:
            with open(temp_path, "wb") as f:
                f.write(file_content)

            # Atomic move
            temp_path.rename(file_path)

            # Set file permissions (read-only)
            file_path.chmod(0o644)

            return str(file_path.absolute())

        except Exception as e:
            # Delete the temp file if it remains
            if temp_path.exists():
                temp_path.unlink()
            raise FileUploadError(
                f"file save failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e
