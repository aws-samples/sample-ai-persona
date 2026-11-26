"""Component that assembles agent integrations (KB / dataset analysis) and memory handling for persona agents.

Business workflow shared by AgentDiscussionManager and InterviewManager. Both
Managers used to carry identical methods (`_build_integration_sections` /
`_resolve_dataset_bindings`), which spread the security-sensitive fail-closed
dedup over two places. It is collected here as a single source.

Why a Component: the content is business rules (enable checks, fail-closed
exclusion of bindings, tool construction, prompt section construction), so it
cannot live in shared/ (pure helpers), and Managers cannot import each other.
It is therefore a workflow part of the Manager layer with the dependency
direction Manager -> Component -> Service. It does not import Routers or Managers.

Per-user rules:

- :meth:`PersonaAgentIntegration.build_agent` receives an ``ExecutionContext``
  per participant. When ``write_memory`` is false (not the owner, admins
  included), the ``full`` the user chose becomes ``retrieve_only`` (the
  conversation is not written to long-term memory).
- :meth:`PersonaAgentIntegration.asset_filter` drops bound KBs and datasets for
  which ``can_use_asset`` is false (they enter neither prompt nor tools).
  Persona-dedicated datasets (``PersonaBinding.dedicated``) are not dropped.
  Assets whose usage scope cannot be read are also dropped (fail closed) and
  the conversation goes on. Dropped assets are logged as ``agent.asset_excluded``.
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
    """Result of assembling the integrations. The Manager passes it straight to agent creation."""

    enhanced_prompt: str
    additional_tools: Optional[List[Any]]


def _asset_type(binding: PersonaBinding) -> ResourceType:
    if binding.target_type is BindingTargetType.KB:
        return ResourceType.KNOWLEDGE_BASE
    return ResourceType.DATASET


class PersonaAgentIntegration:
    """Manager-layer part that assembles KB / dataset analysis tools and prompt sections.

    Services are taken by constructor injection like Managers (the Component
    knows nothing of service_factory, so tests can inject mocks).
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
            binding_store: ``PersonaBindingStore``: bindings are read
                with a ``persona_id`` query, never from the old tables.
            resource_lookup: Usage scopes of the organization assets.
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

    # Excluding organization assets

    def asset_filter(
        self, principal: Principal, persona_id: str, bindings: List[PersonaBinding]
    ) -> List[PersonaBinding]:
        """The bindings whose asset ``principal`` may use.

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

    # Assembly

    def prepare(
        self,
        persona_id: str,
        base_prompt: str,
        *,
        enable_kb: bool,
        enable_dataset: bool,
        principal: Principal,
    ) -> IntegrationBundle:
        """Resolve KB / dataset integrations, combine prompt and tools, and return them.

        Enable checks, DB resolution, asset exclusion (only what ``principal``
        may use), fail-closed exclusion and tool construction happen here; only
        the pure combination of prompt and tools is delegated to shared
        :func:`combine_integration`.
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
        """One participant's agent, built for the user who runs it.

        Args:
            context: The runner and the persona's owner (``ExecutionContext``).
            memory_mode: The user's choice; ``full`` becomes ``retrieve_only``
                when ``context.write_memory`` is false.
            initial_messages: History put into the agent directly (a rebuild);
                it never goes through the memory's save path.
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
        """Assemble the prompt sections and tool groups of the KB / dataset integrations."""
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

        # AND of the global kill switch and the session flag.
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
        """Resolve a persona's bindings, dropping duplicated datasets entirely.

        A dataset with several bindings for one persona has no unique choice,
        so it is dropped entirely (fail closed). A unique empty binding_keys
        still means access to all rows. Keys are persona_id + target_key, so
        duplicates should not occur, but the exclusion rule is kept.
        Exclusion of organization assets (``asset_filter``) is done by the
        caller (``_build_sections``).
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
