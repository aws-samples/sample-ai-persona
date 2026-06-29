"""
ドキュメント読み込み・ContentBlock準備ユーティリティ

AgentDiscussionManager, InterviewManager から共通利用される
ドキュメント処理ロジックを集約する。
"""

import re
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...services.database_service import DatabaseService
from ...services.s3_service import S3Service

logger = logging.getLogger(__name__)


def load_documents_metadata(
    document_ids: List[str],
    database_service: DatabaseService,
) -> List[Dict[str, Any]]:
    """
    ドキュメントIDリストからメタデータを取得する。

    Args:
        document_ids: ドキュメントIDのリスト
        database_service: データベースサービス

    Returns:
        メタデータ辞書のリスト。各辞書は id, filename, file_path, file_size, mime_type, uploaded_at を含む。
    """
    documents_metadata: List[Dict[str, Any]] = []

    for doc_id in document_ids:
        try:
            file_info = database_service.get_uploaded_file_info(doc_id)
            if file_info is None:
                logger.warning(f"ドキュメントが見つかりません: {doc_id}")
                continue

            from datetime import datetime

            uploaded_at = file_info.get("uploaded_at")
            if isinstance(uploaded_at, str):
                uploaded_at = datetime.fromisoformat(uploaded_at)

            documents_metadata.append(
                {
                    "id": file_info["id"],
                    "filename": file_info.get(
                        "original_filename", file_info["filename"]
                    ),
                    "file_path": file_info["file_path"],
                    "file_size": file_info.get("file_size", 0),
                    "mime_type": file_info.get("mime_type", "text/plain"),
                    "uploaded_at": uploaded_at.isoformat() if uploaded_at else None,
                }
            )
        except Exception as e:
            logger.error(f"ドキュメントメタデータ取得エラー ({doc_id}): {e}")
            continue

    return documents_metadata


def prepare_document_contents(
    documents_metadata: List[Dict[str, Any]],
    s3_service: Optional[S3Service] = None,
) -> List[Dict[str, Any]]:
    """
    ドキュメントメタデータからStrands Agent SDK用のContentBlockリストを準備する。

    Args:
        documents_metadata: load_documents_metadata() の返り値
        s3_service: S3サービス（S3パスのファイル読み込みに必要）

    Returns:
        Strands Agent SDK用のContentBlockリスト
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

            content_block = _build_content_block(file_bytes, mime_type, filename)
            if content_block:
                content_list.append(content_block)

        except Exception as e:
            logger.error(f"ドキュメント処理エラー ({filename}): {e}")
            continue

    return content_list


def build_document_context(documents_metadata: List[Dict[str, Any]]) -> Optional[str]:
    """
    ドキュメントメタデータから議論用のコンテキスト文字列を構築する。

    Args:
        documents_metadata: load_documents_metadata() の返り値

    Returns:
        コンテキスト文字列。ドキュメントがない場合はNone。
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
    file_path: str, s3_service: Optional[S3Service]
) -> Optional[bytes]:
    """ファイルパスからバイト列を読み込む。"""
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


def _build_content_block(
    file_bytes: bytes, mime_type: str, filename: str
) -> Optional[Dict[str, Any]]:
    """MIMEタイプに応じたContentBlockを構築する。"""
    if mime_type.startswith("image/"):
        image_format = mime_type.split("/")[-1]
        if image_format not in ("png", "jpeg", "gif", "webp"):
            image_format = "png"
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
    """ファイル名から安全なドキュメント名を生成する。"""
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[^a-zA-Z0-9_]", "_", base)[:100]
