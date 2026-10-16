"""
shared/agent_integration.py 単体テスト

combine_integration は純粋な合成関数（service/db/flag に触れない）。
gate・DB 解決・build は Manager テスト側で検証する。
"""

import pytest
from unittest.mock import Mock

from src.managers.shared.agent_integration import combine_integration


@pytest.mark.unit
class TestCombineIntegration:
    """combine_integration のテスト"""

    def test_no_sections_no_tools(self):
        """section も tool も無ければプロンプト変更なし・ツールなし"""
        prompt, tools = combine_integration("base", [], [])
        assert prompt == "base"
        assert tools is None

    def test_empty_strings_and_lists_ignored(self):
        """空文字 section・空 tool グループは無視される"""
        prompt, tools = combine_integration("base", ["", ""], [[], []])
        assert prompt == "base"
        assert tools is None

    def test_sections_concatenated_in_order(self):
        """section は順に連結される"""
        prompt, _ = combine_integration("base", ["\nA", "\nB"], [])
        assert prompt == "base\nA\nB"

    def test_tool_groups_flattened(self):
        """tool グループは平坦化して結合される"""
        t1, t2, t3 = Mock(), Mock(), Mock()
        _, tools = combine_integration("base", [], [[t1], [t2, t3]])
        assert tools == [t1, t2, t3]

    def test_sections_and_tools_together(self):
        """section と tool の両方を合成する"""
        kb_tool, ds_tool = Mock(), Mock()
        prompt, tools = combine_integration(
            "base",
            ["\n# KB section", "\n# dataset section"],
            [[kb_tool], [ds_tool]],
        )
        assert "# KB section" in prompt
        assert "# dataset section" in prompt
        assert tools == [kb_tool, ds_tool]

    def test_is_pure_no_service_or_db_arguments(self):
        """combine_integration は base_prompt / sections / tool_groups のみを受け取る。

        （service / database_service / flag を引数に取らない = 純粋性の担保）
        """
        import inspect

        params = list(inspect.signature(combine_integration).parameters)
        assert params == ["base_prompt", "sections", "tool_groups"]


# =============================================================================
# U6: 実行する人に合わせた記憶と組織資産（約束 C1・C9、BR6.1、BR6.3、NFR1.50、
# NFR1.51、NFR8.21、AC4.2.2、AC4.2.3）
# =============================================================================

from datetime import datetime  # noqa: E402

from src.managers.components import persona_agent_integration as pai  # noqa: E402
from src.managers.components.persona_agent_integration import (  # noqa: E402
    PersonaAgentIntegration,
)
from src.managers.components.resource_lookup import build_resource_lookup  # noqa: E402
from src.models.authz import (  # noqa: E402
    AccessTarget,
    ExecutionContext,
    Principal,
    ResourceType,
    Role,
)
from src.models.errors import CodedError, ErrorCode  # noqa: E402
from src.models.knowledge_base import KnowledgeBase  # noqa: E402
from src.models.persona import Persona  # noqa: E402
from src.models.persona_binding import PersonaBinding  # noqa: E402
from tests.log_helpers import LogCapture  # noqa: E402

_NOW = datetime(2026, 10, 15)
_OWNER = Principal(
    sub="a", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
_PEER = Principal(
    sub="b", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
_ADMIN = Principal(sub="admin", roles=frozenset({Role.ADMIN}))


def _persona_obj() -> Persona:
    return Persona(
        id="p1",
        name="佐藤",
        age=30,
        occupation="会社員",
        background="背景",
        values=[],
        pain_points=[],
        goals=[],
        created_at=_NOW,
        updated_at=_NOW,
        owner_sub="a",
    )


def _context(principal: Principal) -> ExecutionContext:
    return ExecutionContext.for_persona(
        principal, AccessTarget(ResourceType.PERSONA, "p1", owner_sub="a")
    )


class _AssetReader:
    """KB / dataset decision columns: no usage scope until U7 (admin only)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def read(self, ids, deadline):
        if self.error is not None:
            raise self.error
        return {i: AccessTarget(ResourceType.KNOWLEDGE_BASE, i) for i in ids}


class _Unreachable(CodedError):
    code = ErrorCode.LOOKUP_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("down")
        self.reason = "unreachable"


def _integration(
    monkeypatch, *, lookup_error: Exception | None = None, bindings=None
) -> tuple[PersonaAgentIntegration, Mock, Mock]:
    monkeypatch.setattr(pai.config, "ENABLE_DATASET_ANALYSIS", True)
    kb = KnowledgeBase.create_new("KB123", "社内KB")
    store = Mock()
    store.list_for_persona.return_value = (
        bindings
        if bindings is not None
        else [
            PersonaBinding.for_kb("p1", kb.id, now=_NOW),
            PersonaBinding.for_dataset("p1", "ds-org", now=_NOW),
            PersonaBinding.for_dataset("p1", "ds-own", dedicated=True, now=_NOW),
        ]
    )
    db = Mock()
    db.get_knowledge_base.return_value = kb
    db.get_dataset.side_effect = lambda did: Mock(id=did)
    agent = Mock()
    agent.get_kb_tools.side_effect = lambda b, k: (
        (["kb-tool"], {"name": "社内KB", "description": "", "metadata_filters": {}})
        if b is not None
        else ([], None)
    )
    datasets = Mock()
    datasets.build_binding_tools.side_effect = lambda accepted, ds: (
        [f"tool-{a['dataset_id']}" for a in accepted],
        [
            {
                "alias": a["dataset_id"],
                "name": a["dataset_id"],
                "description": "",
                "row_count": 1,
                "columns": [],
            }
            for a in accepted
        ],
    )
    reader = _AssetReader(lookup_error)
    lookup = build_resource_lookup(
        [(ResourceType.KNOWLEDGE_BASE, reader), (ResourceType.DATASET, reader)],
        sample_rate=0.0,
    )
    component = PersonaAgentIntegration(
        database_service=db,
        agent_service=agent,
        dataset_analysis_service=datasets,
        binding_store=store,
        resource_lookup=lookup,
    )
    return component, agent, datasets


def _build(component, principal, memory_mode="full", **kwargs):
    return component.build_agent(
        _context(principal),
        _persona_obj(),
        "SYSTEM",
        enable_memory=True,
        session_id="d1",
        memory_mode=memory_mode,
        enable_dataset=True,
        enable_kb=True,
        **kwargs,
    )


@pytest.mark.unit
class TestMemoryByRunner:
    @pytest.mark.parametrize(
        ("principal", "chosen", "expected"),
        [
            (_OWNER, "full", "full"),
            (_OWNER, "retrieve_only", "retrieve_only"),
            (_OWNER, "disabled", "disabled"),
            (_PEER, "full", "retrieve_only"),
            (_PEER, "retrieve_only", "retrieve_only"),
            (_PEER, "disabled", "disabled"),
            (_ADMIN, "full", "retrieve_only"),
            (_ADMIN, "retrieve_only", "retrieve_only"),
            (_ADMIN, "disabled", "disabled"),
        ],
    )
    def test_only_the_owner_writes_memory(
        self, monkeypatch, principal, chosen, expected
    ):
        component, agent, _ = _integration(monkeypatch, bindings=[])
        _build(component, principal, memory_mode=chosen)
        assert agent.create_persona_agent.call_args.kwargs["memory_mode"] == expected

    def test_history_is_handed_to_the_agent_service(self, monkeypatch):
        component, agent, _ = _integration(monkeypatch, bindings=[])
        history = [{"role": "user", "content": [{"text": "q"}]}]
        _build(component, _OWNER, initial_messages=history)
        assert (
            agent.create_persona_agent.call_args.kwargs["initial_messages"] == history
        )


@pytest.mark.unit
class TestAssetFilter:
    def test_member_gets_no_organization_assets_but_keeps_dedicated(
        self, monkeypatch, capsys
    ):
        logs = LogCapture(capsys)
        component, agent, datasets = _integration(monkeypatch)
        _build(component, _OWNER)
        kwargs = agent.create_persona_agent.call_args.kwargs
        assert kwargs["additional_tools"] == ["tool-ds-own"]
        assert "社内KB" not in kwargs["system_prompt"]
        assert "ds-org" not in kwargs["system_prompt"]
        accepted = datasets.build_binding_tools.call_args.args[0]
        assert [a["dataset_id"] for a in accepted] == ["ds-own"]
        excluded = logs.events("agent.asset_excluded")
        assert {(e["asset_type"], e["reason"]) for e in excluded} == {
            ("knowledge_base", "not_usable"),
            ("dataset", "not_usable"),
        }
        assert "ds-org" in {e["asset_id"] for e in excluded}
        assert all(e["persona_id"] == "p1" for e in excluded)

    def test_admin_gets_every_asset(self, monkeypatch):
        component, agent, _ = _integration(monkeypatch)
        _build(component, _ADMIN)
        tools = agent.create_persona_agent.call_args.kwargs["additional_tools"]
        assert set(tools) == {"kb-tool", "tool-ds-org", "tool-ds-own"}

    def test_lookup_failure_excludes_the_assets_and_continues(
        self, monkeypatch, capsys
    ):
        logs = LogCapture(capsys)
        component, agent, _ = _integration(monkeypatch, lookup_error=_Unreachable())
        _build(component, _ADMIN)
        assert agent.create_persona_agent.call_args.kwargs["additional_tools"] == [
            "tool-ds-own"
        ]
        reasons = {e["reason"] for e in logs.events("agent.asset_excluded")}
        assert reasons == {"lookup_failed"}

    def test_without_a_lookup_nothing_shared_is_handed_over(self, monkeypatch):
        component, agent, _ = _integration(monkeypatch)
        component._lookup = None
        _build(component, _ADMIN)
        assert agent.create_persona_agent.call_args.kwargs["additional_tools"] == [
            "tool-ds-own"
        ]
