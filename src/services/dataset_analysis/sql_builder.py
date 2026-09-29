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
# column 必須の集計（count は別枠で column 省略可）。median は DuckDB の median()。
_COLUMN_AGGREGATE_FUNCS = frozenset({"sum", "avg", "min", "max", "median"})
# 日付/時刻列の group_by バケット化に使う truncation 単位（LLM 契約の Literal と一致）。
_DATE_TRUNC_UNITS = frozenset({"day", "week", "month", "quarter", "year"})


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
    group_by: Optional[List[Any]] = None,
    metrics: Optional[List[Dict[str, Any]]] = None,
    order_by: Optional[List[Dict[str, Any]]] = None,
    limit: Optional[int] = None,
    forced_filter: Optional[Dict[str, Any]] = None,
    default_limit: int = 100,
    max_limit: int = 200,
) -> Tuple[str, List[Any], Dict[str, str]]:
    """構造化引数から (SQL, params, label_map) を返す。

    label_map は内部別名 → 表示ラベルの写像。集計は ``m0`` → ``sum(amount)`` 等、
    日付バケット列は ``g0`` → ``month(purchase_date)`` 等。非集計かつ非バケットの列は
    写像に含めず、呼び出し側は backend が返す列名をそのまま使う。

    ``group_by`` の各要素は列名文字列、または ``{"column": str, "date_trunc": unit}``
    （unit は day/week/month/quarter/year）。後者は ``date_trunc(unit, col)`` で
    期間バケットに丸めてからグループ化する（生の日付単位に散らばるのを防ぐ）。
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

    # --- group_by を (列名, truncation単位 or None) へ正規化 ---
    # プレーン列は従来どおり素の quoted ident（既存 SQL と一致）。日付バケットは
    # date_trunc 式を別名 g{i} に束ねて SELECT/GROUP BY/ORDER BY で一貫参照する。
    group_specs: List[Tuple[str, Optional[str]]] = []
    for g in group_by or []:
        if isinstance(g, str):
            group_specs.append((require_col(g), None))
        elif isinstance(g, dict):
            gcol = require_col(g.get("column"))
            trunc = g.get("date_trunc")
            # 非文字列（list 等）だと `in` が TypeError を送出し SqlBuildError に
            # 正規化されないため、require_col と同様に型ガードを先に置く。
            if trunc is not None and (
                not isinstance(trunc, str) or trunc not in _DATE_TRUNC_UNITS
            ):
                raise SqlBuildError(f"unknown date_trunc unit: {trunc!r}")
            group_specs.append((gcol, trunc))
        else:
            raise SqlBuildError(f"invalid group_by entry: {g!r}")

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
            # 大文字小文字を無視した部分一致（ILIKE）。'coffee' が 'Coffee' に
            # ヒットしないと、ペルソナがデータに反する回答をしてしまうため。
            # ILIKE メタ文字（% _ \）をエスケープして部分一致に限定する。値は
            # パラメータ化済み（注入不可）だが、未エスケープだと % / _ が
            # ワイルドカードとして働き意図しない行にマッチする。ESCAPE は定数。
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

    # --- 強制フィルタ（binding_keys）: LLM 引数と別枠で必ず AND 付与 ---
    for key, value in (forced_filter or {}).items():
        col = require_col(key)
        where.append(f"{_quote_ident(col)} = {add_param(value)}")

    # --- SELECT ---
    label_map: Dict[str, str] = {}
    # group 列を SELECT/GROUP BY/ORDER BY で使う式へ展開する。
    group_by_exprs: List[str] = []  # GROUP BY 句
    group_order_expr: Dict[str, str] = {}  # 列名 -> ORDER BY で参照する式
    if is_aggregate:
        select_parts: List[str] = []
        for i, (gcol, trunc) in enumerate(group_specs):
            quoted = _quote_ident(gcol)
            if trunc is None:
                select_parts.append(quoted)
                group_by_exprs.append(quoted)
                group_order_expr[gcol] = quoted
            else:
                # date_trunc の単位は allowlist 済みの定数（$N 不可の位置）。
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
                # 集計モードでは group_by 列のみでソートできる。バケット列は
                # 別名 g{i} 経由で参照する（生列名では GROUP BY 式と不一致になる）。
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

    # --- LIMIT（server 上限で clamp）---
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
