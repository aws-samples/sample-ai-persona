"""Real U6 Managers behind the routers, on the fakes of tests/unit/discussion_world.py.

The API tests sign the client in as :data:`ME` (``MEMBER_A``, in ``sales``)
and patch the routers' ``get_*_manager`` functions with Managers wired to one
:class:`ApiWorld`. Decisions are never mocked: ``AccessPolicy``,
``ResourceSharing``, ``ResourceLookup``, ``ParticipantReader`` and
``DiscussionRecords`` are the real ones.

- ``p1``: owned by ME; ``p2``: owned by ``b``, shared with ``sales``;
  ``p3``: owned by ``c`` (ME cannot read it)
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import Mock, patch

from src.managers.agent_discussion_manager import AgentDiscussionManager
from src.managers.discussion_manager import DiscussionManager
from src.managers.persona_manager import PersonaManager
from src.managers.report_manager import ReportManager
from src.models.authz import AccessTarget, Principal, ResourceType, Role
from src.models.discussion import Discussion
from src.models.message import Message
from src.services.agent_service import FacilitatorAgent, PersonaAgent
from tests.conftest import MEMBER_A
from tests.unit.discussion_world import World, persona

ME = MEMBER_A
#: Someone else in ``sales`` and someone outside it.
PEER = Principal(
    sub="b", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
STRANGER = Principal(
    sub="c", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"plan"})
)
PERSONA = ResourceType.PERSONA


def _messages(world: World, ids: tuple[str, ...] = ("p1", "p2")) -> list[Message]:
    return [
        Message.create_new(
            pid, world.persona_bodies[pid].name, f"{pid}の十分に長い発言です" * 6
        )
        for pid in ids
    ]


def _agent(**kw: Any) -> Mock:
    agent = Mock(spec=PersonaAgent)
    agent.get_persona_id.return_value = kw["persona"].id
    agent.get_persona_name.return_value = kw["persona"].name
    agent.respond.return_value = f"{kw['persona'].name}の発言です"
    agent.respond_streaming.side_effect = lambda *a, **k: iter(["発言", "です"])
    return agent


@dataclass
class ApiWorld:
    world: World = field(default_factory=World)

    def __post_init__(self) -> None:
        w = self.world
        w.personas.targets["p1"] = AccessTarget(PERSONA, "p1", owner_sub=ME.sub)
        w.persona_bodies["p1"] = persona("p1", ME.sub, "佐藤")
        self.ai = Mock()
        self.ai.facilitate_discussion.side_effect = lambda *a, **k: _messages(w)
        self.ai.facilitate_discussion_streaming.side_effect = lambda *a, **k: iter(
            _messages(w)
        )
        self.ai.extract_insights.return_value = [
            {
                "category": "顧客",
                "description": "十分に長いインサイトの説明です",
                "confidence_score": 0.8,
            }
        ]
        self.ai.generate_standard_report_streaming.side_effect = lambda **kw: iter(
            ["レポート", "本文"]
        )
        self.agent_service = Mock()
        self.agent_service.create_persona_agent.side_effect = _agent
        self.agent_service.run_report_agent_streaming.side_effect = lambda **kw: iter(
            ["分析"]
        )
        facilitator = Mock(spec=FacilitatorAgent)
        facilitator.rounds = 1
        facilitator.additional_instructions = ""
        facilitator.invoke.return_value = "まとめ"
        facilitator.invoke_streaming.side_effect = lambda *a, **k: iter(["まとめ"])
        self.agent_service.create_facilitator_agent.return_value = facilitator
        self.s3 = Mock()
        self.s3.generate_presigned_url.return_value = "https://signed"
        self.discussions = DiscussionManager(
            ai_service=self.ai,
            database_service=Mock(),
            file_attachments=w.files,
            discussion_records=w.records,
            participants=w.participants,
            resource_sharing=w.sharing,
            owners=w.owners,
            s3_service=self.s3,
        )
        self.agent_discussions = AgentDiscussionManager(
            agent_service=self.agent_service,
            database_service=Mock(),
            dataset_analysis_service=Mock(),
            file_attachments=w.files,
            binding_store=Mock(list_for_persona=Mock(return_value=[])),
            discussion_records=w.records,
            participants=w.participants,
            resource_lookup=w.lookup,
            ai_service=self.ai,
        )
        self.reports = ReportManager(
            ai_service=self.ai,
            agent_service=self.agent_service,
            database_service=Mock(),
            discussion_records=w.records,
            participants=w.participants,
        )
        self.personas = PersonaManager(
            database_service=Mock(), participants=w.participants
        )

    # --- data -------------------------------------------------------------------

    def discussion(
        self,
        owner: Principal | None = None,
        *,
        topic: str = "新商品のマーケティングについて",
        mode: str = "classic",
        shared_with: tuple[str, ...] = (),
    ) -> Discussion:
        """A saved discussion of ``owner`` (ME when omitted)."""
        saver = owner or ME.principal()
        discussion = Discussion.create_new(
            topic=topic,
            participants=["p1", "p2"],
            mode=mode,
            participant_names={"p1": "佐藤", "p2": "鈴木"},
        )
        saved = self.world.records.save_new(saver, discussion, _messages(self.world))
        if shared_with:
            self.world.share(saved.id, *shared_with)
        return saved

    # --- routers ----------------------------------------------------------------

    def patch_routers(self, stack: ExitStack) -> None:
        for target, value in [
            ("web.routers.discussion.get_discussion_manager", self.discussions),
            (
                "web.routers.discussion.get_agent_discussion_manager",
                self.agent_discussions,
            ),
            ("web.routers.discussion.get_report_manager", self.reports),
            ("web.routers.discussion.get_persona_manager", self.personas),
            ("web.routers.api.get_discussion_manager", self.discussions),
            ("web.routers.api.get_agent_discussion_manager", self.agent_discussions),
            ("web.routers.api.get_persona_manager", self.personas),
        ]:
            stack.enter_context(patch(target, return_value=value))
