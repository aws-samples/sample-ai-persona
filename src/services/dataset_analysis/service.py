"""DatasetAnalysisService — analyze_dataset ツール組み立ての Service エントリ。

入口の記述子を用途別に 2 つに分ける（binding 経路 / persona 生成 source 経路）が、
内部では共通の :class:`ResolvedDataset` へ正規化してから tool/SQL を組む。
重複 binding の除外（dedup）は Manager の責務であり、この Service では行わない。
"""

import logging
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TYPE_CHECKING,
    TypedDict,
)

from .dataset_tools import ResolvedDataset, ToolLimits, create_analyze_dataset_tool
from .query_backend import DatasetQueryBackend, DuckDBQueryBackend

if TYPE_CHECKING:
    from ...models.dataset import Dataset

logger = logging.getLogger(__name__)


class BindingDescriptor(TypedDict):
    """binding 経路の入口記述子（discussion / interview）。"""

    dataset_id: str
    binding_keys: Dict[str, str]


class SourceDescriptor(TypedDict):
    """persona 生成 source 経路の入口記述子。forced_filter は持たない。"""

    alias: str
    path: str
    columns: List[str]


class DatasetAnalysisService:
    """analyze_dataset ツールと、それに対応するプロンプト用記述子を生成する。"""

    def __init__(
        self,
        bucket_name: str = "",
        region_name: str = "us-east-1",
        backend: Optional[DatasetQueryBackend] = None,
        limits: Optional[ToolLimits] = None,
    ) -> None:
        self.backend: DatasetQueryBackend = backend or DuckDBQueryBackend(
            bucket_name=bucket_name, region_name=region_name
        )
        self.limits = limits or ToolLimits()

    # ------------------------------------------------------------------
    # binding 経路（discussion / interview）
    # ------------------------------------------------------------------

    def build_binding_tools(
        self,
        accepted_bindings: Sequence[Mapping[str, Any]],
        datasets: Sequence["Dataset"],
    ) -> Tuple[List[Callable], List[Dict[str, Any]]]:
        """重複除外済みの binding から単一 analyze_dataset ツールと記述子を作る。

        Returns:
            (tools, accepted_descriptors)
            tools は単一ツール（要素 0 or 1）。accepted_descriptors は prompt 用の
            表示メタデータ（backend_path / forced_filter 値は含めない）。
        """
        dataset_map = {d.id: d for d in datasets}
        resolved: List[ResolvedDataset] = []
        descriptors: List[Dict[str, Any]] = []

        for index, binding in enumerate(accepted_bindings, start=1):
            dataset = dataset_map.get(binding["dataset_id"])
            if dataset is None:
                continue
            alias = f"dataset_{index}"
            # allowlist 用は列名のみ（sql_builder が完全一致で検証する）。
            column_names = [c.name for c in dataset.columns]
            # プロンプト表示用は名前・型・説明（いずれも安全に露出できる metadata）。
            column_detail = [
                {
                    "name": c.name,
                    "data_type": c.data_type,
                    "description": c.description,
                }
                for c in dataset.columns
            ]
            forced_filter = dict(binding.get("binding_keys") or {})
            display = {
                "name": dataset.name,
                "description": dataset.description,
                "row_count": dataset.row_count,
            }
            resolved.append(
                ResolvedDataset(
                    alias=alias,
                    backend_path=dataset.s3_path,
                    columns=column_names,
                    forced_filter=forced_filter or None,
                    display=display,
                )
            )
            descriptors.append(
                {
                    "alias": alias,
                    "name": dataset.name,
                    "description": dataset.description,
                    "row_count": dataset.row_count,
                    "columns": column_detail,
                }
            )

        if not resolved:
            return [], []

        tool = create_analyze_dataset_tool(resolved, self.backend, self.limits)
        return [tool], descriptors

    # ------------------------------------------------------------------
    # persona 生成 source 経路
    # ------------------------------------------------------------------

    def build_source_tools(
        self,
        source_descriptors: Sequence[Mapping[str, Any]],
    ) -> List[Callable]:
        """アップロード CSV の source 記述子から単一 analyze_dataset ツールを作る。

        別名の採番は Manager が行う。Service は受け取った別名を検証・保持するだけで
        再採番しない（combined preview と tool の別名不一致を防ぐ）。
        """
        seen: set[str] = set()
        resolved: List[ResolvedDataset] = []
        for descriptor in source_descriptors:
            alias = descriptor.get("alias")
            if not alias or alias in seen:
                raise ValueError(f"invalid or duplicate source alias: {alias!r}")
            seen.add(alias)
            resolved.append(
                ResolvedDataset(
                    alias=alias,
                    backend_path=descriptor["path"],
                    columns=list(descriptor["columns"]),
                    forced_filter=None,
                    display={"name": alias},
                )
            )

        if not resolved:
            return []

        tool = create_analyze_dataset_tool(resolved, self.backend, self.limits)
        return [tool]
