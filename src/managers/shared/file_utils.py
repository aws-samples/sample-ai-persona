"""File operation utilities (pure functions, no I/O).

Provides text extraction, encoding detection, CSV parsing and temp file management.
"""

import csv
import io
import os
import re
import uuid
from pathlib import Path
from typing import Any

from ...models.dataset import DatasetColumn


def detect_encoding(content: bytes) -> str:
    """Detect the encoding of a byte string. Raises ValueError if detection fails."""
    for encoding in ("utf-8", "shift_jis", "euc-jp"):
        try:
            content.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("エンコーディングを検出できません（UTF-8, Shift_JIS, EUC-JP以外）")


def extract_text_from_bytes(content: bytes, filename: str) -> str:
    """Extract text from file contents.

    PDF/DOCX/PPTX are converted with markitdown; text formats are decoded.
    """
    file_ext = Path(filename).suffix.lower()

    if file_ext in {".txt", ".md"}:
        encoding = detect_encoding(content)
        return content.decode(encoding)

    if file_ext == ".csv":
        encoding = detect_encoding(content)
        return content.decode(encoding)

    from markitdown import MarkItDown

    md = MarkItDown()
    file_stream = io.BytesIO(content)
    file_stream.name = filename
    result = md.convert_stream(file_stream)
    return result.text_content


def get_csv_preview(content: bytes, max_lines: int = 20) -> str:
    """Build a preview string of the first N rows from CSV bytes."""
    encoding = detect_encoding(content)
    decoded = content.decode(encoding)
    lines = decoded.splitlines()
    preview = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        preview += f"\n... (全{len(lines)}行)"
    return preview


def save_temp_csv(content: bytes) -> str:
    """Save the CSV to /tmp as UTF-8 and return the path."""
    encoding = detect_encoding(content)
    decoded = content.decode(encoding)
    csv_path = f"/tmp/persona_csv_{uuid.uuid4().hex[:8]}.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(decoded)
    return csv_path


def cleanup_temp_files(paths: list[str]) -> None:
    """Delete a temp file (no error if it does not exist)."""
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            continue  # Best-effort delete: ignore missing file or insufficient permission


def normalize_csv_headers(headers: list[str]) -> list[str]:
    """Normalize CSV header names; raise ValueError for names that cannot be queried.

    Strips surrounding whitespace to match DuckDB ``read_csv_auto`` (which trims
    header names). Raises ``ValueError`` when a name is empty/whitespace-only or
    duplicated after stripping: DuckDB renames such columns (``column1`` /
    ``name_1``), so the allowlisted / model-facing name would no longer exist in
    the queryable view and ``analyze_dataset`` would silently fail for that CSV.
    Callers convert this to a coded ``FILE_CSV_HEADER_INVALID`` error.
    """
    normalized = [h.strip() for h in headers]
    if any(not h for h in normalized):
        raise ValueError("CSV header contains an empty column name")
    seen: set[str] = set()
    dups: list[str] = []
    for h in normalized:
        if h in seen and h not in dups:
            dups.append(h)
        seen.add(h)
    if dups:
        raise ValueError(f"CSV header contains duplicate column names: {dups}")
    return normalized


def analyze_csv_schema(
    csv_bytes: bytes, sample_rows: int = 100
) -> tuple[list[DatasetColumn], int]:
    """Parse the schema (column info) and row count from CSV bytes.

    Header names are normalized (surrounding whitespace removed); empty or
    duplicate names raise ValueError.
    """
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))

    headers = next(reader, [])
    if not headers:
        return [], 0
    headers = normalize_csv_headers(headers)

    rows: list[list[str]] = []
    row_count = 0
    for row in reader:
        # csv.reader returns [] for a blank line. DuckDB does not read blank lines as rows, so they are not counted
        if not row:
            continue
        row_count += 1
        if len(rows) < sample_rows:
            rows.append(row)

    columns: list[DatasetColumn] = []
    for idx, header in enumerate(headers):
        values = [row[idx] if idx < len(row) else "" for row in rows]
        data_type = _infer_column_type(values)
        columns.append(DatasetColumn(name=header, data_type=data_type))

    return columns, row_count


def detect_binding_key(columns: list[str], csv_bytes: bytes) -> tuple[str, str]:
    """Detect the identifying key column and first-row values from a CSV."""
    id_candidates = ["user_id", "customer_id", "member_id", "uid", "cid"]
    header_lower = [c.lower().strip() for c in columns]
    key_col = ""
    for candidate in id_candidates:
        if candidate in header_lower:
            key_col = columns[header_lower.index(candidate)]
            break

    if not key_col:
        return "", ""

    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    first_row = next(reader, None)
    if first_row and key_col in first_row:
        return key_col, first_row[key_col]
    return key_col, ""


def infer_behavior_data_type(columns: list[str]) -> str:
    """Infer the data type from CSV column names (return the type with the most column hits)."""
    hints: dict[str, str] = {
        "purchase": "購買履歴",
        "order": "購買履歴",
        "transaction": "購買履歴",
        "buy": "購買履歴",
        "page": "Web行動ログ",
        "click": "Web行動ログ",
        "session": "Web行動ログ",
        "browse": "Web行動ログ",
        "access": "Web行動ログ",
        "inquiry": "問い合わせ履歴",
        "contact": "問い合わせ履歴",
        "support": "問い合わせ履歴",
        "ticket": "問い合わせ履歴",
    }
    scores: dict[str, int] = {}
    for col in columns:
        col_lower = col.lower()
        for keyword, label in hints.items():
            if keyword in col_lower:
                scores[label] = scores.get(label, 0) + 1
                break
    if not scores:
        return ""
    return max(scores, key=scores.get)  # type: ignore[arg-type]


def parse_csv_columns_with_samples(
    csv_bytes: bytes, standard_columns: dict | None = None
) -> dict[str, Any]:
    """Return the CSV column list + sample values + row count + auto-mapping candidates."""
    try:
        import polars as pl

        df_preview = pl.read_csv(
            io.BytesIO(csv_bytes), infer_schema_length=1000, n_rows=5
        )
    except Exception as e:
        raise ValueError(f"CSVの読み込みに失敗しました: {e}") from e

    samples: dict[str, list[str]] = {}
    for col in df_preview.columns:
        vals = df_preview[col].drop_nulls().head(3).to_list()
        samples[col] = [str(v) for v in vals]

    auto_mapping: dict[str, str] = {}
    if standard_columns:
        for col in df_preview.columns:
            col_lower = col.lower().strip()
            for std_col, meta in standard_columns.items():
                if col_lower == std_col or col_lower == meta.get("label", "").lower():
                    auto_mapping[std_col] = col
                    break

    row_count = count_csv_rows(csv_bytes)

    return {
        "columns": df_preview.columns,
        "samples": samples,
        "row_count": row_count,
        "auto_mapping": auto_mapping,
    }


def parse_csv_first_row_with_mapping(
    csv_bytes: bytes, column_mapping: dict[str, str]
) -> dict[str, str]:
    """Return the first CSV row as a dict after applying the mapping (for preview)."""
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    first_row = next(reader, None)
    if first_row is None:
        return {}

    result: dict[str, str] = {}
    reverse_map = {v: k for k, v in column_mapping.items()}
    for csv_col, value in first_row.items():
        mapped_key = reverse_map.get(csv_col, csv_col)
        result[mapped_key] = value or ""
    return result


def count_csv_rows(csv_bytes: bytes) -> int:
    """Return the total number of CSV rows."""
    import polars as pl

    df = pl.read_csv(io.BytesIO(csv_bytes), infer_schema_length=1000)
    return len(df)


def build_results_csv_bytes(headers: list[str], rows: list[list[str]]) -> bytes:
    """Build CSV bytes from CSV headers and row data (UTF-8 with BOM)."""
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")


def parse_results_csv(csv_bytes: bytes) -> list[dict[str, str]]:
    """Parse CSV bytes into the answer data structure."""
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def compress_image_for_batch(
    image_bytes: bytes, max_side: int = 768, quality: int = 50
) -> tuple[bytes, str]:
    """Resize and JPEG-compress an image and return the bytes and media type."""
    from PIL import Image
    from PIL.Image import Resampling

    img: Any = Image.open(io.BytesIO(image_bytes))
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Resampling.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), "image/jpeg"


# Image limits Claude on Bedrock accepts (Issue #217).
# Up to 3.75 MB per image (5 MB after base64). Above 20 images in one request, the
# per-image dimension limit gets stricter. Images pile up in conversation history,
# so always fit the long side to 2000 px.
# Shrink to this long side too (Claude 4.7+ reads up to 2576 px on the long side, so do not go smaller)
MODEL_IMAGE_MAX_BYTES = 3_750_000
MODEL_IMAGE_MAX_SIDE = 2000
_MODEL_IMAGE_QUALITIES = (85, 70, 50, 30)
# Upper limit of pixels to decode. PNG etc. decode at full size, so this caps
# temporary memory (about 100 MiB for 25MP RGBA). JPEG is decoded while shrinking
# (draft), so up to 50MP is accepted, as for avatar images (48-megapixel phone photos included)
MODEL_IMAGE_MAX_SOURCE_PIXELS = 25_000_000
MODEL_IMAGE_MAX_JPEG_SOURCE_PIXELS = 50_000_000
# Format detected from content -> Bedrock image.format. Many camera JPEGs are detected as
# MPO (JPEG with another image appended), but their start reads as a normal JPEG
_MODEL_IMAGE_FORMATS = {
    "PNG": "png",
    "JPEG": "jpeg",
    "MPO": "jpeg",
    "GIF": "gif",
    "WEBP": "webp",
}
_JPEG_FORMATS = frozenset({"JPEG", "MPO"})
# EXIF orientation -> transform that restores the correct orientation. The model does not read metadata, so rotate the pixels
_EXIF_ORIENTATION = 0x0112
_ORIENTATION_TRANSPOSE = {
    2: "FLIP_LEFT_RIGHT",
    3: "ROTATE_180",
    4: "FLIP_TOP_BOTTOM",
    5: "TRANSPOSE",
    6: "ROTATE_270",
    7: "TRANSVERSE",
    8: "ROTATE_90",
}


def fit_image_for_model(
    image_bytes: bytes, image_format: str
) -> tuple[bytes, str] | None:
    """Return the image prepared for the model as ``(bytes, format)``.

    The format is taken from the decoded content, not from ``image_format``
    (which comes from the file extension). An upright image within the limits
    in a format the model accepts is returned unchanged. Any other image is
    resized to fit ``MODEL_IMAGE_MAX_SIDE``, turned upright according to its
    EXIF orientation (the model does not read metadata) and re-encoded as JPEG.
    An image with more pixels than the source limit is not decoded and
    ``None`` is returned. An image that cannot be decoded is returned unchanged
    when it is small enough (the model decides), otherwise ``None``.
    """
    from PIL import Image
    from PIL.Image import Resampling, Transpose

    try:
        # Image.open reads only the header: size and format, no pixel data
        img: Any = Image.open(io.BytesIO(image_bytes))
        size = img.size
        source_format = img.format or ""
    except Exception:
        return _passthrough_if_small(image_bytes, image_format)
    # Checked before anything that may decode the pixel data
    max_pixels = (
        MODEL_IMAGE_MAX_JPEG_SOURCE_PIXELS
        if source_format in _JPEG_FORMATS
        else MODEL_IMAGE_MAX_SOURCE_PIXELS
    )
    if size[0] * size[1] > max_pixels:
        return None
    try:
        transpose = _ORIENTATION_TRANSPOSE.get(_header_orientation(img))
    except Exception:
        return _passthrough_if_small(image_bytes, image_format)
    decoded_format = _MODEL_IMAGE_FORMATS.get(source_format)
    if (
        decoded_format is not None
        and transpose is None
        and len(image_bytes) <= MODEL_IMAGE_MAX_BYTES
        and max(size) <= MODEL_IMAGE_MAX_SIDE
    ):
        return image_bytes, decoded_format

    try:
        # JPEG only (no-op otherwise): decode at a reduced DCT scale
        img.draft("RGB", (MODEL_IMAGE_MAX_SIDE, MODEL_IMAGE_MAX_SIDE))
        img.thumbnail((MODEL_IMAGE_MAX_SIDE, MODEL_IMAGE_MAX_SIDE), Resampling.LANCZOS)
        if transpose is not None:
            img = img.transpose(Transpose[transpose])
        img = _flatten_for_jpeg(img)
        for quality in _MODEL_IMAGE_QUALITIES:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= MODEL_IMAGE_MAX_BYTES:
                return buf.getvalue(), "jpeg"
    except Exception:
        return _passthrough_if_small(image_bytes, image_format)
    return None


def _header_orientation(img: Any) -> int:
    """The EXIF orientation (1 = upright), read only when it came with the header.

    Pillow's PNG ``getexif()`` decodes the whole image to look for an EXIF chunk
    after the pixel data, so it is called only when the EXIF block was already
    read on open (JPEG, MPO and WebP keep it before the pixel data).
    """
    if "exif" not in img.info:
        return 1
    orientation = img.getexif().get(_EXIF_ORIENTATION)
    return orientation if isinstance(orientation, int) else 1


def _passthrough_if_small(
    image_bytes: bytes, image_format: str
) -> tuple[bytes, str] | None:
    if len(image_bytes) <= MODEL_IMAGE_MAX_BYTES:
        return image_bytes, image_format
    return None


def _flatten_for_jpeg(img: Any) -> Any:
    """Convert to a JPEG-compatible mode, putting transparency on white."""
    from PIL import Image

    if img.mode in ("RGB", "L"):
        return img
    if img.mode in ("RGBA", "LA", "P", "PA"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def _infer_column_type(values: list[str]) -> str:
    """Infer a column type from a list of values."""
    non_empty = [v for v in values if v.strip()]
    if not non_empty:
        return "string"

    if all(_is_int(v) for v in non_empty):
        return "integer"
    if all(_is_float(v) for v in non_empty):
        return "float"
    if all(bool(re.match(r"^\d{4}-\d{2}-\d{2}", v)) for v in non_empty):
        return "date"
    return "string"


def _is_int(v: str) -> bool:
    try:
        int(v.replace(",", ""))
        return True
    except ValueError:
        return False


def _is_float(v: str) -> bool:
    try:
        float(v.replace(",", ""))
        return True
    except ValueError:
        return False
