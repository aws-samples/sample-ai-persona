"""Shared utilities for loading documents and preparing ContentBlocks.

Collects the document handling used by AgentDiscussionManager and
InterviewManager.
"""

import re
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from ...models.uploaded_file import UploadedFile
from .file_utils import fit_image_for_model

if TYPE_CHECKING:
    from ...services.s3_service import S3Service

logger = logging.getLogger(__name__)

SUPPORTED_IMAGE_TYPES: List[str] = [
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
]
SUPPORTED_DOCUMENT_TYPES: List[str] = [
    "application/pdf",
    "text/plain",
    "text/csv",
    "text/html",
    "text/markdown",
]
SUPPORTED_MIME_TYPES: List[str] = SUPPORTED_IMAGE_TYPES + SUPPORTED_DOCUMENT_TYPES


def is_supported_mime_type(mime_type: str) -> bool:
    """Return whether the MIME type is supported."""
    return mime_type in SUPPORTED_MIME_TYPES


def is_image_type(mime_type: str) -> bool:
    """Return whether the MIME type is an image."""
    return mime_type in SUPPORTED_IMAGE_TYPES


def load_documents_metadata(
    document_ids: List[str],
    read_file: Callable[[str], UploadedFile],
) -> List[Dict[str, Any]]:
    """Fetch metadata for a list of document IDs.

    ``read_file`` returns only files the user may still attach (``FileAttachments``
    ``attachable_file``). Unusable files are not skipped: the exception from
    ``read_file`` propagates, so the run stops before a discussion starts (same
    condition as when attaching at save time).

    Args:
        document_ids: List of document IDs.
        read_file: file ID -> the user's attachable ``UploadedFile`` (raises otherwise)

    Returns:
        List of metadata dicts, each with id, filename, file_path, file_size, mime_type, uploaded_at.

    Raises:
        Exception: Whatever ``read_file`` raises for an unusable document.
    """
    documents_metadata: List[Dict[str, Any]] = []

    for doc_id in document_ids:
        uploaded = read_file(doc_id)
        documents_metadata.append(
            {
                "id": uploaded.id,
                "filename": uploaded.original_filename,
                "file_path": uploaded.file_path,
                "file_size": uploaded.file_size,
                "mime_type": uploaded.mime_type or "text/plain",
                "uploaded_at": uploaded.uploaded_at.isoformat(),
            }
        )

    return documents_metadata


def prepare_document_contents(
    documents_metadata: List[Dict[str, Any]],
    s3_service: Optional["S3Service"] = None,
) -> List[Dict[str, Any]]:
    """Prepare a ContentBlock list for the Strands Agent SDK from document metadata.

    Args:
        documents_metadata: Return value of load_documents_metadata().
        s3_service: S3 service (needed to read files at S3 paths).

    Returns:
        ContentBlock list for the Strands Agent SDK.
    """
    content_list: List[Dict[str, Any]] = []

    for doc in documents_metadata:
        filename = doc.get("filename", "document")
        try:
            file_path = doc.get("file_path", "")
            mime_type = doc.get("mime_type", "")

            file_bytes = _read_file_bytes(file_path, s3_service)
            if file_bytes is None:
                continue

            content_block = build_content_block(file_bytes, mime_type, filename)
            if content_block:
                content_list.append(content_block)

        except Exception as e:
            logger.error(f"ドキュメント処理エラー ({filename}): {e}")
            continue

    return content_list


def build_document_context(documents_metadata: List[Dict[str, Any]]) -> Optional[str]:
    """Build the discussion context string from document metadata.

    Args:
        documents_metadata: Return value of load_documents_metadata().

    Returns:
        Context string, or None when there are no documents.
    """
    if not documents_metadata:
        return None

    descriptions = [
        f"- {doc['filename']} ({doc.get('mime_type', 'unknown')})"
        for doc in documents_metadata
    ]
    return "\n".join(
        [
            "\n以下のドキュメントを参照しながら議論を進めてください:",
            *descriptions,
        ]
    )


def _read_file_bytes(
    file_path: str, s3_service: Optional["S3Service"]
) -> Optional[bytes]:
    """Read bytes from a file path."""
    if file_path.startswith("s3://"):
        if not s3_service:
            logger.warning(f"S3サービスが利用できません: {file_path}")
            return None
        return s3_service.download_file(file_path)
    else:
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"ファイルが見つかりません: {file_path}")
            return None
        with open(path, "rb") as f:
            return f.read()


def build_content_block(
    file_bytes: bytes, mime_type: str, filename: str
) -> Optional[Dict[str, Any]]:
    """Build a ContentBlock according to the MIME type."""
    if mime_type.startswith("image/"):
        image_format = mime_type.split("/")[-1]
        if image_format not in ("png", "jpeg", "gif", "webp"):
            image_format = "png"
        fitted = fit_image_for_model(file_bytes, image_format)
        if fitted is None:
            logger.warning(
                f"画像をモデルの上限に収められないため除外しました: {filename}"
            )
            return None
        file_bytes, image_format = fitted
        logger.info(f"画像を追加しました: {filename} ({image_format})")
        return {
            "image": {
                "format": image_format,
                "source": {"bytes": file_bytes},
            }
        }

    if mime_type == "application/pdf":
        safe_name = _safe_document_name(filename)
        logger.info(f"PDFを追加しました: {filename}")
        return {
            "document": {
                "name": safe_name,
                "format": "pdf",
                "source": {"bytes": file_bytes},
            }
        }

    text_format_map = {
        "text/plain": "txt",
        "text/csv": "csv",
        "text/html": "html",
        "text/markdown": "md",
    }
    if mime_type in text_format_map:
        safe_name = _safe_document_name(filename)
        doc_format = text_format_map[mime_type]
        logger.info(f"テキストドキュメントを追加しました: {filename} ({doc_format})")
        return {
            "document": {
                "name": safe_name,
                "format": doc_format,
                "source": {"bytes": file_bytes},
            }
        }

    logger.warning(f"サポートされていないMIMEタイプ: {mime_type} ({filename})")
    return None


def _safe_document_name(filename: str) -> str:
    """Generate a safe document name from a file name."""
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[^a-zA-Z0-9_]", "_", base)[:100]
