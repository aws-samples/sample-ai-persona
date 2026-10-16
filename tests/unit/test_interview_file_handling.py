"""インタビューの添付の扱い（U6 BR3.6）。

U6 からは、添付を Router が先に ``FileManager.upload_interview_attachment`` で置き
（種類と大きさの検査は test_file_manager.py）、InterviewManager には ID だけを渡す。
ここでは、置いたファイルからエージェントに渡す ContentBlock を作る部分と、
InterviewManager が生のバイト列を受け取らないことを確かめる。
"""

import pytest

from src.managers.interview_manager import InterviewManager
from src.managers.shared.document_loader import prepare_document_contents


def _metadata(path, mime_type, filename):
    return [{"file_path": str(path), "mime_type": mime_type, "filename": filename}]


@pytest.mark.unit
class TestAttachmentContents:
    """保存したファイルから ContentBlock を作る（作り直しのときも同じ）"""

    def test_png(self, tmp_path):
        path = tmp_path / "image.png"
        path.write_bytes(b"\x89PNG\r\n")
        (block,) = prepare_document_contents(_metadata(path, "image/png", "image.png"))
        assert block["image"]["format"] == "png"

    def test_pdf(self, tmp_path):
        path = tmp_path / "doc.pdf"
        path.write_bytes(b"%PDF-1.4")
        (block,) = prepare_document_contents(
            _metadata(path, "application/pdf", "doc.pdf")
        )
        assert block["document"]["format"] == "pdf"

    def test_text(self, tmp_path):
        path = tmp_path / "note.txt"
        path.write_bytes(b"hello world")
        (block,) = prepare_document_contents(_metadata(path, "text/plain", "note.txt"))
        assert block["document"]["format"] == "txt"

    def test_missing_file_is_skipped(self, tmp_path):
        assert (
            prepare_document_contents(
                _metadata(tmp_path / "gone.png", "image/png", "gone.png")
            )
            == []
        )


@pytest.mark.unit
class TestNoRawBytesInTheManager:
    def test_the_raw_file_paths_are_gone(self):
        for name in (
            "_validate_and_convert_files",
            "send_user_message_with_files",
            "send_user_message_streaming_with_files",
        ):
            assert not hasattr(InterviewManager, name)
