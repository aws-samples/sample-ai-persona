"""analyze_dataset tool closure factory + LLM 入力契約（TypedDict）。

LLM には server 発行の不透明な別名（``dataset_id``）だけを見せ、SQL・S3 パス・
AWS 認証情報・ローカルパスは引数にも戻り値にも出さない。入力は ``dict[str, Any]``
ではなく ``TypedDict`` + ``Literal`` で構造化し、operator/func/direction を schema に
明示する（``dict`` 型だと Strands schema 上ただの object になり operator を表現できない）。
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict, Union

try:
    from typing import NotRequired
except ImportError:  # pragma: no cover - Python < 3.11 fallback
    from typing_extensions import NotRequired  # type: ignore[assignment]

try:
    from strands import tool
except ImportError:  # pragma: no cover - SDK 未導入環境

    def tool(func: Callable) -> Callable:  # type: ignore[no-redef]
        return func


from .query_backend import DatasetAccessError, DatasetQueryBackend, DatasetQueryTimeout
from .sql_builder import SqlBuildError, build_analyze_query

logger = logging.getLogger(__name__)

_VIEW_NAME = "dataset"

Scalar = Union[str, int, float, bool]


class Filter(TypedDict):
    """1 個のフィルタ条件。``in`` は value=list、``contains`` は value=str。"""

    column: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"]
    value: Union[Scalar, List[Scalar]]


class Metric(TypedDict):
    """集計指標。``count`` は column 省略可、それ以外は column 必須。

    ``count_distinct`` は指定列のユニーク値数、``median`` は中央値（外れ値に強い）。
    """

    func: Literal["count", "count_distinct", "sum", "avg", "min", "max", "median"]
    column: NotRequired[str]


class GroupBy(TypedDict):
    """グループ化列。``date_trunc`` を付けると日付/時刻列を期間バケットへ丸める。

    例: ``{"column": "purchase_date", "date_trunc": "month"}`` は月単位で集計する
    （指定しないと日付が生の値で散らばり、月次・週次の傾向が取れない）。
    """

    column: str
    date_trunc: NotRequired[Literal["day", "week", "month", "quarter", "year"]]


class OrderBy(TypedDict):
    """並び順。``column`` と ``metric_index`` は排他（どちらか一方のみ）。"""

    column: NotRequired[str]
    metric_index: NotRequired[int]
    direction: Literal["asc", "desc"]


@dataclass(frozen=True)
class ResolvedDataset:
    """tool クロージャが alias でキーに保持する immutable な dataset 記述子。

    LLM へは ``alias``（不透明 dataset_id）のみ露出する。``backend_path`` /
    ``forced_filter`` は closure 内部にのみ存在し露出しない。プロンプト表示用の
    メタデータは Manager が別途 descriptors として構築する（ここには持たない）。
    """

    alias: str
    backend_path: str
    columns: List[str]
    forced_filter: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class ToolLimits:
    default_limit: int = 100
    max_limit: int = 200
    max_result_chars: int = 8000
    timeout_seconds: float = 30.0


def _format_result(
    columns: List[str],
    rows: List[List[Any]],
    label_map: Dict[str, str],
    max_chars: int,
) -> str:
    """結果を LLM 向けの表形式テキストへ整形する（内部集計別名を表示ラベルへ）。"""
    display_cols = [label_map.get(c, c) for c in columns]
    header = " | ".join(display_cols)
    lines = [f"{len(rows)} 件の結果:", header]
    for row in rows:
        lines.append(" | ".join("" if v is None else str(v) for v in row))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(結果を省略しました)"
    return text


def create_analyze_dataset_tool(
    resolved: List[ResolvedDataset],
    backend: DatasetQueryBackend,
    limits: Optional[ToolLimits] = None,
) -> Callable:
    """複数 dataset を alias で保持する単一 ``analyze_dataset`` ツールを生成する。

    binding 経路・source 経路で共通。全 descriptor を alias でキーに持つ immutable な
    map を closure が保持するため、並行する別クロージャと状態を共有しない。
    """
    limits = limits or ToolLimits()
    # immutable な descriptor map（alias -> ResolvedDataset）。
    dataset_map: Dict[str, ResolvedDataset] = {d.alias: d for d in resolved}

    @tool
    def analyze_dataset(
        dataset_id: str,
        select_columns: Optional[List[str]] = None,
        filters: Optional[List[Filter]] = None,
        group_by: Optional[List[Union[str, GroupBy]]] = None,
        metrics: Optional[List[Metric]] = None,
        order_by: Optional[List[OrderBy]] = None,
        limit: Optional[int] = None,
    ) -> str:
        """データセットを構造化クエリで分析します（SQL は書けません）。

        利用可能な dataset_id と各データセットの列はシステムプロンプトに記載されています。

        2 つのモードがあります:
        - 生データ参照: metrics と group_by を指定しない。select_columns で列を絞れます。
          具体的な商品名・日付・金額の確認や初期サンプルに使います。
        - 集計: metrics を指定する。group_by は任意（無ければ全体集計で 1 行）。

        Args:
            dataset_id: 分析対象データセットの識別子（システムプロンプト記載の別名）。
            select_columns: 取得する列名のリスト（生データ参照モードのみ）。
            filters: 絞り込み条件のリスト。各要素は column / operator / value。
                operator は eq, ne, gt, gte, lt, lte, in, contains のいずれか。
                in は value にリスト、contains は文字列部分一致。
            group_by: 集計のグループ化列（集計モードのみ）。各要素は列名の文字列、
                または {"column": 列名, "date_trunc": 単位}（単位は day/week/month/
                quarter/year）。後者は日付/時刻列を期間バケットへ丸めて集計します
                （月次・週次の傾向を出すときに使用）。
            metrics: 集計指標のリスト。func は count, count_distinct, sum, avg,
                min, max, median。count は column 省略で全件数、それ以外は column 必須。
                count_distinct はユニーク値数、median は中央値（外れ値に強い）。
            order_by: 並び順。column（列名）か metric_index（metrics の 0 始まり位置）の
                いずれか一方と direction（asc / desc）を指定します。
            limit: 取得する最大行数。

        Returns:
            分析結果の表形式テキスト。
        """
        descriptor = dataset_map.get(dataset_id)
        if descriptor is None:
            available = ", ".join(sorted(dataset_map)) or "(なし)"
            return (
                f"データセット '{dataset_id}' は利用できません。"
                f"利用可能な dataset_id: {available}"
            )

        try:
            sql, params, label_map = build_analyze_query(
                view_name=_VIEW_NAME,
                allowed_columns=descriptor.columns,
                select_columns=select_columns,
                filters=[dict(f) for f in filters] if filters else None,
                group_by=(
                    [g if isinstance(g, str) else dict(g) for g in group_by]
                    if group_by
                    else None
                ),
                metrics=[dict(m) for m in metrics] if metrics else None,
                order_by=[dict(o) for o in order_by] if order_by else None,
                limit=limit,
                forced_filter=descriptor.forced_filter,
                default_limit=limits.default_limit,
                max_limit=limits.max_limit,
            )
        except SqlBuildError as e:
            return f"クエリの指定が不正です: {e}"

        import time

        started = time.monotonic()
        try:
            columns, rows = backend.execute(
                descriptor.backend_path,
                sql,
                params,
                timeout=limits.timeout_seconds,
            )
        except DatasetQueryTimeout:
            logger.warning("analyze_dataset timeout: dataset=%s", dataset_id)
            return "データ取得がタイムアウトしました。条件を絞って再度お試しください。"
        except DatasetAccessError:
            logger.error("analyze_dataset access denied: dataset=%s", dataset_id)
            return "データソースにアクセスできませんでした。"
        except Exception:  # noqa: BLE001 - LLM 向けに安全な文字列へ落とす
            logger.error(
                "analyze_dataset query failed: dataset=%s", dataset_id, exc_info=True
            )
            return "データの分析中にエラーが発生しました。条件を確認して再度お試しください。"

        elapsed = time.monotonic() - started
        logger.info(
            "analyze_dataset ok: dataset=%s rows=%d elapsed=%.2fs",
            dataset_id,
            len(rows),
            elapsed,
        )
        if not rows:
            return "条件に一致するデータはありませんでした。"
        return _format_result(columns, rows, label_map, limits.max_result_chars)

    return analyze_dataset
