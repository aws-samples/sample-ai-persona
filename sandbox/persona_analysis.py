"""PersonaAnalysisLibrary: fixed functions run inside the analysis sandbox (C9).

This file is written into each generation's Code Interpreter session and run
there; the app never imports it (NFR8.2). Tests load it from its path and call
the same functions in process (NFR9.3).

It is written for what the session ships with: Python 3.12, duckdb 1.3,
pyarrow 17, numpy and scikit-learn 1.5. Nothing is installed at run time.

Every function takes ``(args, workdir)`` and returns a JSON-safe value or
raises :class:`AnalysisError`. :func:`dispatch` wraps both into the C9 result
``{ok, value}`` / ``{ok, error: {code, context}}``. Error contexts carry counts
and limits only: no row data, no exception text, no URL.

Command line (one JSON line on stdout, nothing else)::

    python persona_analysis.py --workdir work <function> <base64 JSON args>

Layout of ``workdir`` (on disk; /tmp in the session is a RAM tmpfs):
``sources/<source_id>`` the brought-in file, ``prepared/<source_id>.parquet``
the prepared table, ``progress/<progress_id>.json`` the progress of a command.
"""

from __future__ import annotations

import base64
import bisect
import codecs
import datetime as _dt
import decimal
import io
import json
import os
import re
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
import warnings
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Version of the fixed preparation; recorded in PrepareReport (BR4.4).
PREPARE_VERSION = "1"

_CHUNK_BYTES = 16 << 20
_DOWNLOAD_CHUNK_BYTES = 1 << 20
_DOWNLOAD_TIMEOUT_SECONDS = 600
_DUCKDB_MEMORY_LIMIT = "4GB"
_DEFAULT_SEED = 42
#: Line-number examples kept for a failure notice (BR16.2): per reason / in total.
_EXAMPLES_PER_REASON = 3
_EXAMPLES_TOTAL = 5
_DEFAULT_MAX_EXCLUDED_PERCENT = 1
_DEFAULT_SAMPLE_MAX_ROWS = 1_000_000
_ASSIGN_BATCH_ROWS = 100_000
_MAX_CATEGORIES = 20
_TOP_VALUES = 5
_VALUE_MAX_CHARS = 50
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UTF8_BOM = b"\xef\xbb\xbf"
_KNOWN_ENCODINGS = {"utf-8", "utf-8-sig", "cp932", "euc-jp"}
_NUMERIC_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
    "FLOAT",
    "DOUBLE",
}


class AnalysisError(Exception):
    """A failure the caller maps to an error code (never shown as text)."""

    def __init__(self, code: str, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(code)
        self.code = code
        self.context = dict(context or {})


# ---------------------------------------------------------------------------
# paths and small helpers
# ---------------------------------------------------------------------------


def _checked_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise AnalysisError("INVALID_ARGUMENT", {"field": name})
    return value


def _inside(workdir: str, *parts: str) -> str:
    root = os.path.realpath(workdir)
    path = os.path.realpath(os.path.join(root, *parts))
    if path != root and not path.startswith(root + os.sep):
        raise AnalysisError("INVALID_ARGUMENT", {"field": "path"})
    return path


def _source_path(workdir: str, source_id: str) -> str:
    return _inside(workdir, "sources", source_id)


def _parquet_path(workdir: str, source_id: str) -> str:
    return _inside(workdir, "prepared", source_id + ".parquet")


def _write_progress(workdir: str, progress_id: Optional[str], **fields: Any) -> None:
    if not progress_id:
        return
    path = _inside(workdir, "progress", progress_id + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fields, f, ensure_ascii=True)
    os.replace(tmp, path)


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return (
            value
            if value == value and value not in (float("inf"), -float("inf"))
            else None
        )
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if hasattr(value, "item"):  # numpy scalar
        return _json_safe(value.item())
    return str(value)


def _short(value: Any) -> Any:
    value = _json_safe(value)
    if isinstance(value, str) and len(value) > _VALUE_MAX_CHARS:
        return value[:_VALUE_MAX_CHARS]
    return value


def _connect(workdir: str) -> Any:
    import duckdb

    con = duckdb.connect(":memory:")
    tmp_dir = _inside(workdir, "duckdb_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    con.execute(f"SET memory_limit={_sql_literal(_DUCKDB_MEMORY_LIMIT)}")
    con.execute(f"SET temp_directory={_sql_literal(tmp_dir)}")
    con.execute("SET autoinstall_known_extensions=false")
    con.execute("SET autoload_known_extensions=false")
    return con


def _open_dataset(workdir: str, source_id: str) -> Any:
    """A connection with the view ``dataset`` over the prepared table."""
    path = _parquet_path(workdir, _checked_id(source_id, "source_id"))
    if not os.path.isfile(path):
        raise AnalysisError("SOURCE_UNREADABLE")
    con = _connect(workdir)
    con.execute(
        "CREATE VIEW dataset AS SELECT * FROM "
        f"read_parquet({_sql_literal(path)}, file_row_number=true)"
    )
    return con


def _schema(con: Any) -> List[Tuple[str, str]]:
    rows = con.execute("DESCRIBE SELECT * FROM dataset").fetchall()
    return [(str(r[0]), str(r[1])) for r in rows if r[0] != "file_row_number"]


def _is_numeric(type_name: str) -> bool:
    return type_name in _NUMERIC_TYPES or type_name.startswith("DECIMAL")


def _is_out_of_memory(error: BaseException) -> bool:
    return isinstance(error, MemoryError) or "OutOfMemory" in type(error).__name__


# ---------------------------------------------------------------------------
# download (BR6.2): the URL is used here only and never printed
# ---------------------------------------------------------------------------


def download(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Fetch ``url`` into ``sources/<source_id>``. Returns ``{ok, bytes, http_status}``.

    Only ``https://`` is taken: the URL is a presigned S3 GET, and anything
    else (plain http, ``file://``) is refused before anything is opened.
    """
    source_id = _checked_id(args.get("source_id"), "source_id")
    url = args.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise AnalysisError("INVALID_ARGUMENT", {"field": "url"})
    timeout = float(args.get("timeout_seconds", _DOWNLOAD_TIMEOUT_SECONDS))
    dest = _source_path(workdir, source_id)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    written = 0
    status: Optional[int] = None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
            with open(dest, "wb") as out:
                while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):
                    out.write(chunk)
                    written += len(chunk)
        return {"ok": True, "bytes": written, "http_status": status}
    except urllib.error.HTTPError as e:
        status = int(e.code)
    except Exception:  # noqa: BLE001 - the text may contain the URL; drop it
        status = None
    if os.path.exists(dest):
        os.remove(dest)
    return {"ok": False, "bytes": 0, "http_status": status}


# ---------------------------------------------------------------------------
# prepare (BR4.2〜BR4.4)
# ---------------------------------------------------------------------------


class _RetryAsCp932(Exception):
    """A strict UTF-8 read found bytes it cannot decode (BR4.3)."""


def _decode_block(
    block: bytes,
    encoding: str,
    strict: bool,
    counts: Dict[str, int],
    first_line: int = 1,
    bad_lines: Optional[List[int]] = None,
) -> str:
    """Decode whole lines; a line that does not decode becomes an empty line.

    The empty line keeps every later row at its place in the original file, so
    line numbers given to the user stay true (BR16.2). An empty line is skipped
    when the table is read, so it never counts as a row. ``first_line`` is the
    1-based number of the block's first line; the numbers of the first few
    undecodable lines are added to ``bad_lines``.
    """
    try:
        return block.decode(encoding)
    except UnicodeDecodeError:
        if strict:
            raise _RetryAsCp932() from None
    kept: List[str] = []
    for offset, line in enumerate(block.split(b"\n")):
        try:
            kept.append(line.decode(encoding))
        except UnicodeDecodeError:
            kept.append("")
            counts["decode_error"] = counts.get("decode_error", 0) + 1
            if bad_lines is not None and len(bad_lines) < _EXAMPLES_PER_REASON:
                bad_lines.append(first_line + offset)
    return "\n".join(kept)


def _transcode(
    src: str,
    dst: str,
    encoding: str,
    *,
    strict: bool,
    on_progress: Callable[[int, int], None],
) -> Tuple[int, Dict[str, int], List[int]]:
    """Write ``src`` as UTF-8 to ``dst`` 16MB at a time.

    Returns ``(lines, drops, bad_lines)``: the line count, the undecodable
    lines per reason, and the original numbers of the first few of them.
    """
    counts: Dict[str, int] = {}
    bad_lines: List[int] = []
    lines = 0
    done = 0
    rest = b""
    first = True
    with open(src, "rb") as fin, open(dst, "w", encoding="utf-8") as fout:
        while chunk := fin.read(_CHUNK_BYTES):
            done += len(chunk)
            data = rest + chunk
            if first:
                first = False
                if data.startswith(_UTF8_BOM) and encoding == "utf-8":
                    data = data[len(_UTF8_BOM) :]
            cut = data.rfind(b"\n")
            if cut < 0:
                rest = data
                continue
            block, rest = data[: cut + 1], data[cut + 1 :]
            fout.write(
                _decode_block(block, encoding, strict, counts, lines + 1, bad_lines)
            )
            lines += block.count(b"\n")
            on_progress(lines, done)
        if rest:
            fout.write(
                _decode_block(rest, encoding, strict, counts, lines + 1, bad_lines)
            )
            lines += 1
    return lines, counts, bad_lines


def _read_encoding(metadata: Dict[str, Any]) -> str:
    encoding = str(metadata.get("encoding") or "undetermined")
    if encoding == "utf-8-sig":
        return "utf-8"
    if encoding in _KNOWN_ENCODINGS or encoding == "undetermined":
        return encoding
    raise AnalysisError("INVALID_ARGUMENT", {"field": "encoding"})


def _to_parquet(
    workdir: str, utf8_path: str, out: str, delimiter: str
) -> Tuple[int, Dict[str, int], List[Dict[str, str]], Dict[str, List[int]]]:
    import duckdb

    con = _connect(workdir)
    try:
        con.execute(
            "COPY (SELECT * FROM read_csv("
            f"{_sql_literal(utf8_path)}, delim={_sql_literal(delimiter)}, "
            "header=true, ignore_errors=true, store_rejects=true)) "
            f"TO {_sql_literal(out)} (FORMAT parquet, COMPRESSION zstd)"
        )
        rows = int(
            con.execute(
                f"SELECT count(*) FROM read_parquet({_sql_literal(out)})"
            ).fetchone()[0]
        )
        rejects = con.execute(
            "SELECT lower(CAST(error_type AS VARCHAR)), count(DISTINCT line) "
            "FROM reject_errors GROUP BY 1 ORDER BY 1"
        ).fetchall()
        reject_lines: Dict[str, List[int]] = {}
        for reason, _n in rejects:
            reject_lines[str(reason)] = [
                int(r[0])
                for r in con.execute(
                    "SELECT DISTINCT line FROM reject_errors "
                    "WHERE lower(CAST(error_type AS VARCHAR)) = ? "
                    f"ORDER BY line LIMIT {_EXAMPLES_PER_REASON}",
                    [reason],
                ).fetchall()
            ]
        columns = [
            {"name": str(r[0]), "type": str(r[1])}
            for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet({_sql_literal(out)})"
            ).fetchall()
        ]
    except duckdb.Error as e:
        if _is_out_of_memory(e):
            raise AnalysisError("OUT_OF_MEMORY") from None
        raise AnalysisError("SOURCE_UNREADABLE") from None
    finally:
        con.close()
    return rows, {str(k): int(v) for k, v in rejects}, columns, reject_lines


def _header_columns(path: str, delimiter: str) -> Optional[int]:
    """Number of columns in the header line of a UTF-8 file, when it can be read."""
    import csv

    try:
        with open(path, encoding="utf-8", newline="") as f:
            header = next(csv.reader(f, delimiter=delimiter), None)
    except (OSError, UnicodeDecodeError, csv.Error, ValueError):
        return None
    return len(header) if header else None


def _pick_examples(per_reason: Dict[str, List[int]]) -> Dict[str, List[int]]:
    """At most 3 line numbers per reason and 5 in all, smallest first (BR16.2)."""
    picked: Dict[str, List[int]] = {}
    total = 0
    for reason in sorted(per_reason):
        numbers = sorted(set(per_reason[reason]))[:_EXAMPLES_PER_REASON]
        numbers = numbers[: max(_EXAMPLES_TOTAL - total, 0)]
        if numbers:
            picked[reason] = numbers
            total += len(numbers)
    return picked


def _max_percent(args: Dict[str, Any]) -> int:
    value = args.get("max_excluded_percent", _DEFAULT_MAX_EXCLUDED_PERCENT)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise AnalysisError("INVALID_ARGUMENT", {"field": "max_excluded_percent"})
    return value


def prepare(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Prepare one table into ``prepared/<source_id>.parquet`` (BR4.2).

    Args: ``source_id``, ``metadata`` (``encoding``, ``delimiter``), optional
    ``local_path`` (inside ``workdir``) and ``progress_id``.
    Returns a PrepareReport (contract C8).
    """
    source_id = _checked_id(args.get("source_id"), "source_id")
    max_percent = _max_percent(args)
    progress_id = args.get("progress_id")
    if progress_id is not None:
        progress_id = _checked_id(progress_id, "progress_id")
    metadata = dict(args.get("metadata") or {})
    local_path = args.get("local_path")
    src = (
        _inside(workdir, str(local_path))
        if local_path
        else _source_path(workdir, source_id)
    )
    if not os.path.isfile(src) or os.path.getsize(src) == 0:
        raise AnalysisError("SOURCE_UNREADABLE")
    delimiter = str(metadata.get("delimiter") or ",")
    encoding = _read_encoding(metadata)
    total = os.path.getsize(src)
    out = _parquet_path(workdir, source_id)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    utf8_path = out + ".utf8.csv"
    seen = {"lines": 0}

    def on_progress(lines: int, done: int) -> None:
        seen["lines"] = lines
        _write_progress(
            workdir,
            progress_id,
            phase="transcode",
            rows=max(lines - 1, 0),
            bytes_done=done,
            bytes_total=total,
        )

    def memory_context() -> Dict[str, Any]:
        """What is known when memory runs out (BR17.1): rows so far, columns."""
        context: Dict[str, Any] = {}
        if seen["lines"] > 1:
            context["rows_read"] = seen["lines"] - 1
        columns_count = _header_columns(utf8_path, delimiter)
        if columns_count:
            context["columns_count"] = columns_count
        return context

    retried = False
    try:
        try:
            first = "utf-8" if encoding == "undetermined" else encoding
            seen["lines"] = 0
            _, drops, bad_lines = _transcode(
                src,
                utf8_path,
                first,
                strict=encoding == "undetermined",
                on_progress=on_progress,
            )
            settled = first
        except _RetryAsCp932:
            retried = True
            settled = "cp932"
            seen["lines"] = 0
            _, drops, bad_lines = _transcode(
                src, utf8_path, settled, strict=False, on_progress=on_progress
            )
        _write_progress(workdir, progress_id, phase="parquet", bytes_total=total)
        rows, rejects, columns, reject_lines = _to_parquet(
            workdir, utf8_path, out, delimiter
        )
    except MemoryError:
        raise AnalysisError("OUT_OF_MEMORY", memory_context()) from None
    except AnalysisError as e:
        if e.code == "OUT_OF_MEMORY" and not e.context:
            e.context.update(memory_context())
        raise
    finally:
        if os.path.exists(utf8_path):
            os.remove(utf8_path)

    counts = dict(drops)
    for reason, n in rejects.items():
        counts[reason] = counts.get(reason, 0) + n
    excluded = sum(counts.values())
    per_reason = dict(reject_lines)
    if bad_lines:
        per_reason["decode_error"] = list(bad_lines)
    examples = _pick_examples(per_reason)
    if rows == 0:
        raise AnalysisError("SOURCE_UNREADABLE", {"rows_excluded": excluded})
    # Integers only: more than max_percent % of the rows seen (BR16.1, NFR1.14).
    if excluded * 100 > (rows + excluded) * max_percent:
        context: Dict[str, Any] = {"rows_read": rows, "rows_excluded": excluded}
        row_examples = sorted({n for v in examples.values() for n in v})
        if row_examples:
            context["row_examples"] = row_examples
        raise AnalysisError("TOO_MANY_BAD_ROWS", context)
    reported = metadata.get("encoding")
    if reported == "utf-8-sig":
        settled = "utf-8-sig"
    report: Dict[str, Any] = {
        "source_id": source_id,
        "rows_read": rows,
        "rows_excluded": excluded,
        "columns": columns,
        "encoding": settled,
        "encoding_retried": retried,
        "prepare_version": PREPARE_VERSION,
        "memory_limit": _DUCKDB_MEMORY_LIMIT,
    }
    if counts:
        report["excluded_reasons"] = {"counts": counts}
        if examples:
            report["excluded_reasons"]["examples"] = examples
    _write_progress(
        workdir, progress_id, phase="finished", rows=rows, rows_excluded=excluded
    )
    return report


# ---------------------------------------------------------------------------
# profile_dataset and run_query
# ---------------------------------------------------------------------------


def _profile_column(con: Any, name: str, type_name: str, rows: int) -> Dict[str, Any]:
    col = _quote_ident(name)
    non_null, distinct = con.execute(
        f"SELECT count({col}), count(DISTINCT {col}) FROM dataset"
    ).fetchone()
    summary: Dict[str, Any] = {
        "name": name,
        "type": type_name,
        "non_null": int(non_null),
        "distinct": int(distinct),
        "all_distinct": rows > 0 and int(distinct) == rows and int(non_null) == rows,
    }
    if _is_numeric(type_name):
        low, high, mean = con.execute(
            f"SELECT min({col}), max({col}), avg({col}) FROM dataset"
        ).fetchone()
        summary.update(min=_json_safe(low), max=_json_safe(high), mean=_json_safe(mean))
        return summary
    top = con.execute(
        f"SELECT CAST({col} AS VARCHAR) AS v, count(*) AS n FROM dataset "
        f"WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY n DESC, v LIMIT {_TOP_VALUES}"
    ).fetchall()
    summary["top_values"] = [{"value": _short(v), "count": int(n)} for v, n in top]
    return summary


def profile_dataset(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Distribution summary of every column of the prepared table."""
    con = _open_dataset(workdir, args.get("source_id"))
    try:
        rows = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
        columns = [_profile_column(con, n, t, rows) for n, t in _schema(con)]
    finally:
        con.close()
    return {"rows": rows, "columns": columns}


def run_query(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Run SQL built by the app (``sql_builder``) on the view ``dataset``."""
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.lstrip().upper().startswith("SELECT"):
        raise AnalysisError("INVALID_ARGUMENT", {"field": "sql"})
    params = list(args.get("params") or [])
    max_rows = int(args.get("max_rows", 200))
    timeout = float(args.get("timeout_seconds", 30))
    con = _open_dataset(workdir, args.get("source_id"))
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        result = con.execute(sql, params) if params else con.execute(sql)
        columns = [str(d[0]) for d in result.description]
        fetched = result.fetchmany(max_rows + 1)
    except Exception as e:  # noqa: BLE001 - map to codes; drop the text
        if not timer.is_alive():
            raise AnalysisError("QUERY_TIMEOUT", {"seconds": timeout}) from None
        if _is_out_of_memory(e):
            raise AnalysisError("OUT_OF_MEMORY") from None
        raise AnalysisError("QUERY_FAILED") from None
    finally:
        timer.cancel()
        con.close()
    return {
        "columns": columns,
        "rows": [[_json_safe(v) for v in row] for row in fetched[:max_rows]],
        "truncated": len(fetched) > max_rows,
    }


# ---------------------------------------------------------------------------
# cluster_segments (BR5.3)
# ---------------------------------------------------------------------------


class _Features:
    """Standardised numbers and one-hot categories, fixed on the sample."""

    def __init__(self, columns: List[Tuple[str, bool]], sample: Dict[str, List[Any]]):
        import numpy as np

        self.columns = columns
        self.stats: Dict[str, Tuple[float, float]] = {}
        self.categories: Dict[str, List[str]] = {}
        for name, numeric in columns:
            values = sample[name]
            if numeric:
                arr = np.array([np.nan if v is None else float(v) for v in values])
                mean = float(np.nanmean(arr)) if np.isfinite(arr).any() else 0.0
                std = float(np.nanstd(arr)) if np.isfinite(arr).any() else 0.0
                self.stats[name] = (mean, std if std > 0 else 1.0)
            else:
                counts: Dict[str, int] = {}
                for v in values:
                    if v is not None:
                        counts[str(v)] = counts.get(str(v), 0) + 1
                ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                self.categories[name] = [k for k, _ in ranked[:_MAX_CATEGORIES]]

    def numbers(self, name: str, values: List[Any]) -> Any:
        import numpy as np

        mean, _ = self.stats[name]
        return np.array([mean if v is None else float(v) for v in values])

    def matrix(self, batch: Dict[str, List[Any]]) -> Any:
        import numpy as np

        parts = []
        for name, numeric in self.columns:
            values = batch[name]
            if numeric:
                mean, std = self.stats[name]
                parts.append(((self.numbers(name, values) - mean) / std)[:, None])
            else:
                cats = self.categories[name]
                index = {c: i for i, c in enumerate(cats)}
                block = np.zeros((len(values), max(len(cats), 1)))
                for row, v in enumerate(values):
                    i = index.get(None if v is None else str(v))
                    if i is not None:
                        block[row, i] = 1.0
                parts.append(block)
        return np.hstack(parts)


def _select_columns(con: Any, columns: Any) -> List[Tuple[str, bool]]:
    if not isinstance(columns, list) or not columns:
        raise AnalysisError("TOO_FEW_COLUMNS", {"columns": 0})
    schema = dict(_schema(con))
    names = list(dict.fromkeys(str(c) for c in columns))
    unknown = [n for n in names if n not in schema]
    if unknown:
        raise AnalysisError("TOO_FEW_COLUMNS", {"unknown_columns": len(unknown)})
    return [(n, _is_numeric(schema[n])) for n in names]


def _select_sql(columns: List[Tuple[str, bool]]) -> str:
    parts = []
    for name, numeric in columns:
        col = _quote_ident(name)
        parts.append(
            f"CAST({col} AS DOUBLE) AS {col}"
            if numeric
            else f"CAST({col} AS VARCHAR) AS {col}"
        )
    return ", ".join(parts)


def _batches(con: Any, sql: str) -> Any:
    reader = con.execute(sql).fetch_record_batch(_ASSIGN_BATCH_ROWS)
    for batch in reader:
        yield batch.to_pydict()


class _Tally:
    """Per-cluster sizes and feature sums over every row."""

    def __init__(self, k: int, features: _Features) -> None:
        self.k = k
        self.features = features
        self.sizes = [0] * k
        self.sums: Dict[str, List[float]] = {}
        self.cats: Dict[str, List[Dict[str, int]]] = {}
        for name, numeric in features.columns:
            if numeric:
                self.sums[name] = [0.0] * k
            else:
                self.cats[name] = [{} for _ in range(k)]

    def add(self, labels: Any, batch: Dict[str, List[Any]]) -> None:
        for label in labels:
            self.sizes[int(label)] += 1
        for name, numeric in self.features.columns:
            if numeric:
                values = self.features.numbers(name, batch[name])
                for label, v in zip(labels, values):
                    self.sums[name][int(label)] += float(v)
            else:
                for label, v in zip(labels, batch[name]):
                    bucket = self.cats[name][int(label)]
                    key = "" if v is None else str(v)
                    bucket[key] = bucket.get(key, 0) + 1

    def features_of(self, label: int, population: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        size = self.sizes[label]
        for name, numeric in self.features.columns:
            if numeric:
                total = sum(self.sums[name])
                out.append(
                    {
                        "name": name,
                        "value": round(self.sums[name][label] / size, 4),
                        "baseline": round(total / population, 4),
                    }
                )
                continue
            bucket = self.cats[name][label]
            top = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            overall = sum(b.get(top, 0) for b in self.cats[name])
            out.append(
                {
                    "name": f"{name}={_short(top)}",
                    "value": round(bucket[top] / size, 4),
                    "baseline": round(overall / population, 4),
                }
            )
        return out


def _sample(con: Any, select: str, population: int, limit: int, seed: int) -> Any:
    if population <= limit:
        sql = f"SELECT {select} FROM dataset ORDER BY file_row_number"
    else:
        sql = (
            f"SELECT {select} FROM dataset "
            f"ORDER BY hash(file_row_number + {int(seed)}), file_row_number "
            f"LIMIT {int(limit)}"
        )
    merged: Dict[str, List[Any]] = {}
    for batch in _batches(con, sql):
        for key, values in batch.items():
            merged.setdefault(key, []).extend(values)
    return merged


def cluster_segments(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Cluster on a sample, then count every row (BR5.3).

    Args: ``source_id``, ``columns``, ``k`` (the persona count), ``seed``
    (default 42), ``sample_max_rows`` (default 1,000,000).
    Returns ``{k, seed, population, sample_rows, clusters: [{index, size,
    share, features: [{name, value, baseline}]}]}``; sizes sum to the population.
    """
    k = args.get("k")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise AnalysisError("INVALID_ARGUMENT", {"field": "k"})
    seed = int(args.get("seed", _DEFAULT_SEED))
    limit = int(args.get("sample_max_rows", _DEFAULT_SAMPLE_MAX_ROWS))
    con = _open_dataset(workdir, args.get("source_id"))
    try:
        columns = _select_columns(con, args.get("columns"))
        select = _select_sql(columns)
        population = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
        sample = _sample(con, select, population, limit, seed)
        features = _Features(columns, sample)
        model = _fit(features.matrix(sample), k, seed)
        tally = _Tally(k, features)
        for batch in _batches(con, f"SELECT {select} FROM dataset"):
            labels = model(features.matrix(batch))
            tally.add(labels, batch)
    finally:
        con.close()
    if min(tally.sizes) == 0:
        raise AnalysisError("EMPTY_CLUSTER", {"k": k})
    order = sorted(range(k), key=lambda i: (-tally.sizes[i], i))
    clusters = [
        {
            "index": index,
            "size": tally.sizes[label],
            "share": tally.sizes[label] / population,
            "features": tally.features_of(label, population),
        }
        for index, label in enumerate(order)
    ]
    return {
        "k": k,
        "seed": seed,
        "population": population,
        "sample_rows": len(next(iter(sample.values()), [])),
        "clusters": clusters,
    }


def _fit(matrix: Any, k: int, seed: int) -> Callable[[Any], Any]:
    """A function that labels rows; k=1 puts everyone in one group."""
    import numpy as np

    if k == 1:
        return lambda m: np.zeros(len(m), dtype=int)
    if len(matrix) < k or len(np.unique(matrix, axis=0)) < k:
        raise AnalysisError("EMPTY_CLUSTER", {"k": k})
    from sklearn.cluster import KMeans

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(matrix)
    return lambda m: model.predict(m)


# ---------------------------------------------------------------------------
# documents (U4: BR24.1〜BR24.6, BR27.1, NFR1.20, NFR1.22, NFR5.39)
# ---------------------------------------------------------------------------
#
# Layout under ``documents/<source_id>/``: ``chunks/<index>.txt`` the text of
# each chunk, ``index.json`` the chunk positions (pages, heading path) and the
# settled character count, ``normalized.txt`` the whole text normalised for
# the quote check. The limits (chunk size, overlap, the shortest quote) come
# as arguments: this file never reads the app's settings (NFR8.6).
#
# PDF pages are read with pdfminer.six and a docx with the standard library
# (zipfile + ElementTree). Both ship with Code Interpreter and with the app
# (U4 Step 1: markitdown cannot read a docx in the session, and pypdf is not
# in the app, so the same code runs in both places and is tested in process).

_DOCUMENT_FORMATS = frozenset({"pdf", "docx", "txt", "md"})
_MAX_QUOTES = 50
_MD_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_STYLE_HEADING = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n[ \t\r\f\v]*\n")
#: Removed before quotes are compared (with every white space, BR27.1): the
#: soft hyphen, the zero-width space (``wrap_untrusted_data`` puts one in
#: ``<data``) and the zero-width no-break space.
_NOT_COMPARED = frozenset({"­", "​", "﻿"})
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentError(AnalysisError):
    """A document the preparation refuses; the context holds ``source_index``."""


class _Block:
    """One piece of a document: a heading, a paragraph, or the start of a page."""

    __slots__ = ("kind", "text", "level", "page")

    def __init__(
        self, kind: str, text: str = "", level: int = 0, page: int = 0
    ) -> None:
        self.kind = kind
        self.text = text
        self.level = level
        self.page = page


class _Document:
    """The blocks of one document and, for a PDF, its page counts."""

    def __init__(self) -> None:
        self.blocks: List[_Block] = []
        self.pages: Optional[int] = None
        self.no_text_pages: List[int] = []


def _int_arg(args: Dict[str, Any], name: str, low: int, high: int) -> int:
    value = args.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise AnalysisError("INVALID_ARGUMENT", {"field": name})
    return value


def _document_dir(workdir: str, source_id: Any) -> str:
    return _inside(workdir, "documents", _checked_id(source_id, "source_id"))


def _paragraphs(text: str) -> List[str]:
    return [p.strip() for p in _BLANK_LINES.split(text) if p.strip()]


def _looks_encrypted(error: BaseException) -> bool:
    name = type(error).__name__
    return "Password" in name or "Encrypt" in name


def _read_pdf(path: str, document: _Document, on_page: Callable[[int], None]) -> None:
    """Page by page with pdfminer; pages without text are left out (BR24.3)."""
    try:
        from pdfminer.converter import TextConverter
        from pdfminer.layout import LAParams
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfparser import PDFParser
    except ImportError:
        raise AnalysisError("SOURCE_UNREADABLE") from None

    with open(path, "rb") as f:
        try:
            pdf = PDFDocument(PDFParser(f))
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001 - the text may hold data
            if _looks_encrypted(e):
                raise AnalysisError("DOCUMENT_ENCRYPTED") from None
            raise AnalysisError("SOURCE_UNREADABLE") from None
        # BR24.6, NFR1.20: refused even when an empty password opens it
        if getattr(pdf, "encryption", None):
            raise AnalysisError("DOCUMENT_ENCRYPTED")
        resources = PDFResourceManager()
        pages = 0
        for number, page in enumerate(PDFPage.create_pages(pdf), start=1):
            out = io.StringIO()
            device = TextConverter(resources, out, laparams=LAParams())
            try:
                PDFPageInterpreter(resources, device).process_page(page)
            finally:
                device.close()
            pages = number
            text = out.getvalue().replace("\x0c", "")
            paragraphs = _paragraphs(text)
            if not paragraphs:
                document.no_text_pages.append(number)
            else:
                document.blocks.append(_Block("page", page=number))
                document.blocks.extend(_Block("para", p) for p in paragraphs)
            on_page(number)
        document.pages = pages


def _docx_heading_styles(archive: Any) -> Dict[str, int]:
    """Style ID -> heading level, by name (``heading 1``) or outline level."""

    if "word/styles.xml" not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read("word/styles.xml"))
    levels: Dict[str, int] = {}
    for style in root.iter(_WORD + "style"):
        style_id = style.get(_WORD + "styleId")
        if not style_id:
            continue
        name_el = style.find(_WORD + "name")
        name = (name_el.get(_WORD + "val") if name_el is not None else "") or ""
        match = _STYLE_HEADING.match(name.strip())
        if match:
            levels[style_id] = int(match.group(1))
        elif name.strip().lower() == "title":
            levels[style_id] = 1
        outline = style.find(f"{_WORD}pPr/{_WORD}outlineLvl")
        if style_id not in levels and outline is not None:
            value = outline.get(_WORD + "val", "")
            if value.isdigit() and int(value) < 9:
                levels[style_id] = int(value) + 1
    return levels


def _paragraph_text(paragraph: Any) -> str:
    parts: List[str] = []
    for node in paragraph.iter():
        if node.tag == _WORD + "t" and node.text:
            parts.append(node.text)
        elif node.tag == _WORD + "tab":
            parts.append("\t")
        elif node.tag in (_WORD + "br", _WORD + "cr"):
            parts.append("\n")
    return "".join(parts)


def _read_docx(path: str, document: _Document) -> None:
    """Paragraphs and headings of ``word/document.xml``, read as a stream."""

    try:
        with zipfile.ZipFile(path) as archive:
            styles = _docx_heading_styles(archive)
            with archive.open("word/document.xml") as f:
                for _event, element in ET.iterparse(f, events=("end",)):
                    if element.tag != _WORD + "p":
                        continue
                    text = _paragraph_text(element).strip()
                    style = element.find(f"{_WORD}pPr/{_WORD}pStyle")
                    level = styles.get(
                        style.get(_WORD + "val", "") if style is not None else "", 0
                    )
                    element.clear()
                    if not text:
                        continue
                    if level:
                        document.blocks.append(_Block("heading", text, level=level))
                    else:
                        document.blocks.append(_Block("para", text))
    except MemoryError:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError, ValueError):
        raise AnalysisError("SOURCE_UNREADABLE") from None


def _decode_text(data: bytes, encoding: str) -> str:
    if encoding in ("utf-8", "utf-8-sig"):
        return data.decode("utf-8-sig", errors="replace")
    if encoding in ("cp932", "euc-jp"):
        return data.decode(encoding, errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace")


def _read_text(path: str, document: _Document, encoding: str, markdown: bool) -> None:
    """Paragraphs split at blank lines; ``#`` lines are headings in Markdown."""
    with open(path, "rb") as f:
        text = (
            _decode_text(f.read(), encoding).replace("\r\n", "\n").replace("\r", "\n")
        )
    lines: List[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip()
        lines.clear()
        if body:
            document.blocks.append(_Block("para", body))

    for line in text.split("\n"):
        heading = _MD_HEADING.match(line) if markdown else None
        if heading:
            flush()
            document.blocks.append(
                _Block("heading", heading.group(2).strip(), level=len(heading.group(1)))
            )
        elif line.strip():
            lines.append(line)
        else:
            flush()
    flush()


def _document_blocks(
    path: str, fmt: str, encoding: str, on_page: Callable[[int], None]
) -> _Document:
    """Read one brought-in document into blocks (BR24.1). OCR is never tried."""
    document = _Document()
    if fmt == "pdf":
        _read_pdf(path, document, on_page)
    elif fmt == "docx":
        _read_docx(path, document)
    else:
        _read_text(path, document, encoding, markdown=fmt == "md")
    return document


class _Layout:
    """The joined text and where headings, pages and paragraphs start."""

    def __init__(self, document: _Document) -> None:
        pieces: List[str] = []
        length = 0
        self.headings: List[Tuple[int, int, str]] = []
        self.pages: List[Tuple[int, int]] = []
        self.paragraphs: List[int] = []
        pending_page: Optional[int] = None
        for block in document.blocks:
            if block.kind == "page":
                pending_page = block.page
                continue
            if pieces:
                pieces.append("\n\n")
                length += 2
            offset = length
            if pending_page is not None:
                self.pages.append((offset, pending_page))
                pending_page = None
            if block.kind == "heading":
                self.headings.append((offset, block.level, block.text))
                text = "#" * block.level + " " + block.text
            else:
                text = block.text
            self.paragraphs.append(offset)
            pieces.append(text)
            length += len(text)
        self.text = "".join(pieces)

    def page_at(self, offset: int) -> Optional[int]:
        if not self.pages:
            return None
        index = bisect.bisect_right([p[0] for p in self.pages], offset) - 1
        return self.pages[max(index, 0)][1]

    def heading_path(self, offset: int) -> List[str]:
        stack: List[Tuple[int, str]] = []
        for start, level, title in self.headings:
            if start > offset:
                break
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        return [title for _level, title in stack]

    def split(self, chunk_chars: int, overlap: int) -> List[Tuple[int, int, int]]:
        """``(start, end, new_start)`` of each chunk (BR24.2).

        A chunk ends before the last heading that fits, else the last page,
        else the last paragraph, else at ``chunk_chars``; the next one starts
        ``overlap`` characters before that end.
        """

        cuts = (
            [h[0] for h in self.headings],
            [p[0] for p in self.pages],
            self.paragraphs,
        )
        length = len(self.text)
        chunks: List[Tuple[int, int, int]] = []
        start = new_start = 0
        while True:
            if length - start <= chunk_chars:
                chunks.append((start, length, new_start))
                return chunks
            limit = start + chunk_chars
            end = limit
            for offsets in cuts:
                index = bisect.bisect_right(offsets, limit) - 1
                if index >= 0 and offsets[index] > start + overlap:
                    end = offsets[index]
                    break
            chunks.append((start, end, new_start))
            new_start = end
            start = end - overlap


def _normalize_for_quotes(text: str) -> str:
    """NFKC, then no white space, soft hyphen or zero-width space (BR27.1)."""

    normalized = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in normalized if not ch.isspace() and ch not in _NOT_COMPARED
    )


def prepare_document(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Turn one document into chunks inside the session (BR24.1〜BR24.6).

    Args: ``source_id``, ``source_index`` (its place in the generation),
    ``format`` (pdf / docx / txt / md), ``encoding`` (txt / md),
    ``chunk_chars``, ``overlap_chars`` and ``progress_id``.
    Returns a DocumentReport (contract C8): ``source_id``, ``chars``,
    ``chunks``, ``headings``, ``no_text_pages`` and, for a PDF, ``pages``.
    """
    source_id = _checked_id(args.get("source_id"), "source_id")
    source_index = _int_arg(args, "source_index", 0, 1000)
    fmt = args.get("format")
    if fmt not in _DOCUMENT_FORMATS:
        raise AnalysisError("INVALID_ARGUMENT", {"field": "format"})
    chunk_chars = _int_arg(args, "chunk_chars", 1, 1_000_000)
    overlap = _int_arg(args, "overlap_chars", 0, chunk_chars - 1)
    encoding = str(args.get("encoding") or "undetermined")
    progress_id = args.get("progress_id")
    if progress_id is not None:
        progress_id = _checked_id(progress_id, "progress_id")
    src = _source_path(workdir, source_id)
    context = {"source_index": source_index}
    if not os.path.isfile(src) or os.path.getsize(src) == 0:
        raise DocumentError("SOURCE_UNREADABLE", context)

    def on_page(pages: int) -> None:
        _write_progress(workdir, progress_id, phase="pages", pages=pages)

    try:
        document = _document_blocks(src, str(fmt), encoding, on_page)
        layout = _Layout(document)
    except MemoryError:
        raise AnalysisError("OUT_OF_MEMORY", context) from None
    except AnalysisError as e:
        if e.code in ("DOCUMENT_ENCRYPTED", "SOURCE_UNREADABLE"):
            raise DocumentError(e.code, context) from None
        raise
    except Exception:  # noqa: BLE001 - a reader failure; its text may hold data
        raise DocumentError("SOURCE_UNREADABLE", context) from None
    if not layout.text.strip():
        raise DocumentError("DOCUMENT_NO_TEXT", context)

    folder = _document_dir(workdir, source_id)
    os.makedirs(os.path.join(folder, "chunks"), exist_ok=True)
    index: List[Dict[str, Any]] = []
    for number, (start, end, new_start) in enumerate(
        layout.split(chunk_chars, overlap)
    ):
        with open(
            os.path.join(folder, "chunks", f"{number}.txt"), "w", encoding="utf-8"
        ) as f:
            f.write(layout.text[start:end])
        index.append(
            {
                "page_from": layout.page_at(start),
                "page_to": layout.page_at(max(end - 1, start)),
                "heading_path": layout.heading_path(new_start),
            }
        )
    with open(os.path.join(folder, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"chunks": index, "chars": len(layout.text)}, f, ensure_ascii=True)
    with open(os.path.join(folder, "normalized.txt"), "w", encoding="utf-8") as f:
        f.write(_normalize_for_quotes(layout.text))
    report: Dict[str, Any] = {
        "source_id": source_id,
        "chars": len(layout.text),
        "chunks": len(index),
        "headings": len(layout.headings),
        "no_text_pages": list(document.no_text_pages),
    }
    if document.pages is not None:
        report["pages"] = document.pages
    _write_progress(
        workdir,
        progress_id,
        phase="finished",
        pages=document.pages,
        chars=report["chars"],
        chunks=report["chunks"],
    )
    return report


def _read_index(folder: str) -> Dict[str, Any]:
    path = os.path.join(folder, "index.json")
    if not os.path.isfile(path):
        raise AnalysisError("SOURCE_UNREADABLE")
    with open(path, encoding="utf-8") as f:
        return dict(json.load(f))


def document_chunk(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Part of one chunk, ``max_chars`` at a time (NFR5.39).

    Args: ``source_id``, ``index``, ``offset`` and ``max_chars``.
    Returns ``{index, chunks, text, next_offset, page_from, page_to,
    heading_path}``; ``next_offset`` is ``None`` at the end of the chunk.
    """
    folder = _document_dir(workdir, args.get("source_id"))
    meta = _read_index(folder)
    chunks = list(meta.get("chunks") or [])
    index = _int_arg(args, "index", 0, max(len(chunks) - 1, 0))
    if not chunks:
        raise AnalysisError("INVALID_ARGUMENT", {"field": "index"})
    offset = _int_arg(args, "offset", 0, 100_000_000)
    max_chars = _int_arg(args, "max_chars", 1, 1_000_000)
    with open(os.path.join(folder, "chunks", f"{index}.txt"), encoding="utf-8") as f:
        text = f.read()
    end = offset + max_chars
    info = chunks[index]
    return {
        "index": index,
        "chunks": len(chunks),
        "text": text[offset:end],
        "next_offset": end if end < len(text) else None,
        "page_from": info.get("page_from"),
        "page_to": info.get("page_to"),
        "heading_path": list(info.get("heading_path") or []),
    }


def verify_quotes(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Whether each quote is in the document's text (BR27.1).

    Both sides are normalised (:func:`_normalize_for_quotes`), so a quote that
    runs across a line break or the overlap of two chunks is found. A quote
    shorter than ``min_chars`` after normalising is never found.
    Args: ``source_id``, ``quotes`` (50 at most) and ``min_chars``.
    Returns ``{found: [bool, ...]}`` in the order of ``quotes``.
    """
    folder = _document_dir(workdir, args.get("source_id"))
    quotes = args.get("quotes")
    if (
        not isinstance(quotes, list)
        or len(quotes) > _MAX_QUOTES
        or not all(isinstance(q, str) for q in quotes)
    ):
        raise AnalysisError("INVALID_ARGUMENT", {"field": "quotes"})
    min_chars = _int_arg(args, "min_chars", 1, 10_000)
    path = os.path.join(folder, "normalized.txt")
    if not os.path.isfile(path):
        raise AnalysisError("SOURCE_UNREADABLE")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    found = []
    for quote in quotes:
        normalized = _normalize_for_quotes(quote)
        found.append(len(normalized) >= min_chars and normalized in text)
    return {"found": found}


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

FUNCTIONS: Dict[str, Callable[[Dict[str, Any], str], Any]] = {
    "download": download,
    "prepare": prepare,
    "profile_dataset": profile_dataset,
    "run_query": run_query,
    "cluster_segments": cluster_segments,
    "prepare_document": prepare_document,
    "document_chunk": document_chunk,
    "verify_quotes": verify_quotes,
}


def _failure(code: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code}
    if context:
        error["context"] = _json_safe(context)
    return {"ok": False, "error": error}


def dispatch(function: str, args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Run one function and return the C9 result. Never raises."""
    fn = FUNCTIONS.get(function)
    if fn is None:
        return _failure("UNKNOWN_FUNCTION")
    try:
        return {"ok": True, "value": _json_safe(fn(dict(args or {}), workdir))}
    except AnalysisError as e:
        return _failure(e.code, e.context)
    except Exception as e:  # noqa: BLE001 - the text may hold data; keep the type
        if _is_out_of_memory(e):
            return _failure("OUT_OF_MEMORY")
        return _failure("INTERNAL", {"type": type(e).__name__})


def main(argv: List[str]) -> int:
    """``--workdir <dir> <function> <base64 JSON args>``; prints one JSON line."""
    try:
        if len(argv) != 4 or argv[0] != "--workdir":
            raise ValueError("usage")
        workdir, function, encoded = argv[1], argv[2], argv[3]
        args = json.loads(base64.b64decode(encoded).decode("utf-8"))
        result = dispatch(function, args, workdir)
    except Exception:  # noqa: BLE001 - never echo the arguments
        result = _failure("INVALID_ARGUMENT", {"field": "command"})
    # ASCII only: Code Interpreter breaks multibyte characters in a long stdout.
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    return 0


# Make sure codecs for the encodings we name are importable in the session.
codecs.lookup("cp932")
codecs.lookup("euc-jp")

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
