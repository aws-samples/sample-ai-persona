"""
ファイル管理マネージャー
ファイルアップロード、検証、保存機能を提供
"""

import os
import uuid
import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, TYPE_CHECKING
from datetime import datetime

from ..config import config
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory

if TYPE_CHECKING:
    from ..services.s3_service import S3Service


class FileUploadError(Exception):
    """ファイルアップロード関連のエラー"""

    pass


class FileSecurityError(Exception):
    """ファイルセキュリティ関連のエラー"""

    pass


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

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileMetadata":
        """辞書から作成"""
        return cls(
            file_id=data["file_id"],
            original_filename=data["original_filename"],
            saved_filename=data["saved_filename"],
            file_path=data["file_path"],
            file_size=data["file_size"],
            file_hash=data["file_hash"],
            mime_type=data["mime_type"],
            uploaded_at=datetime.fromisoformat(data["uploaded_at"]),
        )


class FileManager:
    """ファイル管理クラス"""

    # 議論用ドキュメントの許可形式
    DISCUSSION_DOCUMENT_FORMATS = {".png", ".jpg", ".jpeg", ".pdf"}
    DISCUSSION_DOCUMENT_MAX_SIZE = 10 * 1024 * 1024  # 10MB per file
    DISCUSSION_DOCUMENT_TOTAL_MAX_SIZE = 32 * 1024 * 1024  # 32MB total (Bedrock limit)

    # 知識ファイルの許可形式
    KNOWLEDGE_FILE_FORMATS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md"}
    KNOWLEDGE_FILE_MAX_SIZE = 10 * 1024 * 1024  # 10MB

    # 市場調査レポートの許可形式
    MARKET_REPORT_FORMATS = {".pdf", ".docx", ".doc", ".txt", ".md"}
    MARKET_REPORT_MAX_SIZE = 10 * 1024 * 1024  # 10MB

    # アンケート画像の許可形式
    SURVEY_IMAGE_FORMATS = {".png", ".jpg", ".jpeg"}
    SURVEY_IMAGE_MAX_SIZE = 5 * 1024 * 1024  # 5MB per file

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
        self.max_file_size = config.MAX_FILE_SIZE
        self.allowed_extensions = config.ALLOWED_FILE_EXTENSIONS
        self.s3_service = s3_service

        # Use singleton database service if not provided
        self.db_service = db_service or service_factory.get_database_service()

        # アップロードディレクトリの作成（ローカルストレージ使用時のみ）
        if not self.s3_service:
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            self.discussion_doc_dir.mkdir(parents=True, exist_ok=True)
            self.knowledge_files_dir.mkdir(parents=True, exist_ok=True)
            self.survey_images_dir.mkdir(parents=True, exist_ok=True)

    def validate_file_format(self, filename: str, file_content: bytes) -> bool:
        """
        ファイル形式を検証する

        Args:
            filename: ファイル名
            file_content: ファイル内容（バイト）

        Returns:
            bool: 検証結果（True: 有効, False: 無効）

        Raises:
            FileUploadError: ファイル形式が無効な場合
        """
        # ファイル拡張子チェック
        if not config.is_allowed_file_extension(filename):
            raise FileUploadError(
                f"許可されていないファイル形式です。"
                f"対応形式: {', '.join(self.allowed_extensions)}"
            )

        # ファイルサイズチェック
        if len(file_content) > self.max_file_size:
            max_size_mb = self.max_file_size / (1024 * 1024)
            raise FileUploadError(
                f"ファイルサイズが制限を超えています。最大サイズ: {max_size_mb:.1f}MB"
            )

        # ファイル内容が空でないかチェック
        if len(file_content) == 0:
            raise FileUploadError("ファイルが空です。")

        # テキストファイルとして読み取り可能かチェック
        try:
            # UTF-8でデコードを試行
            content_str = file_content.decode("utf-8")

            # 最小限の内容チェック（10文字以上）
            if len(content_str.strip()) < 10:
                raise FileUploadError(
                    "ファイル内容が短すぎます。"
                    "インタビューなどの内容を含むテキストファイルをアップロードしてください。"
                )

        except UnicodeDecodeError:
            # UTF-8で読めない場合、他のエンコーディングを試行
            try:
                content_str = file_content.decode("shift_jis")
            except UnicodeDecodeError:
                try:
                    content_str = file_content.decode("euc-jp")
                except UnicodeDecodeError:
                    raise FileUploadError(
                        "テキストファイルとして読み取れません。"
                        "UTF-8、Shift_JIS、EUC-JPのいずれかでエンコードされた"
                        "テキストファイルをアップロードしてください。"
                    )

        return True

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
                f"許可されていないファイル形式です。"
                f"対応形式: {', '.join(self.DISCUSSION_DOCUMENT_FORMATS)}"
            )

        # ファイルサイズチェック（個別ファイル）
        if len(file_content) > self.DISCUSSION_DOCUMENT_MAX_SIZE:
            max_size_mb = self.DISCUSSION_DOCUMENT_MAX_SIZE / (1024 * 1024)
            raise FileUploadError(
                f"ファイルサイズが制限を超えています。最大サイズ: {max_size_mb:.1f}MB"
            )

        # ファイル内容が空でないかチェック
        if len(file_content) == 0:
            raise FileUploadError("ファイルが空です。")

        # MIMEタイプチェック（簡易）
        mime_type = mimetypes.guess_type(filename)[0]
        allowed_mimes = {"image/png", "image/jpeg", "application/pdf"}
        if mime_type not in allowed_mimes:
            raise FileUploadError(f"サポートされていないMIMEタイプです: {mime_type}")

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
                f"許可されていないファイル形式です。"
                f"対応形式: {', '.join(self.KNOWLEDGE_FILE_FORMATS)}"
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
            raise FileUploadError(f"ファイルのマークダウン変換に失敗しました: {str(e)}")

    def extract_text_from_file(self, file_content: bytes, filename: str) -> str:
        """
        市場調査レポートファイルからテキストを抽出する

        PDF/Word/テキストファイルから統一的にテキストを抽出します。
        markitdownライブラリを使用してPDF/Wordをマークダウンに変換し、
        テキストファイルは直接デコードします。

        Args:
            file_content: ファイル内容（バイト）
            filename: ファイル名

        Returns:
            str: 抽出されたテキスト

        Raises:
            FileUploadError: ファイル形式が無効、またはテキスト抽出に失敗した場合
        """
        from markitdown import MarkItDown
        import io

        # ファイル拡張子チェック
        file_ext = Path(filename).suffix.lower()
        if file_ext not in self.MARKET_REPORT_FORMATS:
            raise FileUploadError(
                f"許可されていないファイル形式です。"
                f"対応形式: {', '.join(self.MARKET_REPORT_FORMATS)}"
            )

        # ファイルサイズチェック
        if len(file_content) > self.MARKET_REPORT_MAX_SIZE:
            max_size_mb = self.MARKET_REPORT_MAX_SIZE / (1024 * 1024)
            raise FileUploadError(
                f"ファイルサイズが制限を超えています。最大サイズ: {max_size_mb:.1f}MB"
            )

        # ファイル内容が空でないかチェック
        if len(file_content) == 0:
            raise FileUploadError("ファイルが空です。")

        try:
            # テキストファイルの場合は直接デコード
            if file_ext in {".txt", ".md"}:
                try:
                    text = file_content.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        text = file_content.decode("shift_jis")
                    except UnicodeDecodeError:
                        try:
                            text = file_content.decode("euc-jp")
                        except UnicodeDecodeError:
                            raise FileUploadError(
                                "テキストファイルとして読み取れません。"
                                "UTF-8、Shift_JIS、EUC-JPのいずれかでエンコードされた"
                                "テキストファイルをアップロードしてください。"
                            )
            else:
                # PDF/Wordの場合はmarkitdownで変換
                md = MarkItDown()
                file_stream = io.BytesIO(file_content)
                file_stream.name = filename
                result = md.convert_stream(file_stream)
                text = result.text_content

            # 最小限の内容チェック（100文字以上）
            if len(text.strip()) < 100:
                raise FileUploadError(
                    "ファイル内容が短すぎます。"
                    "市場調査レポートなどの詳細な内容を含むファイルをアップロードしてください。"
                )

            return text

        except FileUploadError:
            raise
        except Exception as e:
            raise FileUploadError(f"ファイルからのテキスト抽出に失敗しました: {str(e)}")

    def upload_interview_file(
        self, file_content: bytes, filename: str, allow_duplicates: bool = False
    ) -> Tuple[str, str, FileMetadata]:
        """
        インタビューファイルをアップロードする

        Args:
            file_content: ファイル内容（バイト）
            filename: 元のファイル名
            allow_duplicates: 重複ファイルを許可するかどうか

        Returns:
            Tuple[str, str, FileMetadata]: (保存されたファイルパス, ファイル内容のテキスト, ファイルメタデータ)

        Raises:
            FileUploadError: アップロード処理でエラーが発生した場合
        """
        try:
            # セキュリティチェック
            self._security_check(filename, file_content)

            # ファイル形式検証
            self.validate_file_format(filename, file_content)

            # 重複チェック（allow_duplicatesがFalseの場合）
            if not allow_duplicates:
                existing_file = self._check_duplicate_file(filename, file_content)
                if existing_file:
                    # 既存ファイルの情報を返す
                    file_text = self._decode_file_content(file_content)
                    return existing_file.file_path, file_text, existing_file

            # ファイル内容をテキストとして取得
            file_text = self._decode_file_content(file_content)

            # ファイルメタデータを生成
            file_metadata = self._create_file_metadata(
                filename, file_content, file_type="persona_interview"
            )

            # ファイルを安全に保存
            saved_file_path = self._save_file_securely(
                file_content,
                file_metadata.saved_filename,
                file_type="persona_interview",
            )
            file_metadata.file_path = saved_file_path

            # メタデータをデータベースに保存
            self._save_file_metadata(file_metadata, file_type="persona_interview")

            return saved_file_path, file_text, file_metadata

        except (FileUploadError, FileSecurityError):
            # 既知のエラーはそのまま再発生
            raise
        except Exception as e:
            # その他のエラーはFileUploadErrorでラップ
            raise FileUploadError(
                f"ファイルアップロード中にエラーが発生しました: {str(e)}"
            )

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
                f"議論用ドキュメントアップロード中にエラーが発生しました: {str(e)}"
            )

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
                    f"許可されていないファイル形式です。対応形式: {', '.join(self.SURVEY_IMAGE_FORMATS)}"
                )
            if len(file_content) > self.SURVEY_IMAGE_MAX_SIZE:
                raise FileUploadError(
                    f"ファイルサイズが制限を超えています。最大サイズ: {self.SURVEY_IMAGE_MAX_SIZE / (1024 * 1024):.1f}MB"
                )
            if len(file_content) == 0:
                raise FileUploadError("ファイルが空です。")

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
                f"アンケート画像アップロード中にエラーが発生しました: {str(e)}"
            )

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
                    f"ファイルサイズが制限を超えています。最大サイズ: {max_size_mb:.1f}MB"
                )

            # ファイル内容が空でないかチェック
            if len(file_content) == 0:
                raise FileUploadError("ファイルが空です。")

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
                f"知識ファイルアップロード中にエラーが発生しました: {str(e)}"
            )

    def save_uploaded_file(self, file_content: bytes, filename: str) -> str:
        """
        アップロードされたファイルを保存する（レガシーメソッド）

        Args:
            file_content: ファイル内容（バイト）
            filename: 保存するファイル名

        Returns:
            str: 保存されたファイルの絶対パス

        Raises:
            FileUploadError: ファイル保存でエラーが発生した場合
        """
        return self._save_file_securely(file_content, filename)

    def _decode_file_content(self, file_content: bytes) -> str:
        """
        ファイル内容をテキストとしてデコードする

        Args:
            file_content: ファイル内容（バイト）

        Returns:
            str: デコードされたテキスト

        Raises:
            FileUploadError: デコードに失敗した場合
        """
        # エンコーディングを順番に試行
        encodings = ["utf-8", "shift_jis", "euc-jp", "cp932"]

        for encoding in encodings:
            try:
                return file_content.decode(encoding)
            except UnicodeDecodeError:
                continue

        # すべてのエンコーディングで失敗した場合
        raise FileUploadError(
            "テキストファイルとして読み取れません。"
            "UTF-8、Shift_JIS、EUC-JPのいずれかでエンコードされた"
            "テキストファイルをアップロードしてください。"
        )

    def get_uploaded_file_content(self, file_path: str) -> str:
        """
        保存されたファイルの内容を取得する

        Args:
            file_path: ファイルパス（ローカルまたはS3パス）

        Returns:
            str: ファイル内容

        Raises:
            FileUploadError: ファイル読み取りでエラーが発生した場合
        """
        try:
            # S3パスの場合
            if file_path.startswith("s3://"):
                if not self.s3_service:
                    raise FileUploadError("S3サービスが設定されていません。")
                file_content = self.s3_service.download_file(file_path)
                return self._decode_file_content(file_content)

            # ローカルファイルの場合
            path = Path(file_path)

            if not path.exists():
                raise FileUploadError("指定されたファイルが見つかりません。")

            with open(path, "rb") as f:
                file_content = f.read()

            return self._decode_file_content(file_content)

        except OSError as e:
            raise FileUploadError(f"ファイル読み取り中にエラーが発生しました: {str(e)}")

    def delete_uploaded_file(self, file_id: str) -> bool:
        """
        アップロードされたファイルを削除する

        Args:
            file_id: 削除するファイルのID

        Returns:
            bool: 削除成功の場合True

        Raises:
            FileUploadError: ファイル削除でエラーが発生した場合
        """
        try:
            # メタデータを取得
            metadata = self._get_file_metadata_by_id(file_id)
            if not metadata:
                return False

            # S3パスの場合
            if metadata.file_path.startswith("s3://"):
                if self.s3_service:
                    self.s3_service.delete_file(metadata.file_path)
                deleted_file = True
            else:
                # ローカルファイルの場合
                path = Path(metadata.file_path)

                # アップロードディレクトリ内のファイルかチェック
                if not str(path.absolute()).startswith(str(self.upload_dir.absolute())):
                    raise FileSecurityError("指定されたファイルは削除できません。")

                # ファイルを削除
                deleted_file = False
                if path.exists():
                    path.unlink()
                    deleted_file = True

            # メタデータを削除
            deleted_metadata = self._delete_file_metadata(file_id)

            return deleted_file or deleted_metadata

        except (FileUploadError, FileSecurityError):
            raise
        except Exception as e:
            raise FileUploadError(f"ファイル削除中にエラーが発生しました: {str(e)}")

    def _delete_file_metadata(self, file_id: str) -> bool:
        """
        ファイルメタデータをデータベースから削除する

        Args:
            file_id: ファイルID

        Returns:
            bool: 削除成功の場合True
        """
        try:
            return self.db_service.delete_uploaded_file_info(file_id)
        except Exception as e:
            raise DatabaseError(
                f"ファイルメタデータ削除中にエラーが発生しました: {str(e)}"
            )

    def list_uploaded_files(self) -> List[FileMetadata]:
        """
        アップロードされたファイルの一覧を取得する

        Returns:
            List[FileMetadata]: ファイルメタデータのリスト
        """
        try:
            return self._get_all_file_metadata()
        except Exception as e:
            raise FileUploadError(f"ファイル一覧取得中にエラーが発生しました: {str(e)}")

    def get_file_metadata(self, file_id: str) -> Optional[FileMetadata]:
        """
        ファイルメタデータを取得する

        Args:
            file_id: ファイルID

        Returns:
            FileMetadata: ファイルメタデータ（見つからない場合はNone）
        """
        try:
            return self._get_file_metadata_by_id(file_id)
        except Exception as e:
            raise FileUploadError(
                f"ファイルメタデータ取得中にエラーが発生しました: {str(e)}"
            )

    def verify_file_integrity(self, file_id: str) -> bool:
        """
        ファイルの整合性を検証する

        Args:
            file_id: ファイルID

        Returns:
            bool: 整合性が保たれている場合True
        """
        try:
            metadata = self._get_file_metadata_by_id(file_id)
            if not metadata:
                return False

            # S3パスの場合
            if metadata.file_path.startswith("s3://"):
                if not self.s3_service:
                    return False
                try:
                    file_content = self.s3_service.download_file(metadata.file_path)
                    # ファイルサイズの確認
                    if len(file_content) != metadata.file_size:
                        return False
                    # ハッシュ値の確認
                    current_hash = hashlib.sha256(file_content).hexdigest()
                    return current_hash == metadata.file_hash
                except Exception:
                    return False

            # ローカルファイルの場合
            file_path = Path(metadata.file_path)
            if not file_path.exists():
                return False

            # ファイルサイズの確認
            if file_path.stat().st_size != metadata.file_size:
                return False

            # ハッシュ値の確認
            with open(file_path, "rb") as f:
                current_hash = hashlib.sha256(f.read()).hexdigest()

            return current_hash == metadata.file_hash

        except Exception as e:
            raise FileUploadError(
                f"ファイル整合性検証中にエラーが発生しました: {str(e)}"
            )

    def cleanup_orphaned_files(self) -> int:
        """
        孤立したファイル（メタデータがないファイル）を削除する

        Returns:
            int: 削除されたファイル数
        """
        try:
            deleted_count = 0
            all_metadata = self._get_all_file_metadata()
            registered_files = {Path(meta.file_path).name for meta in all_metadata}

            for file_path in self.upload_dir.glob("*"):
                if file_path.is_file() and file_path.name not in registered_files:
                    try:
                        file_path.unlink()
                        deleted_count += 1
                    except OSError:
                        # 削除に失敗したファイルは無視
                        pass

            return deleted_count

        except Exception as e:
            raise FileUploadError(f"孤立ファイル削除中にエラーが発生しました: {str(e)}")

    def get_storage_usage(self) -> Dict[str, Any]:
        """
        ストレージ使用量情報を取得する

        Returns:
            Dict[str, Any]: ストレージ使用量情報
        """
        try:
            total_size = 0
            file_count = 0

            for file_path in self.upload_dir.glob("*"):
                if file_path.is_file():
                    total_size += file_path.stat().st_size
                    file_count += 1

            return {
                "total_files": file_count,
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "upload_dir": str(self.upload_dir.absolute()),
                "max_file_size_mb": round(self.max_file_size / (1024 * 1024), 2),
            }

        except Exception as e:
            raise FileUploadError(
                f"ストレージ使用量取得中にエラーが発生しました: {str(e)}"
            )

    def search_files_by_content(self, search_term: str) -> List[FileMetadata]:
        """
        ファイル内容でファイルを検索する

        Args:
            search_term: 検索キーワード

        Returns:
            List[FileMetadata]: 検索にマッチしたファイルのメタデータリスト
        """
        try:
            matching_files = []
            all_files = self._get_all_file_metadata()

            for metadata in all_files:
                try:
                    content = self.get_uploaded_file_content(metadata.file_path)
                    if search_term.lower() in content.lower():
                        matching_files.append(metadata)
                except FileUploadError:
                    # ファイル読み取りエラーは無視して続行
                    continue

            return matching_files

        except Exception as e:
            raise FileUploadError(f"ファイル検索中にエラーが発生しました: {str(e)}")

    def get_file_statistics(self) -> Dict[str, Any]:
        """
        ファイル統計情報を取得する

        Returns:
            Dict[str, Any]: ファイル統計情報
        """
        try:
            all_files = self._get_all_file_metadata()

            if not all_files:
                return {
                    "total_files": 0,
                    "average_size_bytes": 0,
                    "largest_file_size": 0,
                    "smallest_file_size": 0,
                    "file_types": {},
                    "upload_dates": [],
                }

            file_sizes = [f.file_size for f in all_files]
            file_types: dict[str, int] = {}

            for metadata in all_files:
                ext = Path(metadata.original_filename).suffix.lower()
                file_types[ext] = file_types.get(ext, 0) + 1

            return {
                "total_files": len(all_files),
                "average_size_bytes": sum(file_sizes) // len(file_sizes),
                "largest_file_size": max(file_sizes),
                "smallest_file_size": min(file_sizes),
                "file_types": file_types,
                "upload_dates": [f.uploaded_at.date().isoformat() for f in all_files],
            }

        except Exception as e:
            raise FileUploadError(f"ファイル統計取得中にエラーが発生しました: {str(e)}")

    def bulk_delete_files(self, file_ids: List[str]) -> Dict[str, bool]:
        """
        複数���ファイルを一括削除する

        Args:
            file_ids: 削除するファイルIDのリスト

        Returns:
            Dict[str, bool]: ファイルIDと削除結果のマッピング
        """
        results = {}

        for file_id in file_ids:
            try:
                results[file_id] = self.delete_uploaded_file(file_id)
            except Exception:
                results[file_id] = False

        return results

    def export_file_metadata(self) -> List[Dict[str, Any]]:
        """
        ファイルメタデータをエクスポート用の辞書リストとして取得する

        Returns:
            List[Dict[str, Any]]: エクスポート用メタデータリスト
        """
        try:
            all_files = self._get_all_file_metadata()
            return [metadata.to_dict() for metadata in all_files]

        except Exception as e:
            raise FileUploadError(
                f"メタデータエクスポート中にエラーが発生しました: {str(e)}"
            )

    def validate_system_health(self) -> Dict[str, Any]:
        """
        ファイル管理システムの健全性をチェックする

        Returns:
            Dict[str, Any]: システム健全性レポート
        """
        try:
            health_report: Dict[str, Any] = {
                "upload_dir_exists": self.upload_dir.exists(),
                "upload_dir_writable": os.access(self.upload_dir, os.W_OK),
                "database_accessible": False,
                "orphaned_files": 0,
                "corrupted_files": [],
                "missing_files": [],
                "total_files": 0,
            }

            # データベースアクセス確認
            try:
                all_files = self._get_all_file_metadata()
                health_report["database_accessible"] = True
                health_report["total_files"] = len(all_files)

                # ファイル整合性チェック
                for metadata in all_files:
                    file_path = Path(metadata.file_path)

                    if not file_path.exists():
                        health_report["missing_files"].append(metadata.file_id)
                    elif not self.verify_file_integrity(metadata.file_id):
                        health_report["corrupted_files"].append(metadata.file_id)

            except Exception:
                health_report["database_accessible"] = False

            # 孤立ファイル数をカウント
            if health_report["upload_dir_exists"]:
                registered_files = {Path(f.file_path).name for f in all_files}
                orphaned_count = 0

                for file_path in self.upload_dir.glob("*"):
                    if file_path.is_file() and file_path.name not in registered_files:
                        orphaned_count += 1

                health_report["orphaned_files"] = orphaned_count

            return health_report

        except Exception as e:
            return {
                "error": f"健全性チェック中にエラーが発生しました: {str(e)}",
                "upload_dir_exists": False,
                "upload_dir_writable": False,
                "database_accessible": False,
                "orphaned_files": 0,
                "corrupted_files": [],
                "missing_files": [],
                "total_files": 0,
            }

    # プライベートメソッド

    def _check_duplicate_file(
        self, filename: str, file_content: bytes
    ) -> Optional[FileMetadata]:
        """
        重複ファイルをチェックする

        Args:
            filename: ファイル名
            file_content: ファイル内容

        Returns:
            FileMetadata: 重複ファイルが見つかった場合のメタデータ（見つからない場合はNone）
        """
        try:
            # ファイルハッシュを計算
            import hashlib

            file_hash = hashlib.sha256(file_content).hexdigest()
            file_size = len(file_content)

            all_files = self.db_service.get_all_uploaded_files()

            # Filter for matching files
            matching_files = [
                f
                for f in all_files
                if f.get("original_filename", f.get("filename")) == filename
                and f.get("file_size") == file_size
                and f.get("file_hash") == file_hash
            ]

            if not matching_files:
                return None

            # Sort by uploaded_at and get the most recent
            matching_files.sort(key=lambda x: x["uploaded_at"], reverse=True)
            file_info = matching_files[0]

            # ファイルが実際に存在するかチェック
            file_path = Path(file_info["file_path"])
            if not file_path.exists():
                # S3パスの場合は存在チェックをスキップ
                if not file_info["file_path"].startswith("s3://"):
                    # ローカルファイルが存在しない場合はメタデータを削除
                    self.db_service.delete_uploaded_file_info(file_info["id"])
                    return None

            # メタデータオブジェクトを作成して返す
            return FileMetadata(
                file_id=file_info["id"],
                original_filename=file_info.get(
                    "original_filename", file_info["filename"]
                ),
                saved_filename=file_info["filename"],
                file_path=file_info["file_path"],
                file_size=file_info.get("file_size", 0),
                file_hash=file_info.get("file_hash", ""),
                mime_type=file_info.get("mime_type", "text/plain"),
                uploaded_at=file_info["uploaded_at"],
            )

        except Exception:
            # エラーが発生した場合は重複なしとして処理を続行
            return None

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
            raise FileSecurityError("ファイル名に不正な文字が含まれています。")

        # ファイル名の長さチェック
        if len(filename) > 255:
            raise FileSecurityError("ファイル名が長すぎます。")

        # 隠しファイルのチェック
        if filename.startswith("."):
            raise FileSecurityError("隠しファイルはアップロードできません。")

        # バイナリファイルの簡易チェック（NULL文字の存在）
        # allow_binaryがTrueの場合はスキップ
        if not allow_binary and b"\x00" in file_content[:1024]:  # 最初の1KBをチェック
            raise FileSecurityError("バイナリファイルはアップロードできません。")

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
            raise FileUploadError(f"ファイル保存中にエラーが発生しました: {str(e)}")

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
                f"ファイルメタデータ保存中にエラーが発生しました: {str(e)}"
            )

    def _get_file_metadata_by_id(self, file_id: str) -> Optional[FileMetadata]:
        """
        ファイルIDでメタデータを取得する

        Args:
            file_id: ファイルID

        Returns:
            FileMetadata: ファイルメタデータ（見つからない場合はNone）
        """
        try:
            file_info = self.db_service.get_uploaded_file_info(file_id)
            if file_info is None:
                return None

            # Handle uploaded_at - convert string to datetime if needed
            uploaded_at = file_info["uploaded_at"]
            if isinstance(uploaded_at, str):
                uploaded_at = datetime.fromisoformat(uploaded_at)

            return FileMetadata(
                file_id=file_info["id"],
                original_filename=file_info.get(
                    "original_filename", file_info["filename"]
                ),
                saved_filename=file_info["filename"],
                file_path=file_info["file_path"],
                file_size=file_info.get("file_size", 0),
                file_hash=file_info.get("file_hash", ""),
                mime_type=file_info.get("mime_type", "text/plain"),
                uploaded_at=uploaded_at,
            )
        except Exception as e:
            raise DatabaseError(
                f"ファイルメタデータ取得中にエラーが発生しました: {str(e)}"
            )

    def _get_all_file_metadata(self) -> List[FileMetadata]:
        """
        すべてのファイルメタデータを取得する

        Returns:
            List[FileMetadata]: ファイルメタデータのリスト
        """
        try:
            files = self.db_service.get_all_uploaded_files()
            metadata_list = []
            for file_info in files:
                metadata = FileMetadata(
                    file_id=file_info["id"],
                    original_filename=file_info.get(
                        "original_filename", file_info["filename"]
                    ),
                    saved_filename=file_info["filename"],
                    file_path=file_info["file_path"],
                    file_size=file_info.get("file_size", 0),
                    file_hash=file_info.get("file_hash", ""),
                    mime_type=file_info.get("mime_type", "text/plain"),
                    uploaded_at=file_info["uploaded_at"],
                )
                metadata_list.append(metadata)
            # Sort by uploaded_at descending
            metadata_list.sort(key=lambda x: x.uploaded_at, reverse=True)
            return metadata_list
        except Exception as e:
            raise DatabaseError(
                f"ファイルメタデータ一覧取得中にエラーが発生しました: {str(e)}"
            )
