"""Make a large made-up Shift_JIS CSV for the 1GB manual E2E (BR20.2).

The values are random numbers and a fixed vocabulary only; no real data is
used. The file is written a little at a time, so memory stays small however
large the file is. Standard library only.

Example (about 1GB)::

    uv run python scripts/make_large_sample_csv.py --size-mb 1000 --columns 30 \\
        --out /tmp/large_sample.csv
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from typing import BinaryIO, Optional, Sequence

_ENCODING = "cp932"
_MIN_COLUMNS = 6
_PREFECTURES = ("東京都", "大阪府", "愛知県", "福岡県", "北海道", "宮城県", "広島県")
_JOBS = ("会社員", "公務員", "自営業", "学生", "主婦", "パート", "無職")
_GENDERS = ("男性", "女性")
_WORDS = ("はい", "いいえ", "どちらでもない", "よく使う", "ときどき使う", "使わない")
_BASE_HEADER = ("id", "年齢", "性別", "都道府県", "職業", "年収")


@dataclass(frozen=True)
class Written:
    """What was written: rows (without the header) and bytes."""

    rows: int
    bytes: int


def _open_binary(path: str) -> BinaryIO:
    return open(path, "wb")  # noqa: SIM115 - used as a context manager by the caller


def _header(columns: int) -> list[str]:
    extra = columns - len(_BASE_HEADER)
    return list(_BASE_HEADER) + [
        f"数値{i + 1}" if i % 2 == 0 else f"分類{i + 1}" for i in range(extra)
    ]


def _row(rng: random.Random, row_id: int, columns: int) -> str:
    cells = [
        str(row_id),
        str(rng.randint(18, 85)),
        rng.choice(_GENDERS),
        rng.choice(_PREFECTURES),
        rng.choice(_JOBS),
        str(rng.randint(100, 2000) * 10),
    ]
    for i in range(columns - len(_BASE_HEADER)):
        cells.append(
            str(round(rng.uniform(0, 1000), 2)) if i % 2 == 0 else rng.choice(_WORDS)
        )
    return ",".join(cells)


def write_csv(
    out: str,
    *,
    rows: Optional[int] = None,
    size_mb: Optional[int] = None,
    columns: int = 30,
    seed: int = 42,
    chunk_rows: int = 20_000,
) -> Written:
    """Write the CSV to ``out`` and return what was written.

    Args:
        out: Path of the file.
        rows: Number of data rows. Used when ``size_mb`` is not given.
        size_mb: Keep writing rows until the file reaches this many MiB.
        columns: Number of columns (at least 6).
        seed: Random seed; the same seed gives the same file.
        chunk_rows: Rows encoded and written at a time.

    Raises:
        ValueError: Neither ``rows`` nor ``size_mb`` is given, or the numbers
            are out of range.
    """
    if rows is None and size_mb is None:
        raise ValueError("give rows or size_mb")
    if columns < _MIN_COLUMNS:
        raise ValueError(f"columns must be at least {_MIN_COLUMNS}")
    if (rows is not None and rows < 1) or (size_mb is not None and size_mb < 1):
        raise ValueError("rows and size_mb must be positive")
    if chunk_rows < 1:
        raise ValueError("chunk_rows must be positive")
    rng = random.Random(seed)
    target_bytes = size_mb * 1024 * 1024 if size_mb is not None else None
    total_rows = 0
    total_bytes = 0
    with _open_binary(out) as f:
        head = (",".join(_header(columns)) + "\n").encode(_ENCODING)
        f.write(head)
        total_bytes += len(head)
        while True:
            if target_bytes is None:
                assert rows is not None
                remaining = rows - total_rows
                if remaining <= 0:
                    break
                batch = min(chunk_rows, remaining)
            else:
                if total_bytes >= target_bytes:
                    break
                batch = chunk_rows
            lines = [_row(rng, total_rows + i + 1, columns) for i in range(batch)]
            data = ("\n".join(lines) + "\n").encode(_ENCODING)
            f.write(data)
            total_rows += batch
            total_bytes += len(data)
    return Written(rows=total_rows, bytes=total_bytes)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command line entry; prints the rows and bytes written."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, help="path of the CSV to write")
    parser.add_argument("--rows", type=int, help="number of data rows")
    parser.add_argument("--size-mb", type=int, help="write until this size (MiB)")
    parser.add_argument("--columns", type=int, default=30, help="number of columns")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    if args.rows is None and args.size_mb is None:
        parser.error("give --rows or --size-mb")
    written = write_csv(
        args.out,
        rows=args.rows if args.size_mb is None else None,
        size_mb=args.size_mb,
        columns=args.columns,
        seed=args.seed,
    )
    sys.stdout.write(
        f"wrote {written.rows} rows, {written.bytes} bytes to {args.out}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
