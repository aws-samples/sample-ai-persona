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


def _decode(webp: bytes) -> Image.Image:
    img = Image.open(BytesIO(webp))
    img.load()
    return img.convert("RGB")


class TestOrientation:
    @pytest.mark.parametrize("orientation", range(1, 9))
    def test_matches_exif_transpose_then_fit(self, orientation):
        """縮小後に回転補正しても、回転補正してから縮小した結果と一致すること"""
        from PIL import ImageOps

        # 4象限を塗り分けた横長画像（回転・反転の取り違えを色で検出する）
        src = Image.new("RGB", (400, 200))
        for box, color in [
            ((0, 0, 200, 100), (255, 0, 0)),
            ((200, 0, 400, 100), (0, 255, 0)),
            ((0, 100, 200, 200), (0, 0, 255)),
            ((200, 100, 400, 200), (255, 255, 0)),
        ]:
            src.paste(color, box)
        exif = Image.Exif()
        exif[0x0112] = orientation
        buf = BytesIO()
        src.save(buf, format="PNG", exif=exif.tobytes())

        result = _decode(normalize_avatar_image(buf.getvalue()))

        with Image.open(BytesIO(buf.getvalue())) as reopened:
            expected = ImageOps.fit(
                ImageOps.exif_transpose(reopened).convert("RGB"),
                (AVATAR_SIZE, AVATAR_SIZE),
            )
        for xy in [(64, 64), (192, 64), (64, 192), (192, 192)]:
            assert result.getpixel(xy) == pytest.approx(expected.getpixel(xy), abs=8)


class TestColorModes:
    @pytest.mark.parametrize("mode", ["L", "LA", "P", "CMYK"])
    def test_non_rgb_sources_are_normalized(self, mode):
        fmt = "JPEG" if mode == "CMYK" else "PNG"
        buf = BytesIO()
        Image.new(mode, (300, 300)).save(buf, format=fmt)

        with Image.open(BytesIO(normalize_avatar_image(buf.getvalue()))) as img:
            assert img.format == "WEBP"
            assert img.size == (AVATAR_SIZE, AVATAR_SIZE)


class TestMemoryBounds:
    def test_jpeg_is_decoded_at_reduced_scale(self, monkeypatch):
        """大きな JPEG は draft で縮小デコードされ、原寸のビットマップを作らない"""
        from PIL import ImageOps

        fitted_sizes = []
        original_fit = ImageOps.fit

        def spy_fit(image, size, **kwargs):
            fitted_sizes.append(image.size)
            return original_fit(image, size, **kwargs)

        monkeypatch.setattr(image_normalize.ImageOps, "fit", spy_fit)

        normalize_avatar_image(_image_bytes("JPEG", size=(4000, 3000)))

        (decoded,) = fitted_sizes
        assert decoded[0] * decoded[1] < 4000 * 3000 / 4
        assert min(decoded) >= AVATAR_SIZE

    def test_concurrent_decodes_are_limited(self, monkeypatch):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        lock = threading.Lock()
        active = 0
        peak = 0

        def slow_normalize(content):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return b""

        monkeypatch.setattr(image_normalize, "_normalize", slow_normalize)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(normalize_avatar_image, [b""] * 8))

        assert peak == image_normalize.MAX_CONCURRENT_AVATAR_DECODES


class TestPixelLimitsPerFormat:
    def test_jpeg_has_higher_limit_than_png(self, monkeypatch):
        """JPEG は縮小デコードできるため、PNG/WebP より大きな画素数を許す"""
        monkeypatch.setattr(image_normalize, "MAX_AVATAR_SOURCE_PIXELS", 100)

        normalize_avatar_image(_image_bytes("JPEG", size=(20, 20)))
        with pytest.raises(image_normalize.AvatarImageTooLargeError) as exc_info:
            normalize_avatar_image(_image_bytes("PNG", size=(20, 20)))

        assert exc_info.value.max_pixels == 100

    def test_jpeg_over_its_limit_is_rejected(self, monkeypatch):
        monkeypatch.setattr(image_normalize, "MAX_AVATAR_JPEG_SOURCE_PIXELS", 100)

        with pytest.raises(image_normalize.AvatarImageTooLargeError) as exc_info:
            normalize_avatar_image(_image_bytes("JPEG", size=(20, 20)))

        assert exc_info.value.max_pixels == 100

    def test_48mp_phone_photo_is_accepted(self):
        """4,800万画素の JPEG（iPhone 48MP 撮影: 8064x6048）をアップロードできること"""
        buf = BytesIO()
        Image.linear_gradient("L").resize((8064, 6048)).convert("RGB").save(
            buf, format="JPEG", quality=85
        )

        with Image.open(BytesIO(normalize_avatar_image(buf.getvalue()))) as img:
            assert img.size == (AVATAR_SIZE, AVATAR_SIZE)
