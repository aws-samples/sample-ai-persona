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
import hashlib
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import warnings
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
    # U6 (NFR3.35): counted before any AI-written code can run
    report["parquet_sha256"] = _file_sha256(out)
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
# profile_segments (U6 addendum: the features of a layer a fixed tool made)
# ---------------------------------------------------------------------------

_SEGMENT_COMPARISONS = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
_MAX_IN_VALUES = 100
_MAX_LAYERS = 20
_MISSING_KEY = "（欠損）"
_FILTERED_KEY = "filtered"


def _filter_value(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AnalysisError("INVALID_ARGUMENT", {"field": "filters"})
    return value


def _where_clause(filters: Any, names: set) -> Tuple[str, List[Any], set]:
    """``WHERE`` text, its parameters and the columns an ``eq`` filter fixes.

    The columns must be the table's, the operators are an allowlist and every
    value is a bound parameter, so nothing the AI wrote becomes SQL text.
    """
    if filters is None:
        return "", [], set()
    if not isinstance(filters, list):
        raise AnalysisError("INVALID_ARGUMENT", {"field": "filters"})
    parts: List[str] = []
    params: List[Any] = []
    fixed: set = set()
    for item in filters:
        if not isinstance(item, dict):
            raise AnalysisError("INVALID_ARGUMENT", {"field": "filters"})
        column, operator, value = (
            item.get("column"),
            item.get("operator"),
            item.get("value"),
        )
        if column not in names or not isinstance(operator, str):
            raise AnalysisError("INVALID_ARGUMENT", {"field": "filters"})
        col = _quote_ident(column)
        if operator in _SEGMENT_COMPARISONS:
            parts.append(f"{col} {_SEGMENT_COMPARISONS[operator]} ?")
            params.append(_filter_value(value))
            if operator == "eq":
                fixed.add(column)
        elif operator == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= _MAX_IN_VALUES:
                raise AnalysisError("INVALID_ARGUMENT", {"field": "filters"})
            parts.append(f"{col} IN ({', '.join('?' for _ in value)})")
            params.extend(_filter_value(v) for v in value)
            if len(value) == 1:
                fixed.add(column)
        elif operator == "contains":
            parts.append(f"contains(lower(CAST({col} AS VARCHAR)), lower(?))")
            params.append(str(_filter_value(value)))
        else:
            raise AnalysisError("INVALID_ARGUMENT", {"field": "filters"})
    return (" WHERE " + " AND ".join(parts)) if parts else "", params, fixed


def profile_segments(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Count each layer's size and features from the rows of a table.

    Args: ``source_id``, ``filters`` (``{column, operator, value}`` list) and
    ``by`` (a column). With ``by`` each value of the column (after the filters)
    is a layer, the 20 biggest at most; without it the filtered rows are one
    layer (``filtered``). Returns ``{population, layers: [{key, size, share,
    features}]}``. The features are those of the recount (``mean`` with
    ``spread`` and ``share``) against all rows of the table; the ``by`` column
    and the columns an equal filter fixes are left out.
    """
    timeout = float(args.get("timeout_seconds", 30))
    con = _open_dataset(workdir, args.get("source_id"))
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        schema = _schema(con)
        names = {name for name, _ in schema}
        by = args.get("by")
        if by is not None and by not in names:
            raise AnalysisError("INVALID_ARGUMENT", {"field": "by"})
        where, params, fixed = _where_clause(args.get("filters"), names)
        key = (
            f"coalesce(CAST({_quote_ident(by)} AS VARCHAR), {_sql_literal(_MISSING_KEY)})"
            if by is not None
            else _sql_literal(_FILTERED_KEY)
        )
        population = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
        con.execute(
            "CREATE VIEW d AS SELECT CAST(file_row_number AS VARCHAR) AS __pid, "
            "* FROM dataset"
        )
        con.execute(
            "CREATE TEMP TABLE a0 AS SELECT CAST(file_row_number AS VARCHAR) AS person_id, "
            f"{key} AS segment FROM dataset{where}",
            params,
        )
        con.execute(
            "CREATE VIEW a AS SELECT * FROM a0 WHERE segment IN (SELECT segment "
            f"FROM a0 GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT {_MAX_LAYERS})"
        )
        sizes = con.execute(
            "SELECT segment, count(*) FROM a GROUP BY 1 ORDER BY 2 DESC, 1"
        ).fetchall()
        features = _profile_views(con, schema, set(fixed) | ({by} if by else set()))
    except AnalysisError:
        raise
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
        "population": population,
        "layers": [
            {
                "key": str(key_),
                "size": int(size),
                "share": round(int(size) / population, 6) if population else 0.0,
                "features": features.get(str(key_), []),
            }
            for key_, size in sizes
        ],
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
        # Cells that have a value, per cluster: the shown mean divides by these
        self.valid: Dict[str, List[int]] = {}
        self.cats: Dict[str, List[Dict[str, int]]] = {}
        for name, numeric in features.columns:
            if numeric:
                self.sums[name] = [0.0] * k
                self.valid[name] = [0] * k
            else:
                self.cats[name] = [{} for _ in range(k)]

    def add(self, labels: Any, batch: Dict[str, List[Any]]) -> None:
        for label in labels:
            self.sizes[int(label)] += 1
        for name, numeric in self.features.columns:
            if numeric:
                # The clustering fills blanks with the mean; the shown
                # statistics must not (a blank is not a value)
                for label, v in zip(labels, batch[name]):
                    if v is None:
                        continue
                    self.sums[name][int(label)] += float(v)
                    self.valid[name][int(label)] += 1
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
                have = self.valid[name][label]
                have_all = sum(self.valid[name])
                if not have:
                    # Nothing to average: a "mean 0" would read as lower than all
                    continue
                out.append(
                    {
                        "name": name,
                        "kind": "mean",
                        "value": round(self.sums[name][label] / have, 4),
                        "baseline": round(total / have_all, 4),
                        "spread": round(float(self.features.stats[name][1]), 4),
                    }
                )
                continue
            bucket = self.cats[name][label]
            top = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            overall = sum(b.get(top, 0) for b in self.cats[name])
            out.append(
                {
                    "name": f"{name}={_short(top)}",
                    "kind": "share",
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
    share, features: [{name, kind, value, baseline}]}]}``; sizes sum to the population.
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
# U6: AI-written code (BR36), the check after it (NFR3.35) and the playbook
# functions (BR37.3, BR37.4, BR39.2)
# ---------------------------------------------------------------------------
#
# ``run_ai_code`` runs the code as its own process group in an empty folder
# ``ai_work/<random>``; the code gets the names of :func:`_ai_namespace` and
# hands its result to :func:`emit_result`, which writes the person IDs and
# their segment only. ``finalize_ai_result`` is run from the source of this
# file the app sends inside the command (``python -I -c``; nothing is written
# to disk, so a process the code left cannot swap it): it stops the group and
# anything else the code left, hashes the prepared Parquets and the session's
# own copy of this file, and counts the sizes and the population again from
# the data. Nothing the code printed or wrote as a number is used.

_SOURCE_ALIAS = re.compile(r"^source_[1-3]$")
_POPULATION_SOURCE = re.compile(
    r"^(source_[1-3])$|^join:source_[1-3](\+source_[1-3])+$"
)
_WORK_DIR = re.compile(r"^ai_work/[A-Za-z0-9_-]{1,64}$")
_PGID_FILE = re.compile(r"^ai_work/[A-Za-z0-9_-]{1,64}\.pgid$")
_AI_STDOUT_MAX_CHARS = 20_000
_AI_ERROR_MAX_CHARS = 4_000
_AI_DEFAULT_TIMEOUT_SECONDS = 120
_ASSIGNMENT_FILE = "assignment.parquet"
_META_FILE = "meta.json"
_EMIT_COUNT_FILE = "emit_count"
_MAX_STATISTICS = 50
_MAX_METRICS = 60
#: The columns the last ``join_tables`` of this process added (key first).
_LAST_JOINED: List[str] = []
_MAX_FEATURES = 20
_JOIN_FUNCS = {
    "count": "count({col})",
    "count_distinct": "count(DISTINCT {col})",
    "sum": "sum({col})",
    "mean": "avg({col})",
    "avg": "avg({col})",
    "min": "min({col})",
    "max": "max({col})",
    "first": "min({col})",
    "last": "max({col})",
}
# Characters that cannot be part of a name; letters of any script (Japanese
# included), digits and ``_`` stay (U6 addendum).
_NAME_PART = re.compile(r"[^\w]")


def _unique_name(name: str, used: set) -> str:
    """``name``, or ``name_2``, ``name_3``.. when it is taken (U6 addendum).

    ``used`` holds lower-cased names: DuckDB identifiers are case-insensitive.
    """
    out, n = name, 1
    while out.lower() in used:
        n += 1
        out = f"{name}_{n}"
    used.add(out.lower())
    return out


# What the code runs with (filled in by :func:`_ai_namespace`).
_AI_CONTEXT: Dict[str, Any] = {}

_BOOTSTRAP = """
import importlib.util, json, sys
lib_path, workdir, work, sources, code_path = sys.argv[1:6]
spec = importlib.util.spec_from_file_location("persona_analysis", lib_path)
lib = importlib.util.module_from_spec(spec)
sys.modules["persona_analysis"] = lib
spec.loader.exec_module(lib)
names = lib._ai_namespace(workdir, work, json.loads(sources))
with open(code_path, encoding="utf-8") as f:
    code = f.read()
exec(compile(code, "ai_code.py", "exec"), names)
"""


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_or_missing(path: str) -> str:
    return _file_sha256(path) if os.path.isfile(path) else "missing"


def _checked_sources(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise AnalysisError("INVALID_ARGUMENT", {"field": "sources"})
    return {
        str(alias): _checked_id(source_id, "sources")
        for alias, source_id in value.items()
        if _SOURCE_ALIAS.match(str(alias))
    }


def _checked_relative(value: Any, pattern: "re.Pattern[str]", name: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise AnalysisError("INVALID_ARGUMENT", {"field": name})
    return value


def _ai_namespace(workdir: str, work: str, sources: Dict[str, str]) -> Dict[str, Any]:
    """The names AI-written code sees (the tested functions, bound here)."""
    paths = {
        str(alias): _parquet_path(workdir, _checked_id(source_id, "sources"))
        for alias, source_id in sources.items()
        if _SOURCE_ALIAS.match(str(alias))
    }
    _AI_CONTEXT.clear()
    _AI_CONTEXT.update(workdir=workdir, work=work, paths=paths)
    # The tables read through read_table / join_tables, for the record: a
    # merge of the code's own leaves no joined columns, yet used both tables.
    read: List[str] = []

    def note(alias: Any) -> None:
        if alias in paths and alias not in read:
            read.append(alias)

    def read_table(alias: str) -> Any:
        """The prepared table ``alias`` as a pandas DataFrame."""
        import duckdb

        if alias not in paths:
            raise KeyError(f"unknown table {alias}; use one of {sorted(paths)}")
        note(alias)
        con = duckdb.connect(":memory:")
        try:
            return con.execute(
                f"SELECT * FROM read_parquet({_sql_literal(paths[alias])})"
            ).df()
        finally:
            con.close()

    def emit(frame: Any, assign_column: str, population_source: str, **kw: Any) -> None:
        kw["tables_read"] = list(read)  # what was read, never what the code says
        emit_result(frame, assign_column, population_source, out_dir=work, **kw)

    def join(axis: str, key: str, others: List[Dict[str, Any]]) -> Any:
        frame = join_tables(axis, key, others, paths=paths)
        note(axis)
        for other in others or []:
            if isinstance(other, dict):
                note(other.get("source"))
        return frame

    return {
        "__name__": "__ai_code__",
        "SOURCES": dict(paths),
        "read_table": read_table,
        "emit_result": emit,
        "join_tables": join,
        "compare_groups": compare_groups,
        "rfm": rfm,
        "person_profile": person_profile,
    }


def _columns_of(frame: Any, *names: str) -> Dict[str, List[Any]]:
    """Columns of a pandas DataFrame, a pyarrow Table or a dict of lists."""
    if hasattr(frame, "to_pydict"):
        data = frame.to_pydict()
    elif hasattr(frame, "columns") and hasattr(frame, "__getitem__"):
        data = {str(c): list(frame[c]) for c in frame.columns if str(c) in names}
    elif isinstance(frame, dict):
        data = {str(k): list(v) for k, v in frame.items()}
    else:
        raise TypeError("emit_result needs a DataFrame, a pyarrow Table or a dict")
    missing = [n for n in names if n not in data]
    if missing:
        raise KeyError(f"emit_result: no column {missing[0]}")
    return {n: data[n] for n in names}


def _numeric_columns(frame: Any, skip: Tuple[str, ...]) -> List[str]:
    """Names of the numeric columns of a pandas DataFrame (none for other frames)."""
    if not (hasattr(frame, "select_dtypes") and hasattr(frame, "columns")):
        return []
    try:
        numeric = frame.select_dtypes(include="number")
    except Exception:  # noqa: BLE001 - a frame that cannot say its dtypes
        return []
    return [str(c) for c in numeric.columns if str(c) not in skip]


def _text_or_none(value: Any) -> Optional[str]:
    """An ID or a segment as text; missing values (None, NaN, NA, NaT) stay None.

    Otherwise a pandas missing value would become the text ``nan`` and pass
    the check as a segment (or a person) of its own.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        import pandas as pd

        if pd.isna(value) is True:
            return None
    except Exception:  # noqa: BLE001 - no pandas, or a value it cannot judge
        pass
    return str(value)


def _metric_value(value: Any) -> Optional[float]:
    """A number the code computed as a float; text, bools and NaN are no metric."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _short_features(features: Any) -> Dict[str, List[Dict[str, Any]]]:
    if not isinstance(features, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, items in list(features.items())[:_MAX_FEATURES]:
        if isinstance(items, list):
            out[str(key)[:_VALUE_MAX_CHARS]] = [
                {str(k)[:_VALUE_MAX_CHARS]: _short(v) for k, v in dict(i).items()}
                for i in items[:_MAX_FEATURES]
                if isinstance(i, dict)
            ]
    return out


def emit_result(
    frame: Any,
    assign_column: str,
    population_source: str,
    *,
    id_column: str,
    statistics: Optional[List[Dict[str, Any]]] = None,
    joined_columns: Optional[List[str]] = None,
    features: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    metrics: Optional[List[str]] = None,
    out_dir: Optional[str] = None,
    tables_read: Optional[List[str]] = None,
) -> None:
    """Hand the result of AI-written code over, once (BR36.3).

    ``frame`` has one row per person: ``id_column`` names the person (for a
    joined table, the axis table's ID column) and ``assign_column`` the
    segment of the row. Only the IDs and the segments are written; sizes and
    the population are counted again from the data by the check.
    ``statistics`` and ``features`` are kept as passed and are NOT recounted
    (the app never lets them back an evidence); ``features`` is not used at all,
    the library counts the features itself. ``metrics`` names numeric columns
    of ``frame`` the code computed (such as ``recency``); their mean in each
    segment is shown as ``derived`` features (the values are the code's own).
    Left out, every numeric column of a pandas frame counts (the columns of the
    axis table are counted by the library itself and are not repeated).
    ``tables_read`` is set by the sandbox (the tables the code read), not by
    the code.

    Raises:
        RuntimeError: Called a second time.
        KeyError / TypeError / ValueError: The frame does not fit.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    folder = out_dir or os.getcwd()
    count_path = os.path.join(folder, _EMIT_COUNT_FILE)
    previous = 0
    if os.path.exists(count_path):
        with open(count_path, encoding="utf-8") as f:
            previous = int(f.read().strip() or 0)
    with open(count_path, "w", encoding="utf-8") as f:
        f.write(str(previous + 1))
    if previous:
        raise RuntimeError("emit_result is called once per run")
    if not _POPULATION_SOURCE.match(str(population_source)):
        raise ValueError("population_source is source_n or join:source_n+source_m")
    # Those of the axis table are dropped when the features are counted.
    if metrics is None:
        metrics = _numeric_columns(frame, (id_column, assign_column))
    names = [str(m) for m in metrics][:_MAX_METRICS]
    columns = _columns_of(frame, id_column, assign_column, *names)
    if joined_columns is None:
        attrs = getattr(frame, "attrs", None)
        joined_columns = list(attrs.get("joined_columns", [])) if attrs else []
        if not joined_columns and str(population_source).startswith("join:"):
            joined_columns = list(_LAST_JOINED)
    data: Dict[str, Any] = {
        "person_id": [_text_or_none(v) for v in columns[id_column]],
        "segment": [_text_or_none(v) for v in columns[assign_column]],
    }
    for index, name in enumerate(names):
        data[f"metric_{index}"] = [_metric_value(v) for v in columns[name]]
    table = pa.table(data)
    pq.write_table(table, os.path.join(folder, _ASSIGNMENT_FILE))
    meta = {
        "population_source": str(population_source),
        "id_column": str(id_column),
        "statistics": _json_safe(list(statistics or [])[:_MAX_STATISTICS]),
        "joined_columns": [str(c)[:_VALUE_MAX_CHARS] for c in joined_columns][:50],
        "features": _short_features(features),
        "metrics": [str(m)[:_VALUE_MAX_CHARS] for m in names],
        "tables_read": [str(t)[:_VALUE_MAX_CHARS] for t in tables_read or []][:10],
    }
    with open(os.path.join(folder, _META_FILE), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=True)


def run_ai_code(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Run AI-written code as its own process group (BR36.1, NFR3.35).

    Args: ``code``, ``work_dir`` (``ai_work/<random>``, must not exist),
    ``pgid_file`` (``ai_work/<random>.pgid``, outside that folder),
    ``sources`` (alias -> source_id) and ``timeout_seconds``.
    Returns ``{status, exit_code, timed_out, stdout, error_text}``; the app
    reads only whether it ended, the output goes to the agent as data.
    """
    code = args.get("code")
    if not isinstance(code, str):
        raise AnalysisError("INVALID_ARGUMENT", {"field": "code"})
    work_rel = _checked_relative(args.get("work_dir"), _WORK_DIR, "work_dir")
    pgid_rel = _checked_relative(args.get("pgid_file"), _PGID_FILE, "pgid_file")
    sources = _checked_sources(args.get("sources"))
    timeout = int(args.get("timeout_seconds", _AI_DEFAULT_TIMEOUT_SECONDS))
    work = _inside(workdir, work_rel)
    if os.path.exists(work):
        raise AnalysisError("INVALID_ARGUMENT", {"field": "work_dir"})
    os.makedirs(work)
    code_path = os.path.join(work, "ai_code.py")
    with open(code_path, "w", encoding="utf-8") as f:
        f.write(code)
    # Files, not pipes: a process the code leaves behind keeps a pipe open,
    # and the wait is for the code's own process only.
    base = _inside(workdir, pgid_rel[: -len(".pgid")])
    with (
        open(base + ".out", "w", encoding="utf-8") as out,
        open(base + ".err", "w", encoding="utf-8") as err,
    ):
        process = subprocess.Popen(  # noqa: S603 - the sandbox's own interpreter
            [
                sys.executable,
                "-c",
                _BOOTSTRAP,
                os.path.abspath(__file__),
                os.path.abspath(workdir),
                work,
                json.dumps(sources),
                code_path,
            ],
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        with open(_inside(workdir, pgid_rel), "w", encoding="utf-8") as f:
            f.write(str(process.pid))
        timed_out = False
        try:
            process.wait(timeout=max(timeout, 1))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process.pid)
            process.wait()
    ok = process.returncode == 0 and not timed_out
    return {
        "status": "ok" if ok else "failed",
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": _tail(base + ".out", _AI_STDOUT_MAX_CHARS),
        "error_text": _tail(base + ".err", _AI_ERROR_MAX_CHARS),
    }


def _tail(path: str, max_chars: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[-max_chars:]
    except OSError:
        return ""


def _kill_group(pgid: int) -> None:
    """Stop the process group ``pgid`` (never this process's own group)."""
    if pgid <= 1 or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _read_recorded(workdir: str, pgid_rel: Any) -> Optional[int]:
    """The pid in a ``.pgid`` file: also the run's session and group ID.

    ``run_ai_code`` starts the code with ``start_new_session``, so the pid it
    records leads its own session and process group.
    """
    if not isinstance(pgid_rel, str) or not _PGID_FILE.match(pgid_rel):
        return None
    path = _inside(workdir, pgid_rel)
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# Security review: AI-written code can ``fork()`` and ``setsid()`` so a child
# survives the group stop and stays in the session (and could swap files
# between two checks). Every check therefore lists the processes, stops those
# the code left and reports how many there were; the app treats any as a
# change. ``/proc`` is read in the session (Linux); ``ps`` is the fallback of
# machines without it (macOS, tests), which knows no session ID nor folder.
_PROC_ROOT = "/proc"
_PS = "/bin/ps"
_SCAN_ROUNDS = 5
_GONE_WAIT_SECONDS = 2.0
_GONE_POLL_SECONDS = 0.02


def _proc_table(proc_root: str) -> Optional[Dict[int, Dict[str, Any]]]:
    """Every process under ``proc_root``, or ``None`` when it cannot be read.

    Each entry holds ``ppid``, ``pgid``, ``sid``, ``cwd`` (``""`` when hidden),
    ``cmdline`` (arguments joined by spaces) and ``zombie``. A table without
    this process is not trusted (``None``).
    """
    try:
        names = os.listdir(proc_root)
    except OSError:
        return None
    table: Dict[int, Dict[str, Any]] = {}
    for name in names:
        if not name.isdigit():
            continue
        base = os.path.join(proc_root, name)
        try:
            with open(
                os.path.join(base, "stat"), encoding="utf-8", errors="replace"
            ) as f:
                stat = f.read()
            # The command name may hold ")" and spaces: read after the last one.
            fields = stat[stat.rindex(")") + 1 :].split()
            state, ppid, pgid, sid = fields[0], *map(int, fields[1:4])
        except (OSError, ValueError, IndexError):
            continue  # gone meanwhile, or not a process entry
        try:
            cwd = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            cwd = ""
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                raw = f.read()
            cmdline = raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        table[int(name)] = {
            "ppid": ppid,
            "pgid": pgid,
            "sid": sid,
            "cwd": cwd,
            "cmdline": cmdline,
            "zombie": state in ("Z", "X"),
        }
    return table if os.getpid() in table else None


def _ps_table(ps: str) -> Optional[Dict[int, Dict[str, Any]]]:
    """The same table from ``ps`` (no session ID, no folder), or ``None``."""
    try:
        listed = subprocess.run(  # noqa: S603 - a fixed absolute path, no shell
            [ps, "-A", "-o", "pid=,ppid=,pgid=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    table: Dict[int, Dict[str, Any]] = {}
    for line in listed.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        table[pid] = {
            "ppid": ppid,
            "pgid": pgid,
            "sid": None,
            "cwd": "",
            "cmdline": parts[4] if len(parts) > 4 else "",
            "zombie": parts[3].startswith("Z"),
        }
    return table if os.getpid() in table else None


def _process_table() -> Optional[Dict[int, Dict[str, Any]]]:
    if os.path.isdir(_PROC_ROOT):
        return _proc_table(_PROC_ROOT)
    return _ps_table(_PS)


def _protected(table: Dict[int, Dict[str, Any]], self_pid: int) -> set:
    """This process, its ancestors and pid 0 / 1: never stopped."""
    keep = {0, 1, self_pid}
    pid = self_pid
    while pid in table and table[pid]["ppid"] not in keep:
        pid = table[pid]["ppid"]
        keep.add(pid)
    return keep


def _find_leftovers(
    table: Dict[int, Dict[str, Any]],
    recorded: set,
    ai_root: str,
    *,
    self_pid: int,
) -> List[int]:
    """Live processes the code left: by its run, its session, group or folder.

    A process counts when it is a recorded run, shares a recorded session or
    group, descends from a recorded run, works inside ``ai_root`` or names
    ``ai_root`` in its command. This process and its ancestors never count.
    """
    keep = _protected(table, self_pid)
    runs = {r for r in recorded if r not in keep}
    inside = ai_root.rstrip(os.sep) + os.sep
    found: List[int] = []
    for pid, info in table.items():
        if pid in keep or info["zombie"]:
            continue
        cwd = str(info.get("cwd") or "")
        hit = (
            pid in runs
            or info.get("sid") in runs
            or info.get("pgid") in runs
            or cwd == inside[:-1]
            or cwd.startswith(inside)
            or inside in str(info.get("cmdline") or "") + os.sep
        )
        parent, seen = info["ppid"], {pid}
        while not hit and parent in table and parent not in seen:
            hit = parent in runs
            seen.add(parent)
            parent = table[parent]["ppid"]
        if hit:
            found.append(pid)
    return found


def _wait_gone(pids: set, groups: set) -> None:
    """Wait a little for stopped processes to end (a zombie counts as ended)."""
    deadline = time.monotonic() + _GONE_WAIT_SECONDS
    while time.monotonic() < deadline:
        table = _process_table()
        if table is None:
            return
        keep = _protected(table, os.getpid())
        if not any(
            not info["zombie"]
            and pid not in keep
            and (pid in pids or info.get("pgid") in groups)
            for pid, info in table.items()
        ):
            return
        time.sleep(_GONE_POLL_SECONDS)


def _stop_leftovers(workdir: str, pgid_files: List[Any]) -> Dict[str, Any]:
    """Stop the recorded groups, then find and stop what is still left.

    Returns ``{process_scan: "ok", leftover_processes: n}``, ``n`` counting the
    processes that survived the group stop, or ``{process_scan:
    "unavailable"}`` when the processes cannot be listed (the app fails closed).
    """
    recorded = set()
    for pgid_rel in pgid_files:
        pid = _read_recorded(workdir, pgid_rel)
        if pid is not None and pid > 1:
            recorded.add(pid)
    for pid in recorded:
        _kill_group(pid)
    _wait_gone(set(), recorded)
    ai_root = os.path.join(os.path.realpath(workdir), "ai_work")
    left: set = set()
    for _ in range(_SCAN_ROUNDS):
        table = _process_table()
        if table is None:
            return {"process_scan": "unavailable"}
        found = _find_leftovers(table, recorded, ai_root, self_pid=os.getpid())
        if not found:
            break
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        left.update(found)
        _wait_gone(set(found), set())
    return {"process_scan": "ok", "leftover_processes": len(left)}


def _hashes(workdir: str, sources: Dict[str, str], library: Any) -> Dict[str, Any]:
    return {
        "library": (
            _hash_or_missing(str(library)) if isinstance(library, str) else "missing"
        ),
        "parquet": {
            source_id: _hash_or_missing(_parquet_path(workdir, source_id))
            for source_id in sources.values()
        },
    }


_PROFILE_MAX_COLUMNS = 400
_PROFILE_MAX_VALUES = 30
_PROFILE_CHUNK = 40
#: Column names that say a number or a text is a key, not a trait (kept in step
#: with ``analysis_policy._ID_NAME`` in the app, plus codes and numbers).
_KEY_NAME = re.compile(
    r"(?<![A-Za-z])[Ii][Dd](?![a-z])|(?<=[a-z])I[Dd](?![a-z])|(?<=[a-z])ID(?![A-Za-z])"
    r"|ＩＤ|番号|コード|郵便|(?<![A-Za-z])(?i:no|num|code|zip|postal)(?![A-Za-z])"
)


def _chunks(items: List[Any], size: int) -> Any:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _profile_features(
    con: Any, source: str, id_column: str, metrics: Optional[List[str]] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """Features of each segment, counted from the axis table (U6 addendum).

    Per segment and per column of the axis table (the ID column and columns
    named like a key left out): the mean of a numeric column (``kind``
    ``mean``, with ``spread``, the standard deviation of all rows) and the
    share of the commonest value of a category column (``kind`` ``share``),
    each with the figure of all rows (``baseline``). Every column is read, a
    chunk of columns per query; which of them matter is decided by the app
    from the sizes of the differences. The code's own ``features`` are never
    used.
    """
    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet({_sql_literal(source)})"
    ).fetchall()
    con.execute(
        f"CREATE VIEW d AS SELECT CAST({_quote_ident(id_column)} AS VARCHAR) "
        f"AS __pid, * FROM read_parquet({_sql_literal(source)})"
    )
    return _profile_views(con, schema, {id_column}, metrics)


def _profile_views(
    con: Any,
    schema: List[Any],
    skip: set,
    metrics: Optional[List[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Features per segment from the views ``d`` (all rows, with ``__pid``) and
    ``a`` (``person_id``, ``segment``): the part shared by the recount and
    :func:`profile_segments`. ``skip`` names the columns that are no trait."""
    total = int(con.execute("SELECT count(*) FROM d").fetchone()[0])
    sizes = {
        str(k): int(n)
        for k, n in con.execute("SELECT segment, count(*) FROM a GROUP BY 1").fetchall()
    }
    out: Dict[str, List[Dict[str, Any]]] = {key: [] for key in sizes}
    numeric: List[str] = []
    categories: List[str] = []
    for row in schema:
        name, type_name = str(row[0]), str(row[1])
        if name in skip or name == "file_row_number" or _KEY_NAME.search(name):
            continue
        if _is_numeric(type_name):
            numeric.append(name)
        elif type_name in ("VARCHAR", "BOOLEAN"):
            categories.append(name)
    numeric = numeric[:_PROFILE_MAX_COLUMNS]
    categories = categories[:_PROFILE_MAX_COLUMNS]
    join = "FROM d JOIN a ON d.__pid = a.person_id"

    for chunk in _chunks(numeric, _PROFILE_CHUNK):
        cols = [_quote_ident(n) for n in chunk]
        overall = con.execute(
            "SELECT "
            + ", ".join(
                f"avg(CAST({c} AS DOUBLE)), stddev_pop(CAST({c} AS DOUBLE))"
                for c in cols
            )
            + " FROM d"
        ).fetchone()
        per_segment = con.execute(
            "SELECT a.segment, "
            + ", ".join(f"avg(CAST(d.{c} AS DOUBLE))" for c in cols)
            + f" {join} GROUP BY 1"
        ).fetchall()
        for index, name in enumerate(chunk):
            mean_all, spread = overall[2 * index], overall[2 * index + 1]
            if mean_all is None:
                continue
            for record in per_segment:
                mean = record[1 + index]
                if mean is not None:
                    out[str(record[0])].append(
                        {
                            "name": _short(name),
                            "kind": "mean",
                            "value": round(float(mean), 4),
                            "baseline": round(float(mean_all), 4),
                            "spread": round(float(spread or 0.0), 4),
                        }
                    )

    for chunk in _chunks(categories, _PROFILE_CHUNK):
        cols = [_quote_ident(n) for n in chunk]
        kinds = con.execute(
            "SELECT " + ", ".join(f"count(DISTINCT {c})" for c in cols) + " FROM d"
        ).fetchone()
        usable = [
            (name, col)
            for name, col, k in zip(chunk, cols, kinds)
            if 2 <= int(k) <= _PROFILE_MAX_VALUES and int(k) < total
        ]
        if not usable:
            continue
        union = " UNION ALL ".join(
            f"SELECT a.segment AS seg, {_sql_literal(name)} AS col, "
            f"CAST(d.{col} AS VARCHAR) AS val, count(*) AS n, false AS whole {join} "
            f"WHERE d.{col} IS NOT NULL GROUP BY 1, 2, 3"
            f" UNION ALL SELECT '', {_sql_literal(name)}, "
            f"CAST({col} AS VARCHAR), count(*), true FROM d "
            f"WHERE {col} IS NOT NULL GROUP BY 2, 3"
            for name, col in usable
        )
        counts: Dict[str, Dict[str, Dict[str, int]]] = {}
        everyone: Dict[str, Dict[str, int]] = {}
        for seg, col_name, value, n, whole_row in con.execute(union).fetchall():
            if whole_row:
                everyone.setdefault(str(col_name), {})[str(value)] = int(n)
            else:
                counts.setdefault(str(col_name), {}).setdefault(str(seg), {})[
                    str(value)
                ] = int(n)
        for name, _ in usable:
            by_segment = counts.get(name, {})
            whole = everyone.get(name, {})
            for key, values in by_segment.items():
                value, n = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                out[key].append(
                    {
                        "name": f"{_short(name)}={_short(value)}",
                        "kind": "share",
                        "value": round(n / sizes[key], 4),
                        "baseline": round(whole.get(value, 0) / total, 4),
                    }
                )

    profiled = {str(row[0]) for row in schema}
    derived = 0
    for index, name in enumerate(metrics or []):
        if name in profiled:
            continue  # a column of the axis table is counted above
        if derived >= _MAX_METRICS:
            break
        col = f"metric_{index}"
        try:
            overall_row = con.execute(
                f"SELECT avg({col}), stddev_pop({col}) FROM a"
            ).fetchone()
            rows = con.execute(
                f"SELECT segment, avg({col}) FROM a GROUP BY 1"
            ).fetchall()
        except Exception as e:  # noqa: BLE001 - a result file without the column
            if _is_out_of_memory(e):
                raise
            continue
        if overall_row[0] is None:
            continue
        derived += 1
        for key, mean in rows:
            if mean is not None and str(key) in out:
                out[str(key)].append(
                    {
                        "name": _short(name),
                        "kind": "derived",
                        "value": round(float(mean), 4),
                        "baseline": round(float(overall_row[0]), 4),
                        "spread": round(float(overall_row[1] or 0.0), 4),
                    }
                )
    return out


def _recount(
    workdir: str, work: str, sources: Dict[str, str]
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """Count sizes and the population again from the assignment and the data.

    Returns ``(result, error, error_context)``; the context holds counts only.
    """
    count_path = os.path.join(work, _EMIT_COUNT_FILE)
    assignment = os.path.join(work, _ASSIGNMENT_FILE)
    if not os.path.isfile(assignment):
        return None, "NO_RESULT", None
    try:
        with open(count_path, encoding="utf-8") as f:
            calls = int(f.read().strip() or 0)
        with open(os.path.join(work, _META_FILE), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None, "RESULT_INVALID", None
    if calls != 1:
        return None, "EMIT_TWICE" if calls > 1 else "NO_RESULT", None
    population_source = str(meta.get("population_source", ""))
    match = _POPULATION_SOURCE.match(population_source)
    if not isinstance(meta, dict) or not match:
        return None, "RESULT_INVALID", None
    axis = match.group(1) or population_source[len("join:") :].split("+")[0]
    if axis not in sources:
        return None, "RESULT_INVALID", None
    id_column = str(meta.get("id_column", ""))
    source = _parquet_path(workdir, sources[axis])
    con = _connect(workdir)
    try:
        con.execute(
            f"CREATE VIEW a AS SELECT * FROM read_parquet({_sql_literal(assignment)})"
        )
        con.execute(
            "CREATE VIEW s AS SELECT CAST("
            f"{_quote_ident(id_column)} AS VARCHAR) AS person_id "
            f"FROM read_parquet({_sql_literal(source)})"
        )
        n_a, d_a, null_a, n_s, d_s, null_s = con.execute(
            "SELECT (SELECT count(*) FROM a), (SELECT count(DISTINCT person_id) "
            "FROM a), (SELECT count(*) FROM a WHERE person_id IS NULL OR segment "
            "IS NULL), (SELECT count(*) FROM s), (SELECT count(DISTINCT person_id) "
            "FROM s WHERE person_id IS NOT NULL), (SELECT count(*) FROM s WHERE "
            "person_id IS NULL)"
        ).fetchone()
        # Counts only (never values): they tell the agent what to fix.
        if null_s:
            return None, "POPULATION_ID_MISSING", {"rows_without_id": int(null_s)}
        if null_a:
            return None, "UNASSIGNED", {"rows_unassigned": int(null_a)}
        # The population first: a history table as the population is the
        # usual cause, and the frame built from it repeats people as well.
        if n_s != d_s:
            return (
                None,
                "DUPLICATE_IDS",
                {"side": "population", "rows": int(n_s), "distinct_ids": int(d_s)},
            )
        if n_a != d_a:
            return (
                None,
                "DUPLICATE_IDS",
                {"side": "result", "rows": int(n_a), "distinct_ids": int(d_a)},
            )
        missing, unknown = con.execute(
            "SELECT (SELECT count(*) FROM s WHERE person_id NOT IN "
            "(SELECT person_id FROM a)), (SELECT count(*) FROM a WHERE person_id "
            "NOT IN (SELECT person_id FROM s))"
        ).fetchone()
        if missing or unknown or n_a != n_s:
            return (
                None,
                "ID_MISMATCH",
                {
                    "population_rows": int(n_s),
                    "result_rows": int(n_a),
                    "not_in_result": int(missing),
                    "not_in_population": int(unknown),
                },
            )
        sizes = con.execute(
            "SELECT segment, count(*) FROM a GROUP BY segment ORDER BY 2 DESC, 1"
        ).fetchall()
        metric_names = meta.get("metrics")
        try:
            profile = _profile_features(
                con,
                source,
                id_column,
                [str(m) for m in metric_names]
                if isinstance(metric_names, list)
                else [],
            )
        except Exception as e:  # noqa: BLE001 - the features are a help, not a result
            if _is_out_of_memory(e):
                raise
            profile = {}
    except Exception as e:  # noqa: BLE001 - a missing column or a bad file
        if _is_out_of_memory(e):
            raise AnalysisError("OUT_OF_MEMORY") from None
        return None, "RESULT_INVALID", None
    finally:
        con.close()
    result: Dict[str, Any] = {
        "population": int(n_s),
        "population_source": population_source,
        "segments": [
            {
                "segment_key": str(key)[:_VALUE_MAX_CHARS],
                "size": int(size),
                "features": list(profile.get(str(key)) or []),
            }
            for key, size in sizes
        ],
    }
    statistics = meta.get("statistics")
    if isinstance(statistics, list) and statistics:
        result["statistics"] = [s for s in statistics if isinstance(s, dict)]
    joined = meta.get("joined_columns")
    if isinstance(joined, list) and joined:
        result["joined_columns"] = [str(c) for c in joined]
    read = meta.get("tables_read")
    if isinstance(read, list):
        # Only the tables of this run (the meta file is the code's to write)
        known = sorted({str(t) for t in read if str(t) in sources})
        if known:
            result["tables_read"] = known
    return result, None, None


def finalize_ai_result(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """The check after one ``run_ai_code`` (NFR3.35 (4)).

    Run from the source the app sent with the command (``python -I -c``).
    Stops the code's process group and anything else it left (see
    :func:`_stop_leftovers`), hashes the prepared Parquets and
    ``session_library``, then recounts the assignment. Returns ``{hashes,
    result, error, error_context, process_scan[, leftover_processes]}``.
    """
    work_rel = _checked_relative(args.get("work_dir"), _WORK_DIR, "work_dir")
    sources = _checked_sources(args.get("sources"))
    processes = _stop_leftovers(workdir, [args.get("pgid_file")])
    hashes = _hashes(workdir, sources, args.get("session_library"))
    result, error, context = _recount(workdir, _inside(workdir, work_rel), sources)
    return {
        "hashes": hashes,
        "result": result,
        "error": error,
        "error_context": context,
        **processes,
    }


def verify_hashes(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """Stop what is left and return the hashes (before the synthesis).

    Returns ``{hashes, process_scan[, leftover_processes]}``.
    """
    sources = _checked_sources(args.get("sources"))
    processes = _stop_leftovers(workdir, list(args.get("pgid_files") or []))
    return {
        "hashes": _hashes(workdir, sources, args.get("session_library")),
        **processes,
    }


def key_overlap(args: Dict[str, Any], workdir: str) -> Dict[str, Any]:
    """How far the values of same-named columns meet (BR37.3). No values.

    Args: ``axis`` and ``other`` (source IDs), ``columns``.
    Returns ``{columns: [{column, match_rate, overlap, axis_matched}]}``:
    ``match_rate`` is the share of the smaller table's distinct values found in
    the other; ``axis_matched`` the axis rows whose value the other has.
    """
    axis = _parquet_path(workdir, _checked_id(args.get("axis"), "axis"))
    other = _parquet_path(workdir, _checked_id(args.get("other"), "other"))
    columns = [str(c) for c in list(args.get("columns") or [])][:20]
    if not os.path.isfile(axis) or not os.path.isfile(other):
        raise AnalysisError("SOURCE_UNREADABLE")
    con = _connect(workdir)
    out: List[Dict[str, Any]] = []
    try:
        con.execute(
            f"CREATE VIEW x AS SELECT * FROM read_parquet({_sql_literal(axis)})"
        )
        con.execute(
            f"CREATE VIEW y AS SELECT * FROM read_parquet({_sql_literal(other)})"
        )
        names_x = {str(r[0]) for r in con.execute("DESCRIBE x").fetchall()}
        names_y = {str(r[0]) for r in con.execute("DESCRIBE y").fetchall()}
        rows_x = int(con.execute("SELECT count(*) FROM x").fetchone()[0])
        rows_y = int(con.execute("SELECT count(*) FROM y").fetchone()[0])
        for name in columns:
            if name not in names_x or name not in names_y:
                out.append(
                    {"column": name, "match_rate": 0.0, "overlap": 0, "axis_matched": 0}
                )
                continue
            col = _quote_ident(name)
            vx = f"SELECT DISTINCT CAST({col} AS VARCHAR) AS v FROM x WHERE {col} IS NOT NULL"
            vy = f"SELECT DISTINCT CAST({col} AS VARCHAR) AS v FROM y WHERE {col} IS NOT NULL"
            dx, dy, overlap = con.execute(
                f"SELECT (SELECT count(*) FROM ({vx})), (SELECT count(*) FROM ({vy})), "
                f"(SELECT count(*) FROM ({vx}) WHERE v IN ({vy}))"
            ).fetchone()
            matched = con.execute(
                f"SELECT count(*) FROM x WHERE CAST({col} AS VARCHAR) IN ({vy})"
            ).fetchone()[0]
            smaller = dx if rows_x <= rows_y else dy
            out.append(
                {
                    "column": name,
                    "match_rate": round(int(overlap) / int(smaller), 4)
                    if smaller
                    else 0.0,
                    "overlap": int(overlap),
                    "axis_matched": int(matched),
                }
            )
    finally:
        con.close()
    return {"columns": out}


def join_tables(
    axis: str,
    key: str,
    others: List[Dict[str, Any]],
    *,
    paths: Optional[Dict[str, str]] = None,
) -> Any:
    """Left join onto the axis table, one row per person (BR37.4).

    Each of ``others`` is ``{"source": alias, "key": column?, "aggregations":
    [{"column", "func", "as"?}]}``; the other table is first reduced to one
    row per key with the aggregations (count, count_distinct, sum, mean, min,
    max, first, last). People without a match stay: ``<alias>_rows`` is 0,
    ``<alias>_matched`` False and the other columns empty. The result is a
    pandas DataFrame whose ``attrs["joined_columns"]`` names the key and the
    columns added.
    """
    import duckdb

    tables = paths if paths is not None else dict(_AI_CONTEXT.get("paths") or {})
    if axis not in tables:
        raise KeyError(f"unknown table {axis}")
    selects = ["a.*"]
    joins: List[str] = []
    added: List[str] = []
    con = duckdb.connect(":memory:")
    try:
        described = con.execute(
            f"SELECT * FROM read_parquet({_sql_literal(tables[axis])}) LIMIT 0"
        ).description
    finally:
        con.close()
    used = {str(d[0]).lower() for d in described or []}
    for n, spec in enumerate(others):
        alias = str(spec.get("source"))
        if alias not in tables or alias == axis:
            raise KeyError(f"unknown table {alias}")
        other_key = _quote_ident(str(spec.get("key") or key))
        parts = [f"{other_key} AS __k", "count(*) AS __n"]
        for agg in list(spec.get("aggregations") or []):
            func = str(agg.get("func", "")).lower()
            if func not in _JOIN_FUNCS:
                raise ValueError(f"unknown aggregation {func}")
            column = str(agg.get("column", ""))
            name = _unique_name(
                str(agg.get("as") or f"{alias}_{_NAME_PART.sub('_', column)}_{func}"),
                used,
            )
            parts.append(
                _JOIN_FUNCS[func].format(col=_quote_ident(column))
                + f" AS {_quote_ident(name)}"
            )
            selects.append(f"b{n}.{_quote_ident(name)}")
            added.append(name)
        rows_name = _unique_name(f"{alias}_rows", used)
        matched_name = _unique_name(f"{alias}_matched", used)
        selects += [
            f"coalesce(b{n}.__n, 0) AS {_quote_ident(rows_name)}",
            f"(b{n}.__k IS NOT NULL) AS {_quote_ident(matched_name)}",
        ]
        added += [rows_name, matched_name]
        joins.append(
            f"LEFT JOIN (SELECT {', '.join(parts)} FROM "
            f"read_parquet({_sql_literal(tables[alias])}) GROUP BY {other_key}) b{n} "
            f"ON CAST(a.{_quote_ident(key)} AS VARCHAR) = CAST(b{n}.__k AS VARCHAR)"
        )
    con = duckdb.connect(":memory:")
    try:
        frame = con.execute(
            f"SELECT {', '.join(selects)} FROM "
            f"read_parquet({_sql_literal(tables[axis])}) a {' '.join(joins)}"
        ).df()
    finally:
        con.close()
    frame.attrs["joined_columns"] = [key, *added]
    # A frame built from this one (a merge, a groupby) loses its attrs; the
    # record of what was joined must not depend on that.
    _LAST_JOINED[:] = [key, *added]
    return frame


def compare_groups(
    frame: Any, group_column: str, columns: List[str]
) -> List[Dict[str, Any]]:
    """Tests of differences between groups (BR39.2).

    Numeric columns: Welch's t-test and Cohen's d for two groups (each group
    against the rest for more). Other columns: the chi-square test and
    Cramér's V. Returns ``[{name, column, value, p_value, effect_size}]``.
    """
    import numpy as np
    from scipy import stats as st

    groups = [g for g in frame[group_column].dropna().unique()]
    out: List[Dict[str, Any]] = []
    for column in columns:
        series = frame[column]
        numeric = str(series.dtype).startswith(("int", "float", "uint", "Int", "Float"))
        if numeric:
            pairs = (
                [(groups[0], groups[1])]
                if len(groups) == 2
                else [(g, None) for g in groups]
            )
            for first, second in pairs:
                a = series[frame[group_column] == first].dropna().astype(float)
                b = (
                    (
                        series[frame[group_column] == second]
                        if second is not None
                        else series[frame[group_column] != first]
                    )
                    .dropna()
                    .astype(float)
                )
                if len(a) < 2 or len(b) < 2:
                    continue
                test = st.ttest_ind(a, b, equal_var=False)
                pooled = np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2)
                effect = float((a.mean() - b.mean()) / pooled) if pooled > 0 else 0.0
                out.append(
                    _json_safe(
                        {
                            "name": "welch_t",
                            "column": column,
                            "groups": [
                                str(first),
                                "rest" if second is None else str(second),
                            ],
                            "value": float(test.statistic),
                            "p_value": float(test.pvalue),
                            "effect_size": effect,
                        }
                    )
                )
            continue
        import pandas as pd

        table = pd.crosstab(frame[group_column], series)
        if table.shape[0] < 2 or table.shape[1] < 2:
            continue
        chi2, p_value, _, _ = st.chi2_contingency(table)
        n = float(table.to_numpy().sum())
        smaller = min(table.shape) - 1
        cramers_v = float(np.sqrt(chi2 / (n * smaller))) if n and smaller else 0.0
        out.append(
            _json_safe(
                {
                    "name": "chi_square",
                    "column": column,
                    "value": float(chi2),
                    "p_value": float(p_value),
                    "effect_size": cramers_v,
                }
            )
        )
    return out


def rfm(
    frame: Any,
    id_column: str,
    date_column: str,
    amount_column: str,
    *,
    as_of: Optional[str] = None,
    bins: int = 5,
) -> Any:
    """Recency, frequency and monetary value per person, scored 1..bins (BR39.2).

    Returns a pandas DataFrame of ``id_column``, ``recency`` (days since the
    last date), ``frequency``, ``monetary`` and ``r_score`` / ``f_score`` /
    ``m_score`` (a more recent, more frequent, larger customer scores higher).
    """
    import pandas as pd

    dates = pd.to_datetime(frame[date_column], errors="coerce")
    data = pd.DataFrame(
        {
            "id": frame[id_column],
            "date": dates,
            "amount": pd.to_numeric(frame[amount_column], errors="coerce"),
        }
    )
    reference = pd.Timestamp(as_of) if as_of else data["date"].max()
    grouped = data.groupby("id").agg(
        last=("date", "max"), frequency=("date", "count"), monetary=("amount", "sum")
    )
    grouped["recency"] = (reference - grouped["last"]).dt.days

    def score(values: Any, ascending: bool) -> Any:
        # Ties share a rank (the mean of theirs), so people who behave the same
        # get the same score; the score is the rank's place among ``bins`` parts.
        ranked = values.rank(method="average", ascending=ascending)
        count = max(min(bins, len(values)), 1)
        place = (ranked / len(values) * count).apply(math.ceil)
        return place.clip(lower=1, upper=count).astype(int)

    grouped["r_score"] = score(grouped["recency"], ascending=False)
    grouped["f_score"] = score(grouped["frequency"], ascending=True)
    grouped["m_score"] = score(grouped["monetary"], ascending=True)
    result = (
        grouped.drop(columns=["last"]).reset_index().rename(columns={"id": id_column})
    )
    return result


_PROFILE_NUMERIC = 8
_PROFILE_CATEGORICAL = 6
_PROFILE_VALUES = 5
_PROFILE_DISTINCT = 20
_HOUR_BINS = ((0, 6), (6, 12), (12, 18), (18, 24))


def person_profile(
    frame: Any,
    id_column: str,
    *,
    date_column: Optional[str] = None,
    as_of: Optional[str] = None,
    skip: Sequence[str] = (),
) -> Any:
    """One row per person from a history table: what they did and when.

    ``frame`` has several rows per person (orders, events, answers). Nothing
    in the table's column names is assumed: the columns are told apart by
    their types and their number of distinct values.

    Returns a pandas DataFrame of ``id_column`` and, per person:

    - ``回数（行）``: rows of the person.
    - a date column (``date_column``, else the first one): ``最後からの日数``
      (days since the person's last date up to ``as_of``, default the last
      date of the table), ``期間（日）``, ``活動日数`` (distinct days), and,
      when the dates carry a time, the share of rows per time of day
      (``0〜5時の割合`` ...) and ``土日の割合``.
    - numbers (up to 8 columns that are not keys): ``<列>の合計`` and
      ``<列>の平均``.
    - columns of 2〜20 values (up to 6 columns, 5 values each): ``<列>=<値>の割合``,
      the share of the person's rows that have the value.

    Key-like columns are left out: names that say ID or code, text columns whose
    values all differ, and the columns named in ``skip`` (the columns the user
    confirmed are no trait, ``data_reading``).
    """
    import pandas as pd

    if id_column not in frame.columns:
        raise KeyError(f"no column {id_column}")
    data = frame[frame[id_column].notna()]
    ids = data[id_column]
    by = data.groupby(id_column, sort=True)
    out = pd.DataFrame({"回数（行）": by.size()})

    left_out = {str(c) for c in skip}

    def is_key(name: str) -> bool:
        column = data[name]
        if str(name) in left_out or _KEY_NAME.search(str(name)):
            return True
        # text whose values all differ names a row, not a trait (a number whose
        # values all differ may be an amount, so it is not judged here)
        return (
            not pd.api.types.is_numeric_dtype(column)
            and not pd.api.types.is_datetime64_any_dtype(column)
            and len(data) > 1
            and column.nunique(dropna=True) == column.notna().sum()
        )

    stamp = date_column
    if stamp is None:
        stamp = next(
            (c for c in data.columns if pd.api.types.is_datetime64_any_dtype(data[c])),
            None,
        )
    if stamp is not None and stamp in data.columns:
        when = pd.to_datetime(data[stamp], errors="coerce")
        last = when.groupby(ids).max()
        first = when.groupby(ids).min()
        reference = pd.Timestamp(as_of) if as_of else when.max()
        out["最後からの日数"] = (reference - last).dt.days
        out["期間（日）"] = (last - first).dt.days
        out["活動日数"] = when.dt.normalize().groupby(ids).nunique()
        if (when.dt.hour.fillna(0) != 0).any():
            for lo, hi in _HOUR_BINS:
                inside = when.dt.hour.between(lo, hi - 1)
                out[f"{lo}〜{hi - 1}時の割合"] = inside.groupby(ids).mean()
        out["土日の割合"] = (when.dt.dayofweek >= 5).groupby(ids).mean()

    skip = {id_column, stamp}
    numeric = [
        c
        for c in data.columns
        if c not in skip
        and pd.api.types.is_numeric_dtype(data[c])
        and not pd.api.types.is_bool_dtype(data[c])
        and not is_key(c)
        and data[c].nunique(dropna=True) > 1
    ][:_PROFILE_NUMERIC]
    for c in numeric:
        values = pd.to_numeric(data[c], errors="coerce")
        out[f"{c}の合計"] = values.groupby(ids).sum()
        out[f"{c}の平均"] = values.groupby(ids).mean()

    kinds = []
    for c in data.columns:
        if c in skip or c in numeric or is_key(c):
            continue
        if pd.api.types.is_datetime64_any_dtype(data[c]):
            continue
        count = data[c].nunique(dropna=True)
        if 2 <= count <= _PROFILE_DISTINCT:
            kinds.append((count, c))
    for _, c in sorted(kinds, key=lambda item: item[0])[:_PROFILE_CATEGORICAL]:
        common = data[c].value_counts().index[:_PROFILE_VALUES]
        for value in common:
            out[f"{c}={value}の割合"] = (data[c] == value).groupby(ids).mean()

    result = out.round(4).reset_index()
    return result


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

FUNCTIONS: Dict[str, Callable[[Dict[str, Any], str], Any]] = {
    "download": download,
    "prepare": prepare,
    "profile_dataset": profile_dataset,
    "run_query": run_query,
    "cluster_segments": cluster_segments,
    "profile_segments": profile_segments,
    "prepare_document": prepare_document,
    "document_chunk": document_chunk,
    "verify_quotes": verify_quotes,
    # U6
    "run_ai_code": run_ai_code,
    "finalize_ai_result": finalize_ai_result,
    "verify_hashes": verify_hashes,
    "key_overlap": key_overlap,
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
