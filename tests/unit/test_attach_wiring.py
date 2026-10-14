"""紐付けの呼び出しと読み先の付け替え（U5、Step 23）。

- 議論の保存（クラシック・エージェント）とテンプレートの作成・更新が、資料・画像の
  ファイル ID で ``FileAttachments.attach_all`` を呼び、親の保存の失敗で ``detach`` する
  （reliability-design.md §3、BR5.3、NFR1.27、NFR1.38、AC2.2.2）
- 議論の開始・実行の資料の読み込みが ``can_read_file`` を通る（他人の資料は読まない）
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
from src.managers.survey_template_manager import (
    SurveyTemplateManager,
    SurveyTemplateManagerError,
)
from src.models.discussion import Discussion
from src.models.errors import CodedError, ErrorCode
from src.models.message import Message
from src.models.survey_template import Question, TemplateImage
from src.models.uploaded_file import AttachResult, UploadedFile, upload_key
from tests.conftest import MEMBER_A, MEMBER_B
from tests.error_helpers import raises_code

A = MEMBER_A.principal()
NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)
FILE_1 = "11111111-1111-4111-8111-111111111111"
FILE_2 = "22222222-2222-4222-8222-222222222222"


def _attachments() -> Mock:
    files = Mock()
    files.attach_all.side_effect = lambda principal, ids, ptype, pid: AttachResult(
        parent_type=ptype,
        parent_id=pid,
        original_expires_at={i: 100 for i in dict.fromkeys(ids)},
    )
    return files


def _discussion(mode: str = "classic", documents=None) -> Discussion:
    discussion = Discussion.create_new(
        topic="新商品の議論", participants=["p1", "p2"], documents=documents, mode=mode
    )
    for i in range(2):
        discussion = discussion.add_message(
            Message.create_new(
                persona_id=f"p{i + 1}",
                persona_name=f"ペルソナ{i + 1}",
                content="十分な長さの発言内容です。" * 3,
            )
        )
    return discussion


def _documents(*ids: str) -> list[dict]:
    return [
        {
            "id": i,
            "filename": "資料.pdf",
            "file_path": f"s3://bucket/uploads/{A.sub}/discussion_document/{i}_資料.pdf",
            "file_size": 3,
            "mime_type": "application/pdf",
        }
        for i in ids
    ]


# =============================================================================
# 議論の保存（クラシック）
# =============================================================================


class TestClassicDiscussionSave:
    def _manager(self, files: Mock) -> tuple[DiscussionManager, Mock]:
        db = Mock()
        db.save_discussion.side_effect = lambda d: d.id
        return (
            DiscussionManager(
                ai_service=Mock(), database_service=db, file_attachments=files
            ),
            db,
        )

    def test_save_attaches_the_documents_to_the_discussion(self):
        files = _attachments()
        manager, db = self._manager(files)
        discussion = _discussion(documents=_documents(FILE_1, FILE_2))

        manager.save_discussion(discussion, principal=A)

        files.attach_all.assert_called_once_with(
            A, [FILE_1, FILE_2], "discussion", discussion.id
        )
        db.save_discussion.assert_called_once()
        files.detach.assert_not_called()

    def test_failed_save_detaches(self):
        files = _attachments()
        manager, db = self._manager(files)
        db.save_discussion.side_effect = RuntimeError("db down")
        discussion = _discussion(documents=_documents(FILE_1))

        with raises_code(DiscussionManagerError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            manager.save_discussion(discussion, principal=A)
        (result,) = files.detach.call_args.args
        assert result.file_ids == (FILE_1,)

    def test_someone_elses_file_stops_the_save(self):
        files = _attachments()
        files.attach_all.side_effect = CodedError("x", code=ErrorCode.FILE_NOT_FOUND)
        manager, db = self._manager(files)

        with raises_code(DiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager.save_discussion(
                _discussion(documents=_documents(FILE_1)), principal=A
            )
        db.save_discussion.assert_not_called()

    def test_documents_without_a_user_are_never_attached(self):
        files = _attachments()
        manager, db = self._manager(files)
        with raises_code(DiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager.save_discussion(_discussion(documents=_documents(FILE_1)))
        files.attach_all.assert_not_called()
        db.save_discussion.assert_not_called()

    def test_no_documents_attach_nothing(self):
        files = _attachments()
        manager, db = self._manager(files)
        manager.save_discussion(_discussion())
        files.attach_all.assert_not_called()
        db.save_discussion.assert_called_once()

    def test_documents_are_read_through_attachable_file(self):
        files = _attachments()
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
        manager, db = self._manager(files)

        data, metadata = manager._load_documents([FILE_1], A)
        assert metadata[0]["id"] == FILE_1
        assert data[0]["file_path"] == "s3://bucket/key"
        db.get_uploaded_file_info.assert_not_called()
        files.readable_file.assert_not_called()

        # R-02: the same code as the save's attach_all
        with raises_code(DiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager._load_documents([FILE_2], A)


# =============================================================================
# 議論の保存（エージェント）
# =============================================================================


class TestAgentDiscussionSave:
    def _manager(self, files: Mock) -> tuple[AgentDiscussionManager, Mock]:
        db = Mock()
        db.save_discussion.side_effect = lambda d: d.id
        return (
            AgentDiscussionManager(
                agent_service=Mock(),
                database_service=db,
                dataset_analysis_service=Mock(),
                file_attachments=files,
                binding_store=Mock(),
            ),
            db,
        )

    def test_save_attaches_and_failure_detaches(self):
        files = _attachments()
        manager, db = self._manager(files)
        discussion = _discussion(mode="agent", documents=_documents(FILE_1))

        manager.save_agent_discussion(discussion, principal=A)
        files.attach_all.assert_called_once_with(
            A, [FILE_1], "discussion", discussion.id
        )

        db.save_discussion.side_effect = RuntimeError("down")
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_OPERATION_FAILED
        ):
            manager.save_agent_discussion(discussion, principal=A)
        files.detach.assert_called_once()

    def test_unattachable_file_stops_the_save(self):
        files = _attachments()
        files.attach_all.side_effect = CodedError("x", code=ErrorCode.FILE_NOT_FOUND)
        manager, db = self._manager(files)
        with raises_code(AgentDiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager.save_agent_discussion(
                _discussion(mode="agent", documents=_documents(FILE_1)), principal=A
            )
        db.save_discussion.assert_not_called()

    def test_documents_are_read_through_attachable_file(self, monkeypatch):
        files = _attachments()
        files.attachable_file.side_effect = CodedError(
            "x", code=ErrorCode.FILE_NOT_FOUND
        )
        manager, db = self._manager(files)

        # R-02: an unusable document stops the discussion (no longer skipped)
        with raises_code(AgentDiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager._load_and_attach_documents([FILE_1], [], None, principal=A)

        files.attachable_file.assert_called_once_with(A, FILE_1)
        files.readable_file.assert_not_called()
        db.get_uploaded_file_info.assert_not_called()

    def test_a_failed_read_is_an_operation_failure(self):
        files = _attachments()
        files.attachable_file.side_effect = CodedError(
            "x", code=ErrorCode.FILE_OPERATION_FAILED
        )
        manager, _ = self._manager(files)
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_OPERATION_FAILED
        ):
            manager._load_and_attach_documents([FILE_1], [], None, principal=A)


# =============================================================================
# テンプレートの作成・更新
# =============================================================================


def _image(file_id: str, owner: str = A.sub) -> TemplateImage:
    key = upload_key(owner, "survey_image", file_id, "a.png")
    return TemplateImage(
        id="img",
        name="画像",
        file_path=f"s3://bucket/{key}",
        mime_type="image/png",
        original_filename="a.png",
    )


class TestTemplateSave:
    def _manager(self, files: Mock) -> tuple[SurveyTemplateManager, Mock]:
        db = Mock()
        return (
            SurveyTemplateManager(
                database_service=db, ai_service=Mock(), file_attachments=files
            ),
            db,
        )

    def _questions(self) -> list[Question]:
        return [Question.create_free_text(text="感想は？")]

    def test_create_attaches_the_image_by_its_path(self):
        files = _attachments()
        manager, db = self._manager(files)

        template = manager.create_template(
            "テンプレ", self._questions(), [_image(FILE_1)], principal=A
        )

        files.attach_all.assert_called_once_with(
            A, [FILE_1], "survey_template", template.id
        )
        db.save_survey_template.assert_called_once()

    def test_create_failure_detaches(self):
        files = _attachments()
        manager, db = self._manager(files)
        db.save_survey_template.side_effect = RuntimeError("down")
        with raises_code(SurveyTemplateManagerError, ErrorCode.SURVEY_OPERATION_FAILED):
            manager.create_template(
                "テンプレ", self._questions(), [_image(FILE_1)], principal=A
            )
        files.detach.assert_called_once()

    def test_a_path_of_another_shape_is_not_found(self):
        files = _attachments()
        manager, db = self._manager(files)
        bad = TemplateImage(
            id="i",
            name="n",
            file_path="s3://bucket/survey_images/x.png",
            mime_type="image/png",
            original_filename="x.png",
        )
        with raises_code(SurveyTemplateManagerError, ErrorCode.FILE_NOT_FOUND):
            manager.create_template("テンプレ", self._questions(), [bad], principal=A)
        db.save_survey_template.assert_not_called()

    def test_update_attaches_only_new_images(self):
        files = _attachments()
        manager, db = self._manager(files)
        from src.models.survey_template import SurveyTemplate

        existing = SurveyTemplate.create_new(
            name="旧", questions=self._questions(), images=[_image(FILE_1)]
        )
        db.get_survey_template.return_value = existing

        # keeping the image attaches nothing
        manager.update_template(
            existing.id, "新", self._questions(), [_image(FILE_1)], principal=A
        )
        files.attach_all.assert_not_called()

        # replacing it attaches only the new one (one image per template)
        manager.update_template(
            existing.id, "新", self._questions(), [_image(FILE_2)], principal=A
        )
        files.attach_all.assert_called_once_with(
            A, [FILE_2], "survey_template", existing.id
        )


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


def _upload(file_id: str = FILE_1) -> UploadedFile:
    return UploadedFile.create_new(
        owner_sub=A.sub,
        file_type="discussion_document",
        original_filename="資料.pdf",
        file_size=3,
        file_hash="h",
        mime_type="application/pdf",
        now=datetime.now(timezone.utc),
        file_id=file_id,
    ).with_path(f"s3://bucket/uploads/{A.sub}/discussion_document/{file_id}_資料.pdf")


def _attached_elsewhere_files():
    """A real FileAttachments whose FILE_1 is already attached to another discussion."""
    from src.managers.components.file_attachments import FileAttachments

    attached = _upload().attached_to("discussion", "another-discussion")
    return FileAttachments(_FakeFileStore(attached), None)


def _personas():
    from src.models.persona import Persona

    return [
        Persona.create_new(
            name=f"ペルソナ{i}",
            age=30,
            occupation="会社員",
            background="背景",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        for i in range(2)
    ]


class TestDocumentsAttachedElsewhere:
    """A document attached to another discussion stops before the AI runs (R-02).

    functional-spec.md WF6 step 5, BR5.3, BR5.4, AC2.2.2: the four start paths
    (classic / agent, plain / streaming) read only the user's unexpired,
    unattached uploads, so the failure comes before the discussion, not at
    the save.
    """

    def _classic(self):
        ai = Mock()
        manager = DiscussionManager(
            ai_service=ai,
            database_service=Mock(),
            file_attachments=_attached_elsewhere_files(),
        )
        return manager, ai

    def _agent(self):
        manager = AgentDiscussionManager(
            agent_service=Mock(),
            database_service=Mock(),
            dataset_analysis_service=Mock(),
            file_attachments=_attached_elsewhere_files(),
            binding_store=Mock(),
        )
        return manager

    def test_classic(self):
        manager, ai = self._classic()
        with raises_code(DiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager.run_classic_discussion(
                _personas(), "新商品の議論", document_ids=[FILE_1], principal=A
            )
        ai.facilitate_discussion.assert_not_called()

    def test_classic_streaming(self):
        manager, ai = self._classic()
        with raises_code(DiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            list(
                manager.run_classic_discussion_streaming(
                    _personas(), "新商品の議論", document_ids=[FILE_1], principal=A
                )
            )
        ai.facilitate_discussion_streaming.assert_not_called()

    def test_agent(self):
        manager = self._agent()
        facilitator = Mock()
        with raises_code(AgentDiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            manager.start_agent_discussion(
                personas=_personas(),
                topic="新商品の議論",
                persona_agents=[Mock(), Mock()],
                facilitator=facilitator,
                document_ids=[FILE_1],
                principal=A,
            )
        facilitator.start_discussion.assert_not_called()

    def test_agent_streaming(self):
        manager = self._agent()
        facilitator = Mock()
        with raises_code(AgentDiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            list(
                manager.start_agent_discussion_streaming(
                    personas=_personas(),
                    topic="新商品の議論",
                    persona_agents=[Mock(), Mock()],
                    facilitator=facilitator,
                    document_ids=[FILE_1],
                    principal=A,
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
        data, metadata = manager._load_documents([FILE_1], A)
        assert metadata[0]["id"] == FILE_1

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
        component = pai.PersonaAgentIntegration(
            database_service=db,
            agent_service=agent,
            dataset_analysis_service=Mock(),
            binding_store=store,
        )

        component.prepare("p1", "BASE", enable_kb=True, enable_dataset=False)

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
