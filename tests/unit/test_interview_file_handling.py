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
        # 縮める先は長辺の上限そのもの（Claude 4.7 以降は長辺 2576 px まで読む）
        assert max(width, height) == MODEL_IMAGE_MAX_SIDE
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


@pytest.mark.unit
class TestImageFormatAndDecodeLimits:
    """形式は中身で決め、展開が重すぎる画像は外す（#217 のレビュー指摘）"""

    def test_format_follows_content_not_extension(self, tmp_path):
        raw = _image_bytes((400, 300), fmt="JPEG")
        path = tmp_path / "photo.png"
        path.write_bytes(raw)

        (block,) = prepare_document_contents(_metadata(path, "image/png", "photo.png"))

        assert block["image"]["format"] == "jpeg"
        assert block["image"]["source"]["bytes"] == raw

    def test_unsupported_format_is_reencoded(self, tmp_path):
        path = tmp_path / "scan.bmp"
        path.write_bytes(_image_bytes((400, 300), fmt="BMP"))

        (block,) = prepare_document_contents(_metadata(path, "image/bmp", "scan.bmp"))

        assert block["image"]["format"] == "jpeg"
        assert _decoded_size(block["image"]["source"]["bytes"]) == (400, 300)

    def test_image_over_pixel_limit_is_skipped(self, tmp_path, monkeypatch):
        from src.managers.shared import file_utils

        monkeypatch.setattr(file_utils, "MODEL_IMAGE_MAX_SOURCE_PIXELS", 1_000_000)
        path = tmp_path / "huge.png"
        path.write_bytes(_image_bytes((2500, 500)))

        assert prepare_document_contents(_metadata(path, "image/png", "huge.png")) == []

    def test_large_jpeg_is_shrunk(self, tmp_path):
        from src.managers.shared.file_utils import MODEL_IMAGE_MAX_SIDE

        path = tmp_path / "big.jpg"
        path.write_bytes(_image_bytes((5000, 4000), fmt="JPEG"))

        (block,) = prepare_document_contents(_metadata(path, "image/jpeg", "big.jpg"))

        width, height = _decoded_size(block["image"]["source"]["bytes"])
        assert max(width, height) <= MODEL_IMAGE_MAX_SIDE
        assert width / height == pytest.approx(1.25, rel=0.02)


def _jpeg_with_orientation(size, orientation):
    import io

    from PIL import Image

    img = Image.new("RGB", size, (200, 100, 50))
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


@pytest.mark.unit
class TestImageAccuracy:
    """モデルが正しく読めるように渡す（#217 の追加対応）"""

    def test_exif_orientation_is_applied(self, tmp_path):
        path = tmp_path / "portrait.jpg"
        path.write_bytes(_jpeg_with_orientation((300, 200), 6))

        (block,) = prepare_document_contents(
            _metadata(path, "image/jpeg", "portrait.jpg")
        )

        assert _decoded_size(block["image"]["source"]["bytes"]) == (200, 300)

    def test_upright_jpeg_with_exif_is_passed_unchanged(self, tmp_path):
        raw = _jpeg_with_orientation((300, 200), 1)
        path = tmp_path / "upright.jpg"
        path.write_bytes(raw)

        (block,) = prepare_document_contents(
            _metadata(path, "image/jpeg", "upright.jpg")
        )

        assert block["image"]["source"]["bytes"] == raw

    def test_camera_mpo_is_passed_as_jpeg(self, tmp_path):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (300, 200), (1, 2, 3)).save(
            buf,
            format="MPO",
            save_all=True,
            append_images=[Image.new("RGB", (300, 200))],
        )
        path = tmp_path / "camera.jpg"
        path.write_bytes(buf.getvalue())

        (block,) = prepare_document_contents(
            _metadata(path, "image/jpeg", "camera.jpg")
        )

        assert block["image"]["format"] == "jpeg"
        assert block["image"]["source"]["bytes"] == buf.getvalue()

    def test_jpeg_has_a_higher_pixel_limit(self, tmp_path, monkeypatch):
        from src.managers.shared import file_utils

        monkeypatch.setattr(file_utils, "MODEL_IMAGE_MAX_SOURCE_PIXELS", 1_000_000)
        monkeypatch.setattr(file_utils, "MODEL_IMAGE_MAX_JPEG_SOURCE_PIXELS", 4_000_000)
        path = tmp_path / "phone.jpg"
        path.write_bytes(_image_bytes((2500, 1000), fmt="JPEG"))

        (block,) = prepare_document_contents(_metadata(path, "image/jpeg", "phone.jpg"))

        width, _ = _decoded_size(block["image"]["source"]["bytes"])
        assert width == file_utils.MODEL_IMAGE_MAX_SIDE
