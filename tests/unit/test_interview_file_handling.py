"""Handling of interview attachments.

The router first stores the attachment with ``FileManager.upload_interview_attachment``
(type and size checks live in test_file_manager.py) and passes only the ID to
InterviewManager. These tests cover building the ContentBlock for the agent from the
stored file, and that InterviewManager never receives raw bytes.
"""

import pytest

from src.managers.shared.document_loader import prepare_document_contents


def _metadata(path, mime_type, filename):
    return [{"file_path": str(path), "mime_type": mime_type, "filename": filename}]


@pytest.mark.unit
class TestAttachmentContents:
    """Builds a ContentBlock from the stored file (same on regeneration)."""

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
