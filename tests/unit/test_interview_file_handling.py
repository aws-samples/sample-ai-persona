"""インタビューの添付の扱い（U6 BR3.6）。

U6 からは、添付を Router が先に ``FileManager.upload_interview_attachment`` で置き
（種類と大きさの検査は test_file_manager.py）、InterviewManager には ID だけを渡す。
ここでは、置いたファイルからエージェントに渡す ContentBlock を作る部分と、
InterviewManager が生のバイト列を受け取らないことを確かめる。
"""

import pytest

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


def _image_bytes(size, fmt="PNG", noise=False):
    import io
    import os

    from PIL import Image

    if noise:
        img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        img = Image.new("RGB", size, (200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _decoded_size(data):
    import io

    from PIL import Image

    return Image.open(io.BytesIO(data)).size


@pytest.mark.unit
class TestImageFitsModelLimits:
    """画像は Bedrock の上限に収めてから渡す（Issue #217）

    Bedrock 上の Claude は 1 枚 3.75 MB（base64 後 5 MB）を超える画像を拒否し、
    1 リクエストの画像が 20 枚を超えると長辺 2000 px を超える画像も拒否する。
    """

    def test_large_image_is_shrunk_under_byte_limit(self, tmp_path):
        from src.managers.shared.file_utils import MODEL_IMAGE_MAX_BYTES

        raw = _image_bytes((1800, 1800), noise=True)
        assert len(raw) > MODEL_IMAGE_MAX_BYTES
        path = tmp_path / "big.png"
        path.write_bytes(raw)

        (block,) = prepare_document_contents(_metadata(path, "image/png", "big.png"))

        data = block["image"]["source"]["bytes"]
        assert len(data) <= MODEL_IMAGE_MAX_BYTES
        assert block["image"]["format"] == "jpeg"
        _decoded_size(data)

    def test_long_side_is_capped(self, tmp_path):
        from src.managers.shared.file_utils import MODEL_IMAGE_MAX_SIDE

        path = tmp_path / "wide.png"
        path.write_bytes(_image_bytes((6000, 300)))

        (block,) = prepare_document_contents(_metadata(path, "image/png", "wide.png"))

        width, height = _decoded_size(block["image"]["source"]["bytes"])
        assert max(width, height) <= MODEL_IMAGE_MAX_SIDE
        assert width / height == pytest.approx(20, rel=0.05)

    def test_image_within_limits_is_passed_unchanged(self, tmp_path):
        raw = _image_bytes((800, 600))
        path = tmp_path / "small.png"
        path.write_bytes(raw)

        (block,) = prepare_document_contents(_metadata(path, "image/png", "small.png"))

        assert block["image"]["source"]["bytes"] == raw
        assert block["image"]["format"] == "png"

    def test_transparent_image_is_flattened_on_white(self, tmp_path):
        import io

        from PIL import Image

        img = Image.new("RGBA", (4000, 100), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        path = tmp_path / "clear.png"
        path.write_bytes(buf.getvalue())

        (block,) = prepare_document_contents(_metadata(path, "image/png", "clear.png"))

        out = Image.open(io.BytesIO(block["image"]["source"]["bytes"]))
        assert out.convert("RGB").getpixel((0, 0)) > (240, 240, 240)

    def test_undecodable_image_over_limit_is_skipped(self, tmp_path):
        from src.managers.shared.file_utils import MODEL_IMAGE_MAX_BYTES

        path = tmp_path / "broken.png"
        path.write_bytes(b"\x89PNG\r\n" + b"\0" * MODEL_IMAGE_MAX_BYTES)

        assert (
            prepare_document_contents(_metadata(path, "image/png", "broken.png")) == []
        )
