#!/usr/bin/env python3
"""
Write the Nemotron Parquet for a closed network deployment (closed network U3, BR6.1).

The closed network never downloads from Hugging Face (``NEMOTRON_SOURCE=s3``).
Run this on a machine that can reach Hugging Face; it builds the Parquet with
the same conversion the app uses (``SurveyBatchService.download_nemotron_dataset``)
and writes it to ``--output``. Then place the file in the app bucket with
``aws s3 cp`` (the procedure is in ``cdk/README.md``, closed network section)::

    uv run python scripts/export_nemotron_parquet.py --output ./nemotron.parquet

It prints the row count, the size and the SHA-256 of the written file, so the
copy placed in S3 can be checked against them. It never touches S3 or AWS.

Exit codes: 0 written; 1 the download or the conversion failed, or the file
exists and ``--force`` is not given; 2 the output's parent directory is missing.
The file is written to a temporary file next to ``--output`` and renamed when
complete, so a failure leaves no partial file.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.survey_batch_service import SurveyBatchService  # noqa: E402

EXIT_FAILED = 1
EXIT_NO_PARENT = 2


class ExportError(Exception):
    """The Parquet could not be written (the message says why)."""


@dataclass(frozen=True)
class ExportResult:
    """What was written: the numbers to check the placed copy against."""

    path: Path
    row_count: int
    size_bytes: int
    sha256: str


def _download_with_app_conversion() -> bytes:
    # The bucket and region are not used by the download (it never calls AWS).
    return SurveyBatchService(
        bucket_name="", region_name=""
    ).download_nemotron_dataset()


def _count_rows(path: Path) -> int:
    import polars as pl

    try:
        return int(pl.scan_parquet(path).select(pl.len()).collect().item())
    except Exception as e:  # polars raises several types for a broken file
        raise ExportError(f"the downloaded data is not a Parquet file: {e}") from e


def export(
    output: Path,
    *,
    force: bool = False,
    download: Optional[Callable[[], bytes]] = None,
) -> ExportResult:
    """Download, convert and write the Nemotron Parquet to ``output``.

    Raises:
        FileNotFoundError: The parent directory of ``output`` does not exist.
        FileExistsError: ``output`` exists and ``force`` is false.
        ExportError: The data is not a Parquet file.
        Exception: Whatever the download raised (nothing is written).
    """
    if not output.parent.is_dir():
        raise FileNotFoundError(f"directory does not exist: {output.parent}")
    if output.exists() and not force:
        raise FileExistsError(f"file exists (use --force to overwrite): {output}")
    data = (download or _download_with_app_conversion)()
    handle, temp_name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(data)
        row_count = _count_rows(temp)
        os.replace(temp, output)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return ExportResult(
        path=output,
        row_count=row_count,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _parse(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write the Nemotron Parquet for a closed network deployment."
    )
    parser.add_argument("--output", required=True, type=Path, help="Parquet to write")
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing file"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the export and print the numbers; return the exit code."""
    args = _parse(argv)
    try:
        result = export(args.output, force=args.force)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NO_PARENT
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as e:
        print(f"error: export failed ({type(e).__name__}): {e}", file=sys.stderr)
        return EXIT_FAILED
    print(f"written: {result.path}")
    print(f"rows: {result.row_count}")
    print(f"bytes: {result.size_bytes}")
    print(f"sha256: {result.sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
