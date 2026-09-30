"""
Avatar image normalization.

Uploaded bytes are never stored as-is: they are decoded with Pillow and
re-encoded, so that disguised files, embedded metadata (EXIF/GPS) and
oversized images never reach storage.
"""

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

#: Formats accepted after decoding (the file extension is not trusted).
ALLOWED_AVATAR_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
#: Upper bound on decoded pixels, checked before the pixel data is loaded.
MAX_AVATAR_SOURCE_PIXELS = 40_000_000
AVATAR_SIZE = 256
AVATAR_MIME_TYPE = "image/webp"
AVATAR_FILE_EXTENSION = ".webp"


class AvatarImageError(ValueError):
    """Raised when uploaded bytes cannot be normalized into an avatar image."""


def normalize_avatar_image(content: bytes) -> bytes:
    """
    Decode an uploaded image and re-encode it as a square WebP avatar.

    Args:
        content: Raw uploaded bytes.

    Returns:
        WebP bytes of an ``AVATAR_SIZE`` x ``AVATAR_SIZE`` image without metadata.

    Raises:
        AvatarImageError: If the bytes are not an allowed, decodable image.
    """
    try:
        with Image.open(BytesIO(content)) as img:
            if img.format not in ALLOWED_AVATAR_FORMATS:
                raise AvatarImageError(f"format {img.format!r} is not allowed")
            width, height = img.size
            if width * height > MAX_AVATAR_SOURCE_PIXELS:
                raise AvatarImageError(f"image is too large ({width}x{height})")
            # Animated WebP: only the first frame is used
            img.seek(0)
            img.load()
            oriented = ImageOps.exif_transpose(img).convert("RGBA")
    except AvatarImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise AvatarImageError(f"image decode failed ({type(e).__name__})") from e

    square = ImageOps.fit(
        oriented, (AVATAR_SIZE, AVATAR_SIZE), method=Image.Resampling.LANCZOS
    )
    out = BytesIO()
    # No exif/icc_profile arguments: the re-encoded file carries no metadata
    square.save(out, format="WEBP", quality=85)
    return out.getvalue()
