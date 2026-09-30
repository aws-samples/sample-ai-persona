"""
Avatar image normalization.

Uploaded bytes are never stored as-is: they are decoded with Pillow and
re-encoded, so that disguised files, embedded metadata (EXIF/GPS) and
oversized images never reach storage.

Memory use is bounded because a small file can still expand into a very large
bitmap (a flat-colour PNG under 5 MiB can hold tens of megapixels):

- the source pixel count is capped before decoding,
- JPEGs are decoded at a reduced scale (``draft``),
- the image is cropped and resized before any full-size mode conversion or
  rotation, so those run on the 256px result instead of the source,
- the number of concurrent decodes is limited process-wide.
"""

import threading
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

#: Formats accepted after decoding (the file extension is not trusted).
ALLOWED_AVATAR_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
#: Upper bound on source pixels, checked before the pixel data is loaded.
#: PNG/WebP decode at full size, so they are held to 16MP (~67 MiB peak).
MAX_AVATAR_SOURCE_PIXELS = 16_000_000
#: JPEGs decode at a reduced DCT scale (``draft``), so a far larger source costs
#: little memory: 48MP baseline ~6 MiB, progressive ~145 MiB peak (measured).
#: 50MP admits 48MP phone photos (e.g. iPhone 8064x6048).
MAX_AVATAR_JPEG_SOURCE_PIXELS = 50_000_000
#: Upper bound on decodes running at the same time across all requests.
MAX_CONCURRENT_AVATAR_DECODES = 2
AVATAR_SIZE = 256
AVATAR_MIME_TYPE = "image/webp"
AVATAR_FILE_EXTENSION = ".webp"

_decode_slots = threading.BoundedSemaphore(MAX_CONCURRENT_AVATAR_DECODES)

_EXIF_ORIENTATION = 0x0112
# EXIF orientation -> transpose that makes the image upright (as ImageOps.exif_transpose)
_ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


class AvatarImageError(ValueError):
    """Raised when uploaded bytes cannot be normalized into an avatar image."""


class AvatarImageTooLargeError(AvatarImageError):
    """Raised when the source image exceeds the pixel limit for its format."""

    def __init__(self, message: str, max_pixels: int):
        super().__init__(message)
        #: The limit that was applied (depends on the format)
        self.max_pixels = max_pixels


def max_source_pixels(image_format: str | None) -> int:
    """Return the source pixel limit for a decoded image format."""
    if image_format == "JPEG":
        return MAX_AVATAR_JPEG_SOURCE_PIXELS
    return MAX_AVATAR_SOURCE_PIXELS


def normalize_avatar_image(content: bytes) -> bytes:
    """
    Decode an uploaded image and re-encode it as a square WebP avatar.

    Blocks while ``MAX_CONCURRENT_AVATAR_DECODES`` other decodes are running.

    Args:
        content: Raw uploaded bytes.

    Returns:
        WebP bytes of an ``AVATAR_SIZE`` x ``AVATAR_SIZE`` image without metadata.

    Raises:
        AvatarImageError: If the bytes are not an allowed, decodable image.
    """
    with _decode_slots:
        return _normalize(content)


def _normalize(content: bytes) -> bytes:
    try:
        with Image.open(BytesIO(content)) as img:
            if img.format not in ALLOWED_AVATAR_FORMATS:
                raise AvatarImageError(f"format {img.format!r} is not allowed")
            width, height = img.size
            max_pixels = max_source_pixels(img.format)
            if width * height > max_pixels:
                raise AvatarImageTooLargeError(
                    f"image is too large ({width}x{height})", max_pixels
                )
            orientation = img.getexif().get(_EXIF_ORIENTATION)
            # Animated WebP: only the first frame is used
            img.seek(0)
            # JPEG only (no-op otherwise): decode at the smallest DCT scale that
            # still keeps both sides at least twice the avatar size
            img.draft(None, (AVATAR_SIZE * 2, AVATAR_SIZE * 2))
            img.load()
            source = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA")
            square = ImageOps.fit(
                source, (AVATAR_SIZE, AVATAR_SIZE), method=Image.Resampling.LANCZOS
            )
    except AvatarImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise AvatarImageError(f"image decode failed ({type(e).__name__})") from e

    # A centred square crop commutes with rotation/flip, so orientation can be
    # corrected on the small result instead of the full-size source
    if orientation in _ORIENTATION_TRANSPOSE:
        square = square.transpose(_ORIENTATION_TRANSPOSE[orientation])

    out = BytesIO()
    # No exif/icc_profile arguments: the re-encoded file carries no metadata
    square.save(out, format="WEBP", quality=85)
    return out.getvalue()
