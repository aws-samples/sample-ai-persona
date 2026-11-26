"""Structured-argument → parameterized SQL builder (pure functions, no I/O).

Converts structured arguments from the LLM (filters / group_by / metrics / order_by / limit)
into parameterized SQL for DuckDB. SQL injection is prevented three ways:
(1) an exact-match column-name allowlist, (2) double-quoting + escaping once past it,
(3) positional ``$N`` parameters for values. Forced filters (binding_keys) are always ANDed in
separately from LLM-supplied filters and cannot be lifted or overridden by LLM arguments.
"""

from typing import Any, Dict, List, Optional, Tuple

# Operator allowlist (must match the Literal in the LLM contract).
_COMPARISON_OPERATORS: Dict[str, str] = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
# Aggregates that require a column (count is separate and may omit it). median is DuckDB's median().
_COLUMN_AGGREGATE_FUNCS = frozenset({"sum", "avg", "min", "max", "median"})
# Truncation units for bucketing date/time group_by columns (must match the Literal in the LLM contract).
_DATE_TRUNC_UNITS = frozenset({"day", "week", "month", "quarter", "year"})


class SqlBuildError(ValueError):
    """Raised when the structured arguments are invalid and SQL cannot be built.

    The tool closure catches this and converts it to an LLM-safe string.
    """


def _quote_ident(name: str) -> str:
    """Quote an identifier with double quotes, escaping any inner ``"`` as ``""``."""
    return '"' + name.replace('"', '""') + '"'


def build_analyze_query(
    *,
    view_name: str,
    allowed_columns: List[str],
    select_columns: Optional[List[str]] = None,
    filters: Optional[List[Dict[str, Any]]] = None,
    group_by: Optional[List[Any]] = None,
    metrics: Optional[List[Dict[str, Any]]] = None,
    order_by: Optional[List[Dict[str, Any]]] = None,
    limit: Optional[int] = None,
    forced_filter: Optional[Dict[str, Any]] = None,
    default_limit: int = 100,
    max_limit: int = 200,
) -> Tuple[str, List[Any], Dict[str, str]]:
    """Return (SQL, params, label_map) from the structured arguments.

    label_map maps internal alias -> display label. Aggregates map ``m0`` -> ``sum(amount)`` etc.,
    date-bucket columns ``g0`` -> ``month(purchase_date)`` etc. Non-aggregate, non-bucket columns are
    left out and callers use the column names the backend returns as is.

    Each ``group_by`` item is a column-name string or ``{"column": str, "date_trunc": unit}``
    (unit is day/week/month/quarter/year). The latter rounds with ``date_trunc(unit, col)``
    into period buckets before grouping (avoids scattering across raw dates).
    """
    allowed = set(allowed_columns)

    def require_col(col: Any) -> str:
        if not isinstance(col, str) or col not in allowed:
            raise SqlBuildError(f"unknown column: {col!r}")
        return col

    params: List[Any] = []

    def add_param(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    is_aggregate = bool(metrics)

    # --- Mode consistency checks ---
    if group_by and not metrics:
        raise SqlBuildError("group_by requires metrics (implicit count is not allowed)")
    if is_aggregate and select_columns:
        raise SqlBuildError("select_columns cannot be combined with metrics")

    # --- Normalize group_by into (column name, truncation unit or None) ---
    # Plain columns stay a bare quoted ident as before (matches existing SQL). Date buckets
    # bind the date_trunc expression to alias g{i} and reference it consistently in SELECT/GROUP BY/ORDER BY.
    group_specs: List[Tuple[str, Optional[str]]] = []
    for g in group_by or []:
        if isinstance(g, str):
            group_specs.append((require_col(g), None))
        elif isinstance(g, dict):
            gcol = require_col(g.get("column"))
            trunc = g.get("date_trunc")
            # A non-string (e.g. list) makes `in` raise TypeError, which is not normalized into
            # SqlBuildError, so put a type guard first, as in require_col.
            if trunc is not None and (
                not isinstance(trunc, str) or trunc not in _DATE_TRUNC_UNITS
            ):
                raise SqlBuildError(f"unknown date_trunc unit: {trunc!r}")
            group_specs.append((gcol, trunc))
        else:
            raise SqlBuildError(f"invalid group_by entry: {g!r}")

    # --- WHERE (LLM-supplied filters) ---
    where: List[str] = []
    for f in filters or []:
        col = require_col(f.get("column"))
        op = f.get("operator")
        value = f.get("value")
        quoted = _quote_ident(col)
        if op == "in":
            if not isinstance(value, list) or not value:
                raise SqlBuildError("operator 'in' requires a non-empty list value")
            placeholders = ", ".join(add_param(v) for v in value)
            where.append(f"{quoted} IN ({placeholders})")
        elif op == "contains":
            if not isinstance(value, str):
                raise SqlBuildError("operator 'contains' requires a string value")
            # Case-insensitive substring match (ILIKE). If 'coffee' did not hit 'Coffee',
            # the persona would answer against the data.
            # Escape the ILIKE metacharacters (% _ \) to keep it a substring match. Values are
            # already parameterized (no injection), but unescaped % / _ act as wildcards
            # and match unintended rows. ESCAPE is a constant.
            escaped_value = (
                value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            where.append(
                f"{quoted} ILIKE {add_param('%' + escaped_value + '%')} ESCAPE '\\'"
            )
        elif op in _COMPARISON_OPERATORS:
            if isinstance(value, list):
                raise SqlBuildError(f"operator {op!r} does not accept a list value")
            where.append(f"{quoted} {_COMPARISON_OPERATORS[op]} {add_param(value)}")
        else:
            raise SqlBuildError(f"unknown operator: {op!r}")

    # --- Forced filters (binding_keys): always ANDed, separately from LLM arguments ---
    for key, value in (forced_filter or {}).items():
        col = require_col(key)
        where.append(f"{_quote_ident(col)} = {add_param(value)}")

    # --- SELECT ---
    label_map: Dict[str, str] = {}
    # Expand group columns into the expressions used in SELECT/GROUP BY/ORDER BY.
    group_by_exprs: List[str] = []  # GROUP BY clause
    group_order_expr: Dict[
        str, str
    ] = {}  # Column name -> expression referenced in ORDER BY
    if is_aggregate:
        select_parts: List[str] = []
        for i, (gcol, trunc) in enumerate(group_specs):
            quoted = _quote_ident(gcol)
            if trunc is None:
                select_parts.append(quoted)
                group_by_exprs.append(quoted)
                group_order_expr[gcol] = quoted
            else:
                # The date_trunc unit is an allowlisted constant (a position where $N is not allowed).
                expr = f"date_trunc('{trunc}', {quoted})"
                alias = f"g{i}"
                select_parts.append(f"{expr} AS {alias}")
                group_by_exprs.append(expr)
                group_order_expr[gcol] = alias
                label_map[alias] = f"{trunc}({gcol})"
        for i, metric in enumerate(metrics or []):
            func = metric.get("func")
            mcol = metric.get("column")
            alias = f"m{i}"
            if func == "count":
                if mcol:
                    expr = f"count({_quote_ident(require_col(mcol))})"
                    label = f"count({mcol})"
                else:
                    expr = "count(*)"
                    label = "count"
            elif func == "count_distinct":
                if not mcol:
                    raise SqlBuildError("aggregate 'count_distinct' requires a column")
                expr = f"count(DISTINCT {_quote_ident(require_col(mcol))})"
                label = f"count(distinct {mcol})"
            elif func in _COLUMN_AGGREGATE_FUNCS:
                if not mcol:
                    raise SqlBuildError(f"aggregate {func!r} requires a column")
                expr = f"{func}({_quote_ident(require_col(mcol))})"
                label = f"{func}({mcol})"
            else:
                raise SqlBuildError(f"unknown aggregate func: {func!r}")
            select_parts.append(f"{expr} AS {alias}")
            label_map[alias] = label
        select_sql = ", ".join(select_parts)
    elif select_columns:
        select_sql = ", ".join(_quote_ident(require_col(c)) for c in select_columns)
    else:
        select_sql = "*"

    # --- ORDER BY ---
    order_parts: List[str] = []
    for o in order_by or []:
        order_col: Any = o.get("column")
        metric_index: Any = o.get("metric_index")
        direction = o.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise SqlBuildError(f"unknown order direction: {direction!r}")
        dir_sql = "DESC" if direction == "desc" else "ASC"
        has_col = order_col is not None
        has_index = metric_index is not None
        if has_col == has_index:
            raise SqlBuildError(
                "order_by requires exactly one of 'column' or 'metric_index'"
            )
        if has_col:
            if is_aggregate:
                # In aggregate mode you can only sort by group_by columns. Bucket columns are
                # referenced via alias g{i} (the raw column name would not match the GROUP BY expression).
                if order_col not in group_order_expr:
                    raise SqlBuildError("order_by column must be one of group_by")
                order_parts.append(f"{group_order_expr[order_col]} {dir_sql}")
            else:
                order_parts.append(f"{_quote_ident(require_col(order_col))} {dir_sql}")
        else:
            if not is_aggregate:
                raise SqlBuildError("metric_index ordering requires metrics")
            if (
                not isinstance(metric_index, int)
                or isinstance(metric_index, bool)
                or metric_index < 0
                or metric_index >= len(metrics or [])
            ):
                raise SqlBuildError(f"metric_index out of range: {metric_index!r}")
            order_parts.append(f"m{metric_index} {dir_sql}")

    # --- LIMIT (clamped to the server maximum) ---
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        effective_limit = min(limit, max_limit)
    else:
        effective_limit = min(default_limit, max_limit)

    sql = f"SELECT {select_sql} FROM {view_name}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if is_aggregate and group_by_exprs:
        sql += " GROUP BY " + ", ".join(group_by_exprs)
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    sql += f" LIMIT {effective_limit}"

    return sql, params, label_map
