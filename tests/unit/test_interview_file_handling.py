"""InterviewManager ファイル処理統合のテスト。"""

import pytest
from unittest.mock import Mock, patch

from src.managers.interview_manager import (
    InterviewManager,
    InterviewValidationError,
)


@pytest.mark.unit
class TestValidateAndConvertFiles:
    """_validate_and_convert_files のテスト"""

    def setup_method(self):
        self.manager = InterviewManager(agent_service=Mock(), database_service=Mock())

    def test_valid_png(self):
        """PNG画像が正しく変換されること"""
        raw_files = [(b"\x89PNG\r\n", "image.png", "image/png")]
        contents, metadata = self.manager._validate_and_convert_files(raw_files)

        assert len(contents) == 1
        assert "image" in contents[0]
        assert contents[0]["image"]["format"] == "png"
        assert len(metadata) == 1
        assert metadata[0]["filename"] == "image.png"
        assert metadata[0]["mime_type"] == "image/png"

    def test_valid_pdf(self):
        """PDFが正しく変換されること"""
        raw_files = [(b"%PDF-1.4", "doc.pdf", "application/pdf")]
        contents, metadata = self.manager._validate_and_convert_files(raw_files)

        assert len(contents) == 1
        assert "document" in contents[0]
        assert contents[0]["document"]["format"] == "pdf"

    def test_valid_text(self):
        """テキストファイルが正しく変換されること"""
        raw_files = [(b"hello world", "note.txt", "text/plain")]
        contents, metadata = self.manager._validate_and_convert_files(raw_files)

        assert len(contents) == 1
        assert contents[0]["document"]["format"] == "txt"

    def test_unsupported_mime_raises(self):
        """未サポートMIMEでInterviewValidationErrorが投げられること"""
        raw_files = [(b"data", "file.zip", "application/zip")]

        with pytest.raises(InterviewValidationError, match="サポートされていません"):
            self.manager._validate_and_convert_files(raw_files)

    def test_oversized_image_raises(self):
        """画像サイズ超過でInterviewValidationErrorが投げられること"""
        large_bytes = b"\x89PNG" * (6 * 1024 * 1024)  # >5MB
        raw_files = [(large_bytes, "big.png", "image/png")]

        with pytest.raises(InterviewValidationError, match="大きすぎます"):
            self.manager._validate_and_convert_files(raw_files)

    def test_oversized_document_raises(self):
        """ドキュメントサイズ超過でInterviewValidationErrorが投げられること"""
        large_bytes = b"%PDF" * (11 * 1024 * 1024)  # >10MB
        raw_files = [(large_bytes, "big.pdf", "application/pdf")]

        with pytest.raises(InterviewValidationError, match="大きすぎます"):
            self.manager._validate_and_convert_files(raw_files)

    def test_empty_file_skipped(self):
        """空ファイルがスキップされること"""
        raw_files = [(b"", "empty.txt", "text/plain")]
        contents, metadata = self.manager._validate_and_convert_files(raw_files)

        assert len(contents) == 0
        assert len(metadata) == 0

    def test_multiple_files(self):
        """複数ファイルが正しく処理されること"""
        raw_files = [
            (b"\x89PNG", "img.png", "image/png"),
            (b"%PDF-1.4", "doc.pdf", "application/pdf"),
            (b"text content", "note.txt", "text/plain"),
        ]
        contents, metadata = self.manager._validate_and_convert_files(raw_files)

        assert len(contents) == 3
        assert len(metadata) == 3


@pytest.mark.unit
class TestSendUserMessageWithFiles:
    """send_user_message_with_files のテスト"""

    def setup_method(self):
        self.mock_agent_service = Mock()
        self.mock_agent_service.create_persona_agent_with_integrations.return_value = (
            Mock()
        )
        self.manager = InterviewManager(
            agent_service=self.mock_agent_service, database_service=Mock()
        )

    def test_delegates_to_send_user_message(self):
        """send_user_message に正しく委譲すること"""
        with patch.object(self.manager, "send_user_message") as mock_send:
            mock_send.return_value = []
            self.manager.send_user_message_with_files(
                "session-1", "hello", raw_files=None
            )
            mock_send.assert_called_once_with(
                session_id="session-1",
                message="hello",
                document_contents=None,
                document_metadata=None,
            )

    def test_validation_error_on_bad_file(self):
        """不正ファイルでInterviewValidationErrorが投げられること"""
        with pytest.raises(InterviewValidationError):
            self.manager.send_user_message_with_files(
                "session-1",
                "hello",
                raw_files=[(b"data", "file.exe", "application/octet-stream")],
            )
