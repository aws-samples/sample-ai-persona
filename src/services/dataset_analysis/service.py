"""DatasetAnalysisService: the Service entry point that assembles the analyze_dataset tool.

Entry descriptors are split by use (binding path / persona-generation source path),
but are normalized into a common :class:`ResolvedDataset` internally before building tools/SQL.
Deduplicating bindings is the Manager's job, not this Service's.
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
)

from .dataset_tools import ResolvedDataset, ToolLimits, create_analyze_dataset_tool
from .query_backend import DatasetQueryBackend, DuckDBQueryBackend

if TYPE_CHECKING:
    from ...models.dataset import Dataset

logger = logging.getLogger(__name__)


class DatasetAnalysisService:
    """Create the analyze_dataset tool and the matching prompt descriptors."""

    def __init__(
        self,
        bucket_name: str = "",
        region_name: str = "us-east-1",
        backend: Optional[DatasetQueryBackend] = None,
        limits: Optional[ToolLimits] = None,
        max_concurrent_queries: int = 4,
    ) -> None:
        # If a backend is injected, its concurrency setting is respected and not overridden here
        # (max_concurrent_queries only applies when the default DuckDB backend is created).
        self.backend: DatasetQueryBackend = backend or DuckDBQueryBackend(
            bucket_name=bucket_name,
            region_name=region_name,
            max_concurrent_queries=max_concurrent_queries,
        )
        self.limits = limits or ToolLimits()

    # ------------------------------------------------------------------
    # Binding path (discussion / interview)
    # ------------------------------------------------------------------

    def build_binding_tools(
        self,
        accepted_bindings: Sequence[Mapping[str, Any]],
        datasets: Sequence["Dataset"],
    ) -> Tuple[List[Callable], List[Dict[str, Any]]]:
        """Build a single analyze_dataset tool and descriptors from deduplicated bindings.

        Returns:
            (tools, accepted_descriptors)
            tools is a single tool (0 or 1 elements). accepted_descriptors is display
            metadata for the prompt (no backend_path / forced_filter values).
        """
        dataset_map = {d.id: d for d in datasets}
        resolved: List[ResolvedDataset] = []
        descriptors: List[Dict[str, Any]] = []

        for index, binding in enumerate(accepted_bindings, start=1):
            dataset = dataset_map.get(binding["dataset_id"])
            if dataset is None:
                continue
            alias = f"dataset_{index}"
            # The allowlist gets column names only (sql_builder validates by exact match).
            column_names = [c.name for c in dataset.columns]
            # Prompt display gets name, type and description (all safe-to-expose metadata).
            column_detail = [
                {
                    "name": c.name,
                    "data_type": c.data_type,
                    "description": c.description,
                }
                for c in dataset.columns
            ]
            forced_filter = dict(binding.get("binding_keys") or {})
            resolved.append(
                ResolvedDataset(
                    alias=alias,
                    backend_path=dataset.s3_path,
                    columns=column_names,
                    forced_filter=forced_filter or None,
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
    # Persona-generation source path
    # ------------------------------------------------------------------

    def build_source_tools(
        self,
        source_descriptors: Sequence[Mapping[str, Any]],
    ) -> List[Callable]:
        """Build a single analyze_dataset tool from uploaded-CSV source descriptors.

        Alias numbering is done by the Manager. This Service only validates and keeps the given aliases
        and never renumbers them (prevents mismatches between the combined preview and the tool aliases).
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
                )
            )

        if not resolved:
            return []

        tool = create_analyze_dataset_tool(resolved, self.backend, self.limits)
        return [tool]
