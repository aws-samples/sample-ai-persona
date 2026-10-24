"""ペルソナエージェントへの統合機能（KB / データセット分析）と記憶の扱いを組み立てるコンポーネント。

AgentDiscussionManager / InterviewManager が共通で使うビジネスワークフロー。以前は
両 Manager に同一メソッド（`_build_integration_sections` / `_resolve_dataset_bindings`）が
コピーされており、security-sensitive な fail-closed dedup が2箇所に分散していた。単一
ソース化するためこの Component に集約する。

配置の理由: 中身は「有効判定・binding の fail-closed 除外・ツール構築・prompt section
構築」というビジネスルールなので shared/（純粋ヘルパー）には置けない。かといって
Manager 同士は import できない。よって Manager → Component → Service の依存方向を持つ
Manager 層のワークフロー部品とする。Router/Manager は import しない。

U6（約束 C1・C9、BR6.1、BR6.3）:

- :meth:`PersonaAgentIntegration.build_agent` は参加者ごとの ``ExecutionContext`` を
  受け取る。``write_memory`` が偽（所有者でない、admin を含む）なら、利用者が選んだ
  ``full`` を ``retrieve_only`` にする（会話を長期記憶に書き込まない）。
- :meth:`PersonaAgentIntegration.asset_filter` は、紐付いた KB・データセットのうち
  ``can_use_asset`` が偽のものを除く（prompt にもツールにも入れない）。ペルソナ専用の
  データセット（``PersonaBinding.dedicated``）は除かない。利用範囲の読み取りに届かない
  資産も除いて（fail-closed）会話は続ける。除いた資産は ``agent.asset_excluded`` に出す。
"""

from dataclasses import dataclass
from typing import Any, List, Optional

from ...config import config
from ...models.authz import ExecutionContext, Principal, ResourceType
from ...models.errors import LookupUnavailableError
from ...models.persona_binding import BindingTargetType, PersonaBinding
from ..shared.agent_integration import combine_integration
from ..shared.structured_log import log_event
from .access_policy import AccessPolicy
from .resource_lookup import ResourceLookup


@dataclass(frozen=True)
class IntegrationBundle:
    """統合機能の組み立て結果。Manager はこれを agent 生成にそのまま渡す。"""

    enhanced_prompt: str
    additional_tools: Optional[List[Any]]


def _asset_type(binding: PersonaBinding) -> ResourceType:
    if binding.target_type is BindingTargetType.KB:
        return ResourceType.KNOWLEDGE_BASE
    return ResourceType.DATASET


class PersonaAgentIntegration:
    """KB・データセット分析ツールと prompt section を組み立てる Manager 層の部品。

    Service は Manager と同様にコンストラクタ注入で受け取る（Component は
    service_factory を知らず、テスト時はモックを注入できる）。
    """

    def __init__(
        self,
        dataset_store: Any,
        knowledge_base_store: Any,
        agent_service: Any,
        dataset_analysis_service: Any,
        binding_store: Any = None,
        resource_lookup: Optional[ResourceLookup] = None,
    ) -> None:
        """
        Args:
            dataset_store: Reads the bound datasets.
            knowledge_base_store: Reads the bound knowledge base.
            agent_service: Builds the KB tool and the agent.
            dataset_analysis_service: Builds the dataset tools.
            binding_store: ``PersonaBindingStore`` (U5): bindings are read
                with a ``persona_id`` query, never from the old tables.
            resource_lookup: Usage scopes of the organization assets (U6).
                Without it no organization asset is handed over (fail closed).
        """
        self._datasets = dataset_store
        self._knowledge_bases = knowledge_base_store
        self._agent_service = agent_service
        self._dataset_analysis_service = dataset_analysis_service
        self._binding_store = binding_store
        self._lookup = resource_lookup

    def _bindings(self, persona_id: str) -> List[PersonaBinding]:
        if self._binding_store is None:
            return []
        bindings: List[PersonaBinding] = self._binding_store.list_for_persona(
            persona_id
        )
        return bindings

    def bind_lookup(self, lookup: ResourceLookup) -> None:
        """Use ``lookup`` for asset usage scopes (for managers that build it lazily)."""
        self._lookup = lookup

    # --- 組織資産の除外（BR6.3） ------------------------------------------------------

    def asset_filter(
        self, principal: Principal, persona_id: str, bindings: List[PersonaBinding]
    ) -> List[PersonaBinding]:
        """The bindings whose asset ``principal`` may use (BR6.3, NFR1.51).

        Dedicated datasets are always kept. Assets whose usage scope cannot be
        read are dropped (``reason=lookup_failed``); the conversation goes on.
        """
        shared = [b for b in bindings if not (b.is_dataset and b.dedicated)]
        kept = [b for b in bindings if b.is_dataset and b.dedicated]
        for asset_type in (ResourceType.KNOWLEDGE_BASE, ResourceType.DATASET):
            group = [b for b in shared if _asset_type(b) is asset_type]
            if not group:
                continue
            try:
                if self._lookup is None:
                    raise LookupUnavailableError("no usage-scope lookup")
                targets = self._lookup.get_many(
                    asset_type, [b.target_id for b in group]
                )
            except LookupUnavailableError:
                self._excluded(persona_id, group, "lookup_failed")
                continue
            usable = [
                b
                for b in group
                if AccessPolicy.can_use_asset(principal, targets[b.target_id])
            ]
            self._excluded(
                persona_id, [b for b in group if b not in usable], "not_usable"
            )
            kept += usable
        return [b for b in bindings if b in kept]

    @staticmethod
    def _excluded(persona_id: str, bindings: List[PersonaBinding], reason: str) -> None:
        for binding in bindings:
            log_event(
                "agent.asset_excluded",
                persona_id=persona_id,
                asset_type=_asset_type(binding).value,
                asset_id=binding.target_id,
                reason=reason,
            )

    # --- 組み立て ------------------------------------------------------------------

    def prepare(
        self,
        persona_id: str,
        base_prompt: str,
        *,
        enable_kb: bool,
        enable_dataset: bool,
        principal: Principal,
    ) -> IntegrationBundle:
        """KB / データセット統合を解決し、prompt と tool を合成して返す。

        有効判定・DB 解決・資産の除外（``principal`` が使えるものだけ）・fail-closed
        除外・ツール構築はここで行い、prompt と tool の純粋な合成のみ shared の
        :func:`combine_integration` に委譲する。
        """
        sections, tool_groups = self._build_sections(
            persona_id,
            enable_kb=enable_kb,
            enable_dataset=enable_dataset,
            principal=principal,
        )
        enhanced_prompt, additional_tools = combine_integration(
            base_prompt, sections, tool_groups
        )
        return IntegrationBundle(
            enhanced_prompt=enhanced_prompt, additional_tools=additional_tools
        )

    def build_agent(
        self,
        context: ExecutionContext,
        persona: Any,
        system_prompt: str,
        *,
        enable_memory: bool,
        session_id: Optional[str],
        memory_mode: str,
        enable_dataset: bool,
        enable_kb: bool,
        model_id: Optional[str] = None,
        initial_messages: Optional[List[dict[str, Any]]] = None,
    ) -> Any:
        """One participant's agent, built for the user who runs it (C9, BR6.1).

        Args:
            context: The runner and the persona's owner (``ExecutionContext``).
            memory_mode: The user's choice; ``full`` becomes ``retrieve_only``
                when ``context.write_memory`` is false.
            initial_messages: History put into the agent directly (a rebuild,
                BR3.4); it never goes through the memory's save path.
        """
        effective_mode = memory_mode
        if memory_mode == "full" and not context.write_memory:
            effective_mode = "retrieve_only"
        bundle = self.prepare(
            persona.id,
            system_prompt,
            enable_kb=enable_kb,
            enable_dataset=enable_dataset,
            principal=context.principal,
        )
        return self._agent_service.create_persona_agent(
            persona=persona,
            system_prompt=bundle.enhanced_prompt,
            enable_memory=enable_memory and effective_mode != "disabled",
            session_id=session_id,
            additional_tools=bundle.additional_tools,
            memory_mode=effective_mode,
            model_id=model_id,
            initial_messages=initial_messages,
        )

    def _build_sections(
        self,
        persona_id: str,
        *,
        enable_kb: bool,
        enable_dataset: bool,
        principal: Principal,
    ) -> tuple[list[str], list[list[Any]]]:
        """KB / データセット統合の prompt section と tool group を組み立てる。"""
        from ...prompts.discussion_interview_prompts import (
            build_dataset_prompt_section,
            build_kb_prompt_section,
        )

        sections: list[str] = []
        tool_groups: list[list[Any]] = []
        use_dataset = enable_dataset and config.ENABLE_DATASET_ANALYSIS
        bindings = (
            self.asset_filter(principal, persona_id, self._bindings(persona_id))
            if (enable_kb or use_dataset)
            else []
        )

        if enable_kb:
            kb_binding = next(
                (b for b in bindings if b.target_type is BindingTargetType.KB), None
            )
            knowledge_base = (
                self._knowledge_bases.get(kb_binding.target_id)
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
        組織資産の除外（約束 C9 の asset_filter）は呼び出し側（``_build_sections``）で済ませる。
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
        datasets = [self._datasets.get(did) for did in dataset_ids]
        datasets = [d for d in datasets if d is not None]

        accepted_bindings = [
            {"dataset_id": b.target_id, "binding_keys": b.binding_keys}
            for b in accepted
        ]
        return accepted_bindings, datasets
