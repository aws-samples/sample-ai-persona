"""What the analysis record shows of each step: counted aggregates, never rows.

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package. It is a pure decision: no model, no Service.

The record is copied to a saved persona and may be shared with departments
that never had the source file, and it stays after the file is deleted. So a
step's result keeps only numbers the library counted and the names of columns
and groups:

- groups (``cluster_segments``, ``profile_segments`` and an accepted
  ``run_code`` result): each group's size, share and its biggest differences
  from the whole. A group of fewer than :data:`MIN_SHOWN_SIZE` is shown as
  hidden, its name included (a layer by a column of names carries the person
  in its key).
- a refused ``run_code`` result: the fixed reason and the counts the check
  sent. What the code printed and its error text are never kept: they may
  hold rows, and their numbers were not counted again.
- ``profile_dataset``: rows, columns, their kinds and the missing values; not
  the commonest values, the minimum or the maximum (one person's value).
- ``analyze_dataset``: how many rows it returned, not the rows.
"""

from __future__ import annotations

from typing import Any, Collection, Mapping, Optional, Sequence

from ...models.analysis import EmittedResult, PrepareReport, Segment
from .segment_features import ranked_features

#: A group smaller than this is hidden (its numbers could point at a person).
MIN_SHOWN_SIZE = 10
#: Groups kept of one step, and features kept of one group.
MAX_SHOWN_GROUPS = 20
MAX_SHOWN_FEATURES = 3
#: Columns with missing values kept of one table, the most missing first.
MAX_SHOWN_MISSING = 10
_SHARE_DIGITS = 4
_FEATURE_KEYS = ("name", "kind", "value", "baseline")
_FILTERED_KEY = "filtered"


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _kind_of(type_name: str) -> str:
    """The kind of a DuckDB column type, as the record names it."""
    name = type_name.upper()
    if name.startswith(("DATE", "TIMESTAMP", "TIME")):
        return "date"
    if name.startswith(
        (
            "TINYINT",
            "SMALLINT",
            "INTEGER",
            "BIGINT",
            "HUGEINT",
            "UTINYINT",
            "USMALLINT",
            "UINTEGER",
            "UBIGINT",
            "FLOAT",
            "DOUBLE",
            "REAL",
            "DECIMAL",
        )
    ):
        return "number"
    if name.startswith("BOOLEAN"):
        return "boolean"
    if name.startswith("VARCHAR"):
        return "text"
    return "other"


def _columns_summary(rows: int, columns: Any) -> Optional[dict[str, Any]]:
    """Kinds and missing values of profiled columns (no value of a row)."""
    if not isinstance(columns, list):
        return None
    kinds: dict[str, int] = {}
    missing: list[dict[str, Any]] = []
    for column in columns:
        if not isinstance(column, Mapping):
            continue
        kind = _kind_of(str(column.get("type", "")))
        kinds[kind] = kinds.get(kind, 0) + 1
        filled = _count(column.get("non_null"))
        if filled is not None and filled < rows:
            missing.append(
                {"name": str(column.get("name", "")), "count": rows - filled}
            )
    missing.sort(key=lambda m: (-m["count"], m["name"]))
    return {
        "columns": sum(kinds.values()),
        "types": kinds,
        "missing": missing[:MAX_SHOWN_MISSING],
    }


def table_summary(
    name: str, report: PrepareReport, profile: Mapping[str, Any]
) -> dict[str, Any]:
    """The reading of one table: rows, columns and missing values."""
    columns = _columns_summary(int(report.rows_read), profile.get("columns")) or {
        "columns": len(report.columns),
        "types": {},
        "missing": [],
    }
    return {
        "name": name,
        "rows_read": int(report.rows_read),
        "rows_excluded": int(report.rows_excluded),
        **columns,
    }


def _features(items: Any, skip: Collection[str]) -> list[dict[str, Any]]:
    return [
        {k: f[k] for k in _FEATURE_KEYS if k in f}
        for f in ranked_features(items, skip)[:MAX_SHOWN_FEATURES]
    ]


def _group(
    label: str,
    size: Any,
    population: int,
    features: Any,
    skip: Collection[str],
) -> dict[str, Any]:
    counted = _count(size)
    if counted is None or counted < MIN_SHOWN_SIZE:
        return {"hidden": True}
    share = round(counted / population, _SHARE_DIGITS) if population else None
    return {
        "label": label,
        "size": counted,
        "share": share,
        "features": _features(features, skip),
    }


def _label(segments: Sequence[Segment], matches: Any, fallback: str) -> str:
    """The name the analysis gave a group it adopted, else ``fallback``."""
    for segment in segments:
        if matches(segment):
            return segment.label
    return fallback


def _cluster_groups(
    call_id: str, output: Mapping[str, Any], segments: Sequence[Segment], skip: Any
) -> Optional[list[dict[str, Any]]]:
    clusters = output.get("clusters")
    population = _count(output.get("population"))
    if not isinstance(clusters, list) or population is None:
        return None
    groups: list[dict[str, Any]] = []
    for cluster in clusters[:MAX_SHOWN_GROUPS]:
        if not isinstance(cluster, Mapping):
            continue
        index = cluster.get("index")

        def adopted(s: Segment, index: Any = index) -> bool:
            spec = s.definition.get("spec")
            return (
                s.tool_call_id == call_id
                and isinstance(spec, Mapping)
                and str(spec.get("cluster_index")) == str(index)
            )

        number = index + 1 if isinstance(index, int) else len(groups) + 1
        label = _label(segments, adopted, f"グループ {number}")
        groups.append(
            _group(
                label, cluster.get("size"), population, cluster.get("features"), skip
            )
        )
    return groups


def _layer_groups(
    call_id: str, output: Mapping[str, Any], segments: Sequence[Segment], skip: Any
) -> Optional[list[dict[str, Any]]]:
    layers = output.get("layers")
    population = _count(output.get("population"))
    if not isinstance(layers, list) or population is None:
        return None
    groups: list[dict[str, Any]] = []
    for layer in layers[:MAX_SHOWN_GROUPS]:
        if not isinstance(layer, Mapping):
            continue
        key = str(layer.get("key"))

        def adopted(s: Segment, key: str = key) -> bool:
            return s.profile_tool_call_id == call_id and s.profile_key == key

        fallback = "絞り込んだ人" if key == _FILTERED_KEY else key
        label = _label(segments, adopted, fallback)
        groups.append(
            _group(label, layer.get("size"), population, layer.get("features"), skip)
        )
    return groups


def _emitted_groups(
    call_id: str, emitted: EmittedResult, segments: Sequence[Segment], skip: Any
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for entry in emitted.segments[:MAX_SHOWN_GROUPS]:
        key = str(entry.get("segment_key"))

        def adopted(s: Segment, key: str = key) -> bool:
            return s.tool_call_id == call_id and s.segment_key == key

        label = _label(segments, adopted, key)
        groups.append(
            _group(
                label,
                entry.get("size"),
                int(emitted.population),
                entry.get("features"),
                skip,
            )
        )
    return groups


def _failure(call: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"failure": str(call.error_code or "TOOL_FAILED")}
    counts = getattr(call, "check_counts", None)
    if isinstance(counts, Mapping):
        kept = {
            k: v
            for k, v in counts.items()
            if _count(v) is not None or (k == "side" and v in ("population", "result"))
        }
        if kept:
            result["counts"] = kept
    return result


def call_result(
    call: Any,
    *,
    output: Any,
    emitted: Optional[EmittedResult],
    segments: Sequence[Segment],
    history_tables: Mapping[str, Optional[str]],
    skip: Collection[str] = (),
) -> Optional[dict[str, Any]]:
    """What the record shows of one tool call, or ``None`` for nothing.

    Args:
        call: The tool call (``ToolCall``: ``tool``, ``ok``, ``error_code``,
            ``table``, ``check_counts``).
        output: Its structured output (the Service's ``output_of``).
        emitted: The recounted result of an accepted ``run_code`` call.
        segments: The segments the analysis kept (their names label groups).
        history_tables: The tables with several rows per person (alias ->
            the person column the user confirmed); a fixed tool's counts on
            them are of rows.
        skip: Columns confirmed as no trait; no feature is shown of them.
    """
    tool = str(call.tool)
    call_id = str(call.tool_call_id)
    if tool == "run_code":
        if emitted is not None:
            result: dict[str, Any] = {
                "population": int(emitted.population),
                "unit": "people",
                "groups": _emitted_groups(call_id, emitted, segments, skip),
            }
            if emitted.from_history:
                alias = emitted.population_source.removeprefix("people:")
                result["history"] = {
                    "id_column": history_tables.get(alias) or "",
                    "source_rows": int(emitted.source_rows or 0),
                    "rows_without_id": int(emitted.rows_without_id or 0),
                }
            return result
        if not call.ok and call.error_code != "NO_RESULT":
            return _failure(call)
        return None
    if not call.ok or not isinstance(output, Mapping):
        return None
    unit = "rows" if call.table in history_tables else "people"
    if tool in ("cluster_segments", "profile_segments"):
        made = (
            _cluster_groups(call_id, output, segments, skip)
            if tool == "cluster_segments"
            else _layer_groups(call_id, output, segments, skip)
        )
        if made is None:
            return None
        return {
            "population": int(output["population"]),
            "unit": unit,
            "groups": made,
        }
    if tool == "analyze_dataset":
        rows = output.get("rows")
        return {"rows_returned": len(rows)} if isinstance(rows, list) else None
    if tool == "profile_dataset":
        rows = _count(output.get("rows"))
        columns = _columns_summary(rows or 0, output.get("columns"))
        if rows is None or columns is None:
            return None
        return {"rows": rows, **columns}
    return None
