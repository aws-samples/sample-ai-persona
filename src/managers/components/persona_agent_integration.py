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
from ...models.persona_binding import BindingTargetType, PersonaBinding
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
        binding_store: Any = None,
    ) -> None:
        """
        Args:
            database_service: Reads datasets and knowledge bases.
            agent_service: Builds the KB tool and the agent.
            dataset_analysis_service: Builds the dataset tools.
            binding_store: ``PersonaBindingStore`` (U5): bindings are read
                with a ``persona_id`` query, never from the old tables.
        """
        self._database_service = database_service
        self._agent_service = agent_service
        self._dataset_analysis_service = dataset_analysis_service
        self._binding_store = binding_store

    def _bindings(self, persona_id: str) -> List[PersonaBinding]:
        if self._binding_store is None:
            return []
        bindings: List[PersonaBinding] = self._binding_store.list_for_persona(
            persona_id
        )
        return bindings

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

    def create_agent(
        self,
        persona: Any,
        system_prompt: str,
        *,
        enable_memory: bool,
        session_id: Optional[str],
        memory_mode: str,
        enable_dataset: bool,
        enable_kb: bool,
        model_id: Optional[str] = None,
    ) -> Any:
        """統合機能（KB / データセット）を組み立ててペルソナエージェントを生成する。

        discussion / interview Manager で共通の agent 生成手順。統合の組み立ては
        :meth:`prepare` に委ね、生成は注入された agent_service に委譲する。
        """
        bundle = self.prepare(
            persona.id,
            system_prompt,
            enable_kb=enable_kb,
            enable_dataset=enable_dataset,
        )
        return self._agent_service.create_persona_agent(
            persona=persona,
            system_prompt=bundle.enhanced_prompt,
            enable_memory=enable_memory,
            session_id=session_id,
            additional_tools=bundle.additional_tools,
            memory_mode=memory_mode,
            model_id=model_id,
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
        use_dataset = enable_dataset and config.ENABLE_DATASET_ANALYSIS
        bindings = self._bindings(persona_id) if (enable_kb or use_dataset) else []

        if enable_kb:
            kb_binding = next(
                (b for b in bindings if b.target_type is BindingTargetType.KB), None
            )
            knowledge_base = (
                self._database_service.get_knowledge_base(kb_binding.target_id)
                if kb_binding is not None
                else None
            )
            kb_tools, kb_info = self._agent_service.get_kb_tools(
                kb_binding, knowledge_base
            )
            tool_groups.append(kb_tools)
            if kb_info:
                sections.append(build_kb_prompt_section(**kb_info))

        # global kill switch と session フラグの AND。
        if use_dataset:
            accepted_bindings, datasets = self._resolve_dataset_bindings(
                persona_id, bindings
            )
            ds_tools, descriptors = self._dataset_analysis_service.build_binding_tools(
                accepted_bindings, datasets
            )
            tool_groups.append(ds_tools)
            if descriptors:
                sections.append(build_dataset_prompt_section(descriptors))

        return sections, tool_groups

    def _resolve_dataset_bindings(
        self, persona_id: str, bindings: Optional[List[PersonaBinding]] = None
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        """ペルソナの binding を解決し、重複 dataset を丸ごと除外して返す。

        persona×dataset に複数 binding が存在する dataset はどれを使うか一意に定まらない
        ため、その dataset を丸ごと除外する（fail-closed）。一意な空 binding_keys は
        従来どおり全行アクセスとして許可する。U5 の PersonaBindings はキーが
        persona_id + target_key のため重複は起きないが、除外の規則は残す。
        組織資産の除外（約束 C9 の asset_filter）は U6。
        """
        dataset_bindings = [
            b
            for b in (bindings if bindings is not None else self._bindings(persona_id))
            if b.is_dataset
        ]
        if not dataset_bindings:
            return [], []

        counts: dict[str, int] = {}
        for b in dataset_bindings:
            counts[b.target_id] = counts.get(b.target_id, 0) + 1

        accepted = [b for b in dataset_bindings if counts[b.target_id] == 1]
        if not accepted:
            return [], []

        dataset_ids = list({b.target_id for b in accepted})
        datasets = [self._database_service.get_dataset(did) for did in dataset_ids]
        datasets = [d for d in datasets if d is not None]

        accepted_bindings = [
            {"dataset_id": b.target_id, "binding_keys": b.binding_keys}
            for b in accepted
        ]
        return accepted_bindings, datasets
