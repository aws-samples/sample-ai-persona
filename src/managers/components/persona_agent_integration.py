"""ペルソナエージェントへの統合機能（KB / データセット分析）組み立てコンポーネント。

AgentDiscussionManager / InterviewManager が共通で使うビジネスワークフロー。以前は
両 Manager に同一メソッド（`_build_integration_sections` / `_resolve_dataset_bindings`）が
コピーされており、security-sensitive な fail-closed dedup が2箇所に分散していた。単一
ソース化するためこの Component に集約する。

配置の理由: 中身は「有効判定・binding の fail-closed 除外・ツール構築・prompt section
構築」というビジネスルールなので shared/（純粋ヘルパー）には置けない。かといって
Manager 同士は import できない。よって Manager → Component → Service の依存方向を持つ
Manager 層のワークフロー部品とする。Router/Manager は import しない。
"""

from dataclasses import dataclass
from typing import Any, List, Optional

from ...config import config
from ..shared.agent_integration import combine_integration


@dataclass(frozen=True)
class IntegrationBundle:
    """統合機能の組み立て結果。Manager はこれを agent 生成にそのまま渡す。"""

    enhanced_prompt: str
    additional_tools: Optional[List[Any]]


class PersonaAgentIntegration:
    """KB・データセット分析ツールと prompt section を組み立てる Manager 層の部品。

    Service は Manager と同様にコンストラクタ注入で受け取る（Component は
    service_factory を知らず、テスト時はモックを注入できる）。
    """

    def __init__(
        self,
        database_service: Any,
        agent_service: Any,
        dataset_analysis_service: Any,
    ) -> None:
        self._database_service = database_service
        self._agent_service = agent_service
        self._dataset_analysis_service = dataset_analysis_service

    def prepare(
        self,
        persona_id: str,
        base_prompt: str,
        *,
        enable_kb: bool,
        enable_dataset: bool,
    ) -> IntegrationBundle:
        """KB / データセット統合を解決し、prompt と tool を合成して返す。

        有効判定・DB 解決・fail-closed 除外・ツール構築はここで行い、prompt と tool の
        純粋な合成のみ shared の :func:`combine_integration` に委譲する。
        """
        sections, tool_groups = self._build_sections(
            persona_id, enable_kb=enable_kb, enable_dataset=enable_dataset
        )
        enhanced_prompt, additional_tools = combine_integration(
            base_prompt, sections, tool_groups
        )
        return IntegrationBundle(
            enhanced_prompt=enhanced_prompt, additional_tools=additional_tools
        )

    def _build_sections(
        self, persona_id: str, *, enable_kb: bool, enable_dataset: bool
    ) -> tuple[list[str], list[list[Any]]]:
        """KB / データセット統合の prompt section と tool group を組み立てる。"""
        from ...prompts.discussion_interview_prompts import (
            build_dataset_prompt_section,
            build_kb_prompt_section,
        )

        sections: list[str] = []
        tool_groups: list[list[Any]] = []

        if enable_kb:
            kb_tools, kb_info = self._agent_service.get_kb_tools(
                persona_id, self._database_service
            )
            tool_groups.append(kb_tools)
            if kb_info:
                sections.append(build_kb_prompt_section(**kb_info))

        # global kill switch と session フラグの AND。
        if enable_dataset and config.ENABLE_DATASET_ANALYSIS:
            accepted_bindings, datasets = self._resolve_dataset_bindings(persona_id)
            ds_tools, descriptors = self._dataset_analysis_service.build_binding_tools(
                accepted_bindings, datasets
            )
            tool_groups.append(ds_tools)
            if descriptors:
                sections.append(build_dataset_prompt_section(descriptors))

        return sections, tool_groups

    def _resolve_dataset_bindings(
        self, persona_id: str
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        """ペルソナの binding を解決し、重複 dataset を丸ごと除外して返す。

        persona×dataset に複数 binding が存在する dataset はどれを使うか一意に定まらない
        ため、その dataset を丸ごと除外する（fail-closed）。一意な空 binding_keys は
        従来どおり全行アクセスとして許可する。
        """
        bindings = self._database_service.get_bindings_by_persona(persona_id)
        if not bindings:
            return [], []

        counts: dict[str, int] = {}
        for b in bindings:
            counts[b.dataset_id] = counts.get(b.dataset_id, 0) + 1

        accepted = [b for b in bindings if counts[b.dataset_id] == 1]
        if not accepted:
            return [], []

        dataset_ids = list({b.dataset_id for b in accepted})
        datasets = [self._database_service.get_dataset(did) for did in dataset_ids]
        datasets = [d for d in datasets if d is not None]

        accepted_bindings = [
            {"dataset_id": b.dataset_id, "binding_keys": b.binding_keys}
            for b in accepted
        ]
        return accepted_bindings, datasets
