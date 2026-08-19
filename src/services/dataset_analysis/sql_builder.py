"""Structured-argument → parameterized SQL builder (pure functions, no I/O).

LLM が与える構造化引数（filters / group_by / metrics / order_by / limit）を、
DuckDB 用の parameterized SQL へ変換する。SQL インジェクションは
(1) 列名の完全一致 allowlist、(2) 通過後の double-quote + escape、
(3) 値の位置パラメータ ``$N`` の三重で防ぐ。強制フィルタ（binding_keys）は
LLM 由来フィルタとは別枠で必ず AND 付与し、LLM 引数では解除・上書きできない。
"""

from typing import Any, Dict, List, Optional, Tuple

# 演算子 allowlist（LLM 契約の Literal と一致させること）。
_COMPARISON_OPERATORS: Dict[str, str] = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
_COLUMN_AGGREGATE_FUNCS = frozenset({"sum", "avg", "min", "max"})


class SqlBuildError(ValueError):
    """構造化引数が不正で SQL を組み立てられない場合に送出する。

    ツールクロージャがこれを捕捉し、LLM 向けの安全な文字列へ変換する。
    """


def _quote_ident(name: str) -> str:
    """識別子を double-quote で囲み、内部の ``"`` を ``""`` にエスケープする。"""
    return '"' + name.replace('"', '""') + '"'


def build_analyze_query(
    *,
    view_name: str,
    allowed_columns: List[str],
    select_columns: Optional[List[str]] = None,
    filters: Optional[List[Dict[str, Any]]] = None,
    group_by: Optional[List[str]] = None,
    metrics: Optional[List[Dict[str, Any]]] = None,
    order_by: Optional[List[Dict[str, Any]]] = None,
    limit: Optional[int] = None,
    forced_filter: Optional[Dict[str, Any]] = None,
    default_limit: int = 100,
    max_limit: int = 200,
) -> Tuple[str, List[Any], Dict[str, str]]:
    """構造化引数から (SQL, params, label_map) を返す。

    label_map は内部集計別名（``m0`` 等）→ 表示ラベル（``sum(amount)`` 等）の写像。
    非集計列は写像に含めず、呼び出し側は backend が返す列名をそのまま使う。
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

    # --- モード整合性チェック ---
    if group_by and not metrics:
        raise SqlBuildError("group_by requires metrics (implicit count is not allowed)")
    if is_aggregate and select_columns:
        raise SqlBuildError("select_columns cannot be combined with metrics")

    # --- WHERE（LLM 由来 filter）---
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
            where.append(f"{quoted} LIKE {add_param('%' + value + '%')}")
        elif op in _COMPARISON_OPERATORS:
            if isinstance(value, list):
                raise SqlBuildError(f"operator {op!r} does not accept a list value")
            where.append(f"{quoted} {_COMPARISON_OPERATORS[op]} {add_param(value)}")
        else:
            raise SqlBuildError(f"unknown operator: {op!r}")

    # --- 強制フィルタ（binding_keys）: LLM 引数と別枠で必ず AND 付与 ---
    for key, value in (forced_filter or {}).items():
        col = require_col(key)
        where.append(f"{_quote_ident(col)} = {add_param(value)}")

    # --- SELECT ---
    label_map: Dict[str, str] = {}
    if is_aggregate:
        select_parts: List[str] = [_quote_ident(require_col(g)) for g in group_by or []]
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
                # 集計モードでは group_by 列のみでソートできる。
                if order_col not in (group_by or []):
                    raise SqlBuildError("order_by column must be one of group_by")
                order_parts.append(f"{_quote_ident(order_col)} {dir_sql}")
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

    # --- LIMIT（server 上限で clamp）---
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        effective_limit = min(limit, max_limit)
    else:
        effective_limit = min(default_limit, max_limit)

    sql = f"SELECT {select_sql} FROM {view_name}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if is_aggregate and group_by:
        sql += " GROUP BY " + ", ".join(_quote_ident(g) for g in group_by)
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    sql += f" LIMIT {effective_limit}"

    return sql, params, label_map
