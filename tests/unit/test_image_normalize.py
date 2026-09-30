"""アイコン画像の正規化（Pillow での再エンコード）のテスト"""

from io import BytesIO

import pytest
from PIL import Image

from src.managers.shared import image_normalize
from src.managers.shared.image_normalize import (
    AVATAR_SIZE,
    AvatarImageError,
    normalize_avatar_image,
)


def _image_bytes(fmt: str, size: tuple[int, int] = (320, 200), **save_kwargs) -> bytes:
    buf = BytesIO()
    mode = "RGB" if fmt == "JPEG" else "RGBA"
    Image.new(mode, size, (200, 30, 30)).save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


class TestNormalizeAvatarImage:
    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
    def test_allowed_formats_become_square_webp(self, fmt):
        result = normalize_avatar_image(_image_bytes(fmt))

        with Image.open(BytesIO(result)) as img:
            assert img.format == "WEBP"
            assert img.size == (AVATAR_SIZE, AVATAR_SIZE)

    def test_exif_metadata_is_stripped(self):
        exif = Image.Exif()
        exif[0x010F] = "CameraMaker"  # Make
        exif[0x8825] = {2: (35.0, 41.0, 0.0)}  # GPSInfo: GPSLatitude
        source = _image_bytes("JPEG", exif=exif.tobytes())
        with Image.open(BytesIO(source)) as src:
            assert src.getexif()  # 前提: 元画像には EXIF がある

        result = normalize_avatar_image(source)

        with Image.open(BytesIO(result)) as img:
            assert not img.getexif()
            assert "exif" not in img.info
            assert "icc_profile" not in img.info

    @pytest.mark.parametrize(
        "content",
        [
            b"not an image at all",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,  # 壊れたPNG
        ],
        ids=["text", "svg", "truncated-png"],
    )
    def test_undecodable_content_is_rejected(self, content):
        with pytest.raises(AvatarImageError):
            normalize_avatar_image(content)

    def test_disallowed_format_is_rejected(self):
        with pytest.raises(AvatarImageError):
            normalize_avatar_image(_image_bytes("GIF"))

    def test_too_many_pixels_is_rejected(self, monkeypatch):
        monkeypatch.setattr(image_normalize, "MAX_AVATAR_SOURCE_PIXELS", 100)

        with pytest.raises(AvatarImageError):
            normalize_avatar_image(_image_bytes("PNG", size=(20, 20)))
