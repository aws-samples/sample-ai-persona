"""ファイル操作ユーティリティ（純粋関数 — I/Oなし）

テキスト抽出、エンコーディング検出、CSV解析、一時ファイル管理を提供する。
"""

import csv
import io
import os
import re
import uuid
from pathlib import Path

from ...models.dataset import DatasetColumn


def detect_encoding(content: bytes) -> str:
    """バイト列のエンコーディングを検出する。検出失敗時はValueErrorを送出。"""
    for encoding in ("utf-8", "shift_jis", "euc-jp"):
        try:
            content.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("エンコーディングを検出できません（UTF-8, Shift_JIS, EUC-JP以外）")


def extract_text_from_bytes(content: bytes, filename: str) -> str:
    """ファイル内容からテキストを抽出する。

    PDF/DOCX/PPTX はmarkitdownで変換、テキスト系はデコードする。
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
    """CSVバイト列から先頭N行のプレビュー文字列を生成する。"""
    encoding = detect_encoding(content)
    decoded = content.decode(encoding)
    lines = decoded.splitlines()
    preview = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        preview += f"\n... (全{len(lines)}行)"
    return preview


def save_temp_csv(content: bytes) -> str:
    """CSVをUTF-8で/tmpに保存し、パスを返す。"""
    encoding = detect_encoding(content)
    decoded = content.decode(encoding)
    csv_path = f"/tmp/persona_csv_{uuid.uuid4().hex[:8]}.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(decoded)
    return csv_path


def cleanup_temp_files(paths: list[str]) -> None:
    """一時ファイルを削除する（存在しなくてもエラーにしない）。"""
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def analyze_csv_schema(
    csv_bytes: bytes, sample_rows: int = 100
) -> tuple[list[DatasetColumn], int]:
    """CSVバイト列からスキーマ（カラム情報）と行数を解析する。"""
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))

    headers = next(reader, [])
    if not headers:
        return [], 0

    rows: list[list[str]] = []
    for i, row in enumerate(reader):
        if i >= sample_rows:
            break
        rows.append(row)

    row_count = len(rows)
    for _ in reader:
        row_count += 1

    columns: list[DatasetColumn] = []
    for idx, header in enumerate(headers):
        values = [row[idx] if idx < len(row) else "" for row in rows]
        data_type = _infer_column_type(values)
        columns.append(DatasetColumn(name=header, data_type=data_type))

    return columns, row_count


def detect_binding_key(columns: list[str], csv_bytes: bytes) -> tuple[str, str]:
    """CSVから識別キーカラムと先頭行の値を検出する。"""
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
    """CSVカラム名からデータ種別を推定する（全カラムのヒット数で最多種別を返す）。"""
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


def _infer_column_type(values: list[str]) -> str:
    """値リストからカラムの型を推定する。"""
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
