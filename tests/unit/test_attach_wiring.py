"""紐付けの呼び出しと読み先の付け替え（U5、Step 23。U6 で議論の保存を付け替え）。

- 議論の保存（クラシック・エージェント）が、資料のファイル ID で
  ``FileAttachments.attach_all`` を呼び、親の保存の失敗で ``detach`` する。
  U6 からは両 Manager とも ``DiscussionRecords.save_new`` を通る
  （reliability-design.md §3、BR5.3、NFR1.27、NFR1.38、AC2.2.2）
- テンプレートの作成・更新が画像のファイル ID で ``attach_all`` を呼ぶ
- 議論の開始・実行の資料の読み込みが ``attachable_file`` を通る（他人の資料は読まない）
- ``PersonaAgentIntegration`` は紐付けを ``PersonaBindingStore`` から読み、
  ``AgentService.get_kb_tools`` は KB の紐付けを引数で受け取る

security-design.md §7「紐付けの呼び出しの漏れの検査」。
"""

from datetime import datetime, timezone
from unittest.mock import Mock


from src.managers.agent_discussion_manager import (
    AgentDiscussionManager,
    AgentDiscussionManagerError,
)
from src.managers.discussion_manager import DiscussionManager, DiscussionManagerError
from src.models.errors import CodedError, ErrorCode
from src.models.message import Message
from src.models.uploaded_file import UploadedFile
from src.services.agent_service import FacilitatorAgent, PersonaAgent
from src.services.discussion_store import DiscussionStoreError
from tests.conftest import MEMBER_A, MEMBER_B
from tests.error_helpers import raises_code
from tests.unit.discussion_world import OWNER, World

A = MEMBER_A.principal()
NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)
FILE_1 = "11111111-1111-4111-8111-111111111111"
FILE_2 = "22222222-2222-4222-8222-222222222222"
TOPIC = "新商品の議論について"


def _own_upload(file_id: str) -> UploadedFile:
    return UploadedFile.create_new(
        owner_sub=OWNER.sub,
        file_type="discussion_document",
        original_filename="資料.pdf",
        file_size=3,
        file_hash="h",
        mime_type="application/pdf",
        now=NOW,
        file_id=file_id,
    ).with_path(f"s3://bucket/uploads/{OWNER.sub}/discussion_document/{file_id}.pdf")


def _world_with_uploads(*file_ids: str) -> World:
    world = World()
    uploads = {i: _own_upload(i) for i in file_ids}
    world.files.attachable_file.side_effect = lambda principal, fid: uploads[fid]
    return world


def _messages(world: World) -> list[Message]:
    return [
        Message.create_new(p.id, p.name, "十分な長さの発言内容です。" * 4)
        for p in (world.persona_bodies["p1"], world.persona_bodies["p2"])
    ]


# =============================================================================
# 議論の保存（クラシック）
# =============================================================================


class TestClassicDiscussionSave:
    def _manager(self, world: World) -> tuple[DiscussionManager, Mock]:
        ai = Mock()
        ai.facilitate_discussion.side_effect = lambda *a, **k: _messages(world)
        ai.extract_insights.return_value = []
        return (
            DiscussionManager(
                ai_service=ai,
                database_service=Mock(),
                file_attachments=world.files,
                discussion_records=world.records,
                participants=world.participants,
                resource_sharing=world.sharing,
                owners=world.owners,
                s3_service=Mock(),
            ),
            ai,
        )

    def _run(self, manager, world, *file_ids):
        return manager.run_classic_discussion(
            OWNER,
            [world.persona_bodies["p1"], world.persona_bodies["p2"]],
            TOPIC,
            document_ids=list(file_ids) or None,
        )

    def test_save_attaches_the_documents_to_the_discussion(self):
        world = _world_with_uploads(FILE_1, FILE_2)
        manager, _ = self._manager(world)

        discussion = self._run(manager, world, FILE_1, FILE_2)

        world.files.attach_all.assert_called_once_with(
            OWNER, [FILE_1, FILE_2], "discussion", discussion.id
        )
        assert discussion.id in world.store.bodies
        world.files.detach.assert_not_called()

    def test_failed_save_detaches(self):
        world = _world_with_uploads(FILE_1)
        world.store.fail["put_new"] = [DiscussionStoreError("down")]
        manager, _ = self._manager(world)

        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            self._run(manager, world, FILE_1)
        (result,) = world.files.detach.call_args.args
        assert result.file_ids == (FILE_1,)

    def test_someone_elses_file_stops_the_save(self):
        world = _world_with_uploads(FILE_1)
        world.files.attach_all.side_effect = CodedError(
            "x", code=ErrorCode.FILE_NOT_FOUND
        )
        manager, _ = self._manager(world)

        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            self._run(manager, world, FILE_1)
        assert world.store.bodies == {}

    def test_no_documents_attach_nothing(self):
        world = World()
        manager, _ = self._manager(world)
        discussion = self._run(manager, world)
        world.files.attach_all.assert_not_called()
        assert discussion.id in world.store.bodies

    def test_documents_are_read_through_attachable_file(self):
        files = Mock()
        uploaded = UploadedFile.create_new(
            owner_sub=A.sub,
            file_type="discussion_document",
            original_filename="資料.pdf",
            file_size=3,
            file_hash="h",
            mime_type="application/pdf",
            now=NOW,
            file_id=FILE_1,
        ).with_path("s3://bucket/key")
        files.attachable_file.side_effect = lambda principal, fid: (
            uploaded
            if principal is A and fid == FILE_1
            else (_ for _ in ()).throw(CodedError("x", code=ErrorCode.FILE_NOT_FOUND))
        )
        db = Mock()
        manager = DiscussionManager(
            ai_service=Mock(), database_service=db, file_attachments=files
        )

        data, documents = manager._load_documents([FILE_1], A)
        assert documents[0].id == FILE_1
        assert data[0]["file_path"] == "s3://bucket/key"
        db.get_uploaded_file_info.assert_not_called()
        files.readable_file.assert_not_called()

        # R-02: the same code as the save's attach_all
        with raises_code(DiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager._load_documents([FILE_2], A)


# =============================================================================
# 議論の保存（エージェント）
# =============================================================================


def _agent_manager(world: World) -> AgentDiscussionManager:
    def agent(**kw):
        persona = kw["persona"]
        made = Mock(spec=PersonaAgent)
        made.get_persona_id.return_value = persona.id
        made.get_persona_name.return_value = persona.name
        made.respond.return_value = "発言です"
        return made

    agent_service = Mock()
    agent_service.create_persona_agent.side_effect = agent
    facilitator = Mock(spec=FacilitatorAgent)
    facilitator.rounds = 1
    facilitator.additional_instructions = ""
    facilitator.invoke.return_value = "まとめ"
    agent_service.create_facilitator_agent.return_value = facilitator
    ai = Mock()
    ai.extract_insights.return_value = []
    return AgentDiscussionManager(
        agent_service=agent_service,
        database_service=Mock(),
        dataset_analysis_service=Mock(),
        file_attachments=world.files,
        binding_store=Mock(list_for_persona=Mock(return_value=[])),
        discussion_records=world.records,
        participants=world.participants,
        resource_lookup=world.lookup,
        ai_service=ai,
    )


class TestAgentDiscussionSave:
    def _run(self, manager, world, *file_ids):
        return manager.run_agent_discussion_full(
            OWNER,
            [world.persona_bodies["p1"], world.persona_bodies["p2"]],
            TOPIC,
            rounds=1,
            document_ids=list(file_ids),
        )

    def test_save_attaches_and_failure_detaches(self, monkeypatch):
        monkeypatch.setattr(
            "src.managers.shared.document_loader.prepare_document_contents",
            lambda metadata, s3=None: [],
        )
        world = _world_with_uploads(FILE_1)
        manager = _agent_manager(world)

        discussion = self._run(manager, world, FILE_1)
        world.files.attach_all.assert_called_once_with(
            OWNER, [FILE_1], "discussion", discussion.id
        )

        world.store.fail["put_new"] = [DiscussionStoreError("down")]
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            self._run(manager, world, FILE_1)
        world.files.detach.assert_called_once()

    def test_unattachable_file_stops_the_save(self, monkeypatch):
        monkeypatch.setattr(
            "src.managers.shared.document_loader.prepare_document_contents",
            lambda metadata, s3=None: [],
        )
        world = _world_with_uploads(FILE_1)
        world.files.attach_all.side_effect = CodedError(
            "x", code=ErrorCode.FILE_NOT_FOUND
        )
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            self._run(_agent_manager(world), world, FILE_1)
        assert world.store.bodies == {}

    def test_documents_are_read_through_attachable_file(self):
        world = World()
        world.files.attachable_file.side_effect = CodedError(
            "x", code=ErrorCode.FILE_NOT_FOUND
        )
        manager = _agent_manager(world)

        # R-02: an unusable document stops the discussion (no longer skipped)
        with raises_code(AgentDiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager._load_and_attach_documents([FILE_1], [], None, principal=A)

        world.files.attachable_file.assert_called_once_with(A, FILE_1)
        world.files.readable_file.assert_not_called()

    def test_a_failed_read_is_an_operation_failure(self):
        world = World()
        world.files.attachable_file.side_effect = CodedError(
            "x", code=ErrorCode.FILE_OPERATION_FAILED
        )
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_OPERATION_FAILED
        ):
            _agent_manager(world)._load_and_attach_documents(
                [FILE_1], [], None, principal=A
            )


# テンプレートの作成・更新の画像の紐付けは U7 で本体を先に書く順に変わったため、
# tests/unit/test_survey_template_manager.py（TestCreateTemplate・TestUpdateTemplate）で確かめる。


# =============================================================================
# R-02: 議論の資料の読み取りは、保存時の紐付けと同じ条件（本人・期限内・未紐付け）
# =============================================================================


class _FakeFileStore:
    """In-memory ``UploadedFileStore`` (reads only) for a real FileAttachments."""

    def __init__(self, *files: UploadedFile) -> None:
        self.files = {f.id: f for f in files}

    def get(self, file_id):
        return self.files.get(file_id)

    def get_many(self, file_ids):
        return {i: self.files[i] for i in file_ids if i in self.files}

    def transact_attach(self, *args):  # pragma: no cover - never reached here
        raise AssertionError("no attach is expected")

    def transact_detach(self, *args):  # pragma: no cover - never reached here
        raise AssertionError("no detach is expected")


def _upload(file_id: str = FILE_1, owner: str = A.sub) -> UploadedFile:
    return UploadedFile.create_new(
        owner_sub=owner,
        file_type="discussion_document",
        original_filename="資料.pdf",
        file_size=3,
        file_hash="h",
        mime_type="application/pdf",
        now=datetime.now(timezone.utc),
        file_id=file_id,
    ).with_path(f"s3://bucket/uploads/{owner}/discussion_document/{file_id}_資料.pdf")


def _attached_elsewhere_files():
    """A real FileAttachments whose FILE_1 is already attached to another discussion."""
    from src.managers.components.file_attachments import FileAttachments

    attached = _upload(owner=OWNER.sub).attached_to("discussion", "another-discussion")
    return FileAttachments(_FakeFileStore(attached), None)


class TestDocumentsAttachedElsewhere:
    """A document attached to another discussion stops before the AI runs (R-02).

    functional-spec.md WF6 step 5, BR5.3, BR5.4, AC2.2.2: the four start paths
    (classic / agent, plain / streaming) read only the user's unexpired,
    unattached uploads, so the failure comes before the discussion, not at
    the save.
    """

    def _classic(self):
        world = World()
        ai = Mock()
        manager = DiscussionManager(
            ai_service=ai,
            database_service=Mock(),
            file_attachments=_attached_elsewhere_files(),
            discussion_records=world.records,
            participants=world.participants,
            resource_sharing=world.sharing,
            owners=world.owners,
            s3_service=Mock(),
        )
        return manager, ai, world

    def _agent(self):
        world = World()
        world.files = _attached_elsewhere_files()
        manager = _agent_manager(world)
        return manager, world

    def _personas(self, world):
        return [world.persona_bodies["p1"], world.persona_bodies["p2"]]

    def test_classic(self):
        manager, ai, world = self._classic()
        with raises_code(DiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager.run_classic_discussion(
                OWNER, self._personas(world), TOPIC, document_ids=[FILE_1]
            )
        ai.facilitate_discussion.assert_not_called()

    def test_classic_streaming(self):
        manager, ai, world = self._classic()
        with raises_code(DiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            list(
                manager.run_classic_discussion_streaming(
                    OWNER, self._personas(world), TOPIC, document_ids=[FILE_1]
                )
            )
        ai.facilitate_discussion_streaming.assert_not_called()

    def test_agent(self):
        manager, world = self._agent()
        facilitator = manager.agent_service.create_facilitator_agent.return_value
        with raises_code(AgentDiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager.run_agent_discussion_full(
                OWNER, self._personas(world), TOPIC, rounds=1, document_ids=[FILE_1]
            )
        facilitator.start_discussion.assert_not_called()

    def test_agent_streaming(self):
        manager, world = self._agent()
        facilitator = manager.agent_service.create_facilitator_agent.return_value
        with raises_code(AgentDiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            list(
                manager.run_agent_discussion_streaming(
                    OWNER,
                    self._personas(world),
                    TOPIC,
                    rounds=1,
                    document_ids=[FILE_1],
                )
            )
        facilitator.start_discussion.assert_not_called()

    def test_an_unattached_own_upload_is_still_read(self):
        from src.managers.components.file_attachments import FileAttachments

        manager = DiscussionManager(
            ai_service=Mock(),
            database_service=Mock(),
            file_attachments=FileAttachments(_FakeFileStore(_upload()), None),
        )
        _data, documents = manager._load_documents([FILE_1], A)
        assert documents[0].id == FILE_1

    def test_reading_and_attaching_share_one_rule(self):
        """attach_all refuses exactly the files the start paths refuse."""
        from src.managers.components.file_attachments import FileAttachments

        files = FileAttachments(_FakeFileStore(_upload()), None)
        now = int(datetime.now(timezone.utc).timestamp())
        own = _upload()
        assert files.is_attachable(A, own, now)
        assert not files.is_attachable(
            A, own.attached_to("discussion", "another-discussion"), now
        )
        assert not files.is_attachable(MEMBER_B.principal(), own, now)
        assert not files.is_attachable(A, own, int(own.expires_at or 0))


# =============================================================================
# 読み先の付け替え（PersonaAgentIntegration、AgentService.get_kb_tools）
# =============================================================================


class TestBindingReaders:
    def test_integration_reads_bindings_from_the_binding_store(self, monkeypatch):
        from src.managers.components import persona_agent_integration as pai
        from src.models.knowledge_base import KnowledgeBase
        from src.models.persona_binding import PersonaBinding

        kb = KnowledgeBase.create_new("KB123", "社内KB")
        store = Mock()
        store.list_for_persona.return_value = [
            PersonaBinding.for_kb("p1", kb.id, metadata_filters={"a": "b"}, now=NOW)
        ]
        db = Mock()
        db.get_knowledge_base.return_value = kb
        agent = Mock()
        agent.get_kb_tools.return_value = ([], None)
        from src.managers.components.resource_lookup import build_resource_lookup
        from src.models.authz import AccessTarget, Principal, ResourceType, Role

        class _AnyKb:
            def read(self, ids, deadline):
                return {i: AccessTarget(ResourceType.KNOWLEDGE_BASE, i) for i in ids}

        component = pai.PersonaAgentIntegration(
            database_service=db,
            agent_service=agent,
            dataset_analysis_service=Mock(),
            binding_store=store,
            resource_lookup=build_resource_lookup(
                [(ResourceType.KNOWLEDGE_BASE, _AnyKb())], sample_rate=0.0
            ),
        )

        component.prepare(
            "p1",
            "BASE",
            enable_kb=True,
            enable_dataset=False,
            principal=Principal(sub="admin", roles=frozenset({Role.ADMIN})),
        )

        store.list_for_persona.assert_called_once_with("p1")
        (binding, knowledge_base) = agent.get_kb_tools.call_args.args
        assert binding.target_id == kb.id
        assert knowledge_base is kb
        db.get_kb_binding_by_persona.assert_not_called()

    def test_get_kb_tools_takes_the_binding_as_an_argument(self, monkeypatch):
        from src.models.knowledge_base import KnowledgeBase
        from src.models.persona_binding import PersonaBinding
        from src.services import agent_service as module

        created = {}
        monkeypatch.setattr(
            "src.services.knowledge_base.kb_tools.create_kb_retrieval_tool",
            lambda **kw: created.update(kw) or "tool",
        )
        service = module.AgentService.__new__(module.AgentService)
        kb = KnowledgeBase.create_new("KB123", "社内KB", "説明")

        tools, info = service.get_kb_tools(
            PersonaBinding.for_kb("p1", kb.id, metadata_filters={"a": "b"}), kb
        )

        assert tools == ["tool"]
        assert info == {
            "name": "社内KB",
            "description": "説明",
            "metadata_filters": {"a": "b"},
        }
        assert created["knowledge_base_id"] == "KB123"
        assert service.get_kb_tools(None, None) == ([], None)
