"""SurveyTemplateManager の単体テスト（U7）。

BR1.1〜BR1.7、BR1.9、BR2.3、NFR1.66、NFR1.67、NFR1.69、NFR1.83、NFR1.84、NFR2.21、
NFR2.25、NFR8.25、AC2.1.1〜AC2.1.3、AC4.1.1〜AC4.1.3、AC5.1.2、AC5.1.4。

DynamoDB は moto、ResourceSharing・ResourceLookup・AccessPolicy は本物
（tests/unit/survey_world.py）。AI と FileAttachments はモック。
"""

from unittest.mock import Mock

import pytest

from src.managers.survey_template_manager import (
    MAX_TEMPLATE_BYTES,
    SurveyTemplateManager,
    SurveyTemplateManagerError,
    SurveyTemplateValidationError,
)
from src.models.authz import ResourceType
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.sharing import ListTab
from src.models.survey_template import Question, SurveyTemplate, TemplateImage
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.survey_world import (
    ADMIN,
    MEMBER_A,
    MEMBER_B,
    MEMBER_C,
    NO_ROLE,
    SurveyWorld,
)

TEMPLATE = ResourceType.SURVEY_TEMPLATE
FILE_1 = "11111111-1111-4111-8111-111111111111"
FILE_2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def ai() -> Mock:
    return Mock()


@pytest.fixture
def mgr(survey_world: SurveyWorld, ai: Mock) -> SurveyTemplateManager:
    return SurveyTemplateManager(
        survey_template_store=survey_world.template_store,
        ai_service=ai,
        file_attachments=survey_world.files,
        resource_sharing=survey_world.sharing,
        owners=survey_world.owners,
        resource_lookup=survey_world.lookup,
        sleep=lambda _s: None,
    )


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


@pytest.fixture
def sample_questions() -> list:
    return [
        Question.create_multiple_choice("Q1", ["A", "B"]),
        Question.create_free_text("Q2"),
    ]


def _image(file_id: str, owner: str = "a") -> TemplateImage:
    return TemplateImage(
        id=f"img-{file_id[:4]}",
        name="画像",
        file_path=f"s3://bucket/uploads/{owner}/survey_image/{file_id}_a.png",
        mime_type="image/png",
        original_filename="a.png",
    )


def _stored(world: SurveyWorld, template_id: str) -> dict | None:
    return world.raw(world.templates_table, template_id)


# =============================================================================
# 作成（BR1.1、NFR1.83、NFR2.25）
# =============================================================================


class TestCreateTemplate:
    def test_records_the_owner_and_shares_with_no_one(
        self, mgr, survey_world, sample_questions
    ):
        template = mgr.create_template(MEMBER_A, "テスト", sample_questions)
        item = _stored(survey_world, template.id)
        assert item["owner_sub"] == {"S": "a"}
        assert "shared_group_ids" not in item
        assert template.owner_sub == "a"
        listed = mgr.list_templates(MEMBER_A, "mine", None)
        assert [t.id for t in listed.items] == [template.id]

    def test_needs_resource_create(self, mgr, survey_world, sample_questions):
        with pytest.raises(AccessDeniedError):
            mgr.create_template(NO_ROLE, "テスト", sample_questions)
        assert (
            survey_world.client.scan(TableName=survey_world.templates_table)["Count"]
            == 0
        )

    def test_over_200kb_writes_nothing(self, mgr, survey_world):
        long_text = "あ" * 3000
        questions = [Question.create_free_text(long_text) for _ in range(30)]
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_TOO_LARGE):
            mgr.create_template(MEMBER_A, "大きい", questions, [_image(FILE_1)])
        assert (
            survey_world.client.scan(TableName=survey_world.templates_table)["Count"]
            == 0
        )
        survey_world.files.attach_all.assert_not_called()

    def test_the_size_limit_is_200kb(self):
        assert MAX_TEMPLATE_BYTES == 200 * 1024

    def test_attaches_the_image_after_the_body(
        self, mgr, survey_world, sample_questions
    ):
        template = mgr.create_template(
            MEMBER_A, "画像つき", sample_questions, [_image(FILE_1)]
        )
        survey_world.files.attach_all.assert_called_once_with(
            MEMBER_A, [FILE_1], "survey_template", template.id
        )
        assert _stored(survey_world, template.id) is not None

    def test_failed_attach_removes_the_body(self, mgr, survey_world, sample_questions):
        survey_world.files.attach_all.side_effect = CodedError(
            "not attachable", code=ErrorCode.FILE_NOT_FOUND
        )
        with raises_code(SurveyTemplateManagerError, ErrorCode.FILE_NOT_FOUND):
            mgr.create_template(MEMBER_A, "画像", sample_questions, [_image(FILE_1)])
        assert (
            survey_world.client.scan(TableName=survey_world.templates_table)["Count"]
            == 0
        )

    def test_image_of_another_user_is_not_found(
        self, mgr, survey_world, sample_questions
    ):
        with raises_code(SurveyTemplateManagerError, ErrorCode.FILE_NOT_FOUND):
            mgr.create_template(
                MEMBER_A, "画像", sample_questions, [_image(FILE_1, owner="b")]
            )
        assert (
            survey_world.client.scan(TableName=survey_world.templates_table)["Count"]
            == 0
        )

    def test_a_path_of_another_shape_is_not_found(
        self, mgr, survey_world, sample_questions
    ):
        bad = TemplateImage(
            id="i",
            name="n",
            file_path="s3://bucket/survey_images/x.png",
            mime_type="image/png",
            original_filename="x.png",
        )
        with raises_code(SurveyTemplateManagerError, ErrorCode.FILE_NOT_FOUND):
            mgr.create_template(MEMBER_A, "画像", sample_questions, [bad])
        survey_world.files.attach_all.assert_not_called()

    def test_empty_name_raises(self, mgr, sample_questions):
        with pytest.raises(SurveyTemplateValidationError):
            mgr.create_template(MEMBER_A, "  ", sample_questions)

    def test_no_questions_raises(self, mgr):
        with pytest.raises(SurveyTemplateValidationError):
            mgr.create_template(MEMBER_A, "名前", [])

    def test_invalid_multiple_choice_raises(self, mgr):
        bad_q = [Question.create_multiple_choice("Q", ["一つだけ"])]
        with pytest.raises(SurveyTemplateValidationError):
            mgr.create_template(MEMBER_A, "名前", bad_q)


# =============================================================================
# 一覧（BR1.2、NFR1.66、NFR2.21、NFR2.26）
# =============================================================================


class TestListTemplates:
    @pytest.fixture
    def seeded(self, survey_world):
        world = survey_world
        mine = world.template("a", "Aの共有なし")
        shared = world.template("a", "Aの営業共有")
        world.share(TEMPLATE, shared.id, "sales")
        c_own = world.template("c", "Cのもの")
        legacy = world.template(None, "古いテンプレート")
        b_shared = world.template("b", "Bの営業共有")
        world.share(TEMPLATE, b_shared.id, "sales")
        return {
            "mine": mine,
            "shared": shared,
            "c": c_own,
            "legacy": legacy,
            "b": b_shared,
        }

    def test_mine_is_the_default_and_counts_each_tab(self, mgr, seeded):
        listing = mgr.list_templates(MEMBER_A, None, None)
        assert listing.tab is ListTab.MINE
        assert {t.id for t in listing.items} == {
            seeded["mine"].id,
            seeded["shared"].id,
        }
        assert listing.counts == {"mine": 2, "shared": 1}
        assert listing.show_all is False

    def test_shared_tab_has_what_others_shared_with_my_department(self, mgr, seeded):
        listing = mgr.list_templates(MEMBER_A, "shared", None)
        assert [t.id for t in listing.items] == [seeded["b"].id]
        assert listing.owners == {seeded["b"].id: "利用者b"}
        assert listing.manageable == frozenset()

    def test_other_department_sees_nothing_of_a(self, mgr, seeded):
        listing = mgr.list_templates(MEMBER_C, "shared", None)
        assert listing.items == []
        mine = mgr.list_templates(MEMBER_C, "mine", None)
        assert [t.id for t in mine.items] == [seeded["c"].id]

    def test_member_asking_for_all_gets_mine(self, mgr, seeded):
        listing = mgr.list_templates(MEMBER_A, "all", None)
        assert listing.tab is ListTab.MINE

    def test_admin_all_tab_includes_ownerless_rows(self, mgr, seeded):
        listing = mgr.list_templates(ADMIN, "all", None)
        assert listing.tab is ListTab.ALL
        assert seeded["legacy"].id in {t.id for t in listing.items}
        assert listing.owners[seeded["legacy"].id] == "所有者不明"
        assert seeded["legacy"].id in listing.manageable
        assert listing.counts["all"] == 5

    def test_manageable_marks_own_rows(self, mgr, seeded):
        listing = mgr.list_templates(MEMBER_A, "mine", None)
        assert listing.manageable == frozenset({seeded["mine"].id, seeded["shared"].id})
        assert listing.shared_with[seeded["shared"].id] == ["営業部"]

    def test_search_stays_inside_the_tab(self, mgr, seeded):
        listing = mgr.list_templates(MEMBER_A, "mine", None, search="営業")
        assert [t.id for t in listing.items] == [seeded["shared"].id]
        shared = mgr.list_templates(MEMBER_A, "shared", None, search="Aの")
        assert shared.items == []

    def test_member_lists_and_searches_without_a_scan(self, mgr, seeded, survey_world):
        scan = survey_world.spy("scan")
        mgr.list_templates(MEMBER_A, "mine", None)
        mgr.list_templates(MEMBER_A, "shared", None)
        mgr.list_templates(MEMBER_A, "mine", None, search="A")
        mgr.list_templates(MEMBER_A, "shared", None, search="B")
        assert scan.call_count == 0

    def test_a_body_unshared_after_indexing_is_not_listed(
        self, mgr, seeded, survey_world
    ):
        # 共有の索引が残っていても、本体を読み直して can_read で落とす（BR1.2）
        survey_world.client.update_item(
            TableName=survey_world.templates_table,
            Key={"id": {"S": seeded["b"].id}},
            UpdateExpression="REMOVE shared_group_ids",
        )
        listing = mgr.list_templates(MEMBER_A, "shared", None)
        assert listing.items == []

    def test_unreadable_index_is_lookup_unavailable(self, mgr, seeded, survey_world):
        from botocore.exceptions import ClientError

        def broken(**_):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "down"}}, "Query"
            )

        survey_world.client.query = broken
        with raises_code(CodedError, ErrorCode.LOOKUP_UNAVAILABLE):
            mgr.list_templates(MEMBER_A, "mine", None)


# =============================================================================
# U7 Loop-back 1: 検索の 500 件の上限（NFR2.21、BR1.2）
# =============================================================================


class TestSearchReadsAtMost500:
    """A search reads the newest 500 IDs of the tab and never scans (NFR2.21)."""

    @staticmethod
    def _seed(world: SurveyWorld, owner: str, count: int, *share: str) -> list:
        """Write ``count`` templates (oldest first) with batch writes.

        One transaction per template takes minutes in moto, so the bodies and
        the share index items are written in the shape the services write.
        """
        from dataclasses import replace
        from datetime import timedelta

        from src.models.sharing import ResourceShareEntry
        from tests.unit.survey_world import NOW

        made, bodies, entries = [], [], []
        for i in range(count):
            at = NOW + timedelta(minutes=i)
            template = replace(
                SurveyTemplate.create_new(
                    name=f"検索{i:03d}", questions=[Question.create_free_text("Q")]
                ),
                created_at=at,
                updated_at=at,
            ).owned_by(owner)
            body = world.template_store._item(template.to_dict())
            if share:
                body["shared_group_ids"] = {"SS": sorted(share)}
                entries += [
                    ResourceShareEntry(
                        group, TEMPLATE, template.id, body["created_at"]["S"], owner
                    )
                    for group in share
                ]
            bodies.append(body)
            made.append(template)
        for start in range(0, len(bodies), 25):
            world.client.batch_write_item(
                RequestItems={
                    world.templates_table: [
                        {"PutRequest": {"Item": b}} for b in bodies[start : start + 25]
                    ]
                }
            )
        world.share_store.put_entries(entries)
        return made

    @staticmethod
    def _read_ids(world: SurveyWorld, monkeypatch) -> list:
        """Record every template ID whose body the manager reads."""
        read: list = []
        real = world.template_store.get_many

        def spy(ids):
            read.extend(ids)
            return real(ids)

        monkeypatch.setattr(world.template_store, "get_many", spy)
        return read

    def test_501_in_mine_reads_the_newest_500_and_marks_the_cut(
        self, mgr, survey_world, monkeypatch
    ):
        made = self._seed(survey_world, "a", 501)
        read = self._read_ids(survey_world, monkeypatch)
        scan = survey_world.spy("scan")

        listing = mgr.list_templates(MEMBER_A, "mine", None, search="検索")

        assert len(read) == 500
        assert set(read) == {t.id for t in made[1:]}
        assert listing.search_truncated is True
        assert listing.counts == {"mine": None}
        assert scan.call_count == 0
        # 最も古い 1 件は読まれないため、名前で探しても出ない
        oldest = mgr.list_templates(MEMBER_A, "mine", None, search="検索000")
        assert oldest.items == []

    def test_exactly_500_is_not_marked(self, mgr, survey_world, monkeypatch):
        made = self._seed(survey_world, "a", 500)
        read = self._read_ids(survey_world, monkeypatch)
        scan = survey_world.spy("scan")

        listing = mgr.list_templates(MEMBER_A, "mine", None, search="検索000")

        assert set(read) == {t.id for t in made}
        assert [t.id for t in listing.items] == [made[0].id]
        assert listing.search_truncated is False
        assert listing.counts == {"mine": 500}
        assert scan.call_count == 0

    def test_501_in_shared_reads_the_newest_500_without_a_scan(
        self, mgr, survey_world, monkeypatch
    ):
        made = self._seed(survey_world, "b", 501, "sales")
        read = self._read_ids(survey_world, monkeypatch)
        scan = survey_world.spy("scan")

        listing = mgr.list_templates(MEMBER_A, "shared", None, search="検索")

        assert len(read) == 500
        assert set(read) == {t.id for t in made[1:]}
        assert listing.search_truncated is True
        assert scan.call_count == 0


# =============================================================================
# 読む・管理する（BR1.3〜BR1.6、NFR1.67、NFR1.69）
# =============================================================================


class TestGetTemplate:
    def test_owner_and_shared_department_can_read(self, mgr, survey_world):
        template = survey_world.template("a")
        survey_world.share(TEMPLATE, template.id, "sales")
        assert mgr.get_template(MEMBER_A, template.id).id == template.id
        assert mgr.get_template(MEMBER_B, template.id).id == template.id

    @pytest.mark.parametrize("template_id", ["exists", "missing"])
    def test_unreadable_is_the_same_as_missing(self, mgr, survey_world, template_id):
        template = survey_world.template("a")
        target = template.id if template_id == "exists" else "missing-id"
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            mgr.get_template(MEMBER_C, target)

    def test_ownerless_rows_are_admin_only(self, mgr, survey_world):
        legacy = survey_world.template(None)
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            mgr.get_template(MEMBER_A, legacy.id)
        assert mgr.get_template(ADMIN, legacy.id).id == legacy.id

    def test_edit_screen_needs_manage(self, mgr, survey_world):
        template = survey_world.template("a")
        survey_world.share(TEMPLATE, template.id, "sales")
        assert mgr.get_template_for_edit(MEMBER_A, template.id).id == template.id
        with pytest.raises(AccessDeniedError):
            mgr.get_template_for_edit(MEMBER_B, template.id)
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            mgr.get_template_for_edit(MEMBER_C, template.id)
        assert mgr.get_template_for_edit(ADMIN, template.id).id == template.id


class TestUpdateTemplate:
    def test_partial_update_keeps_owner_shares_and_created_at(
        self, mgr, survey_world, sample_questions
    ):
        template = survey_world.template("a")
        survey_world.share(TEMPLATE, template.id, "sales")
        put = survey_world.spy("put_item")

        updated = mgr.update_template(MEMBER_A, template.id, "新名", sample_questions)

        assert updated.name == "新名"
        assert put.call_count == 0
        stored = survey_world.template_store.get(template.id, consistent=True)
        assert stored.name == "新名"
        assert stored.owner_sub == "a"
        assert stored.shared_group_ids == frozenset({"sales"})
        assert stored.created_at == template.created_at

    def test_admin_update_keeps_the_owner(self, mgr, survey_world, sample_questions):
        template = survey_world.template("a")
        mgr.update_template(ADMIN, template.id, "admin が直した", sample_questions)
        stored = survey_world.template_store.get(template.id, consistent=True)
        assert stored.owner_sub == "a"

    def test_shared_reader_gets_403_and_stranger_404(
        self, mgr, survey_world, sample_questions
    ):
        template = survey_world.template("a")
        survey_world.share(TEMPLATE, template.id, "sales")
        with pytest.raises(AccessDeniedError):
            mgr.update_template(MEMBER_B, template.id, "x", sample_questions)
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            mgr.update_template(MEMBER_C, template.id, "x", sample_questions)
        stored = survey_world.template_store.get(template.id, consistent=True)
        assert stored.name == template.name

    def test_missing_template_is_not_found_and_not_created(
        self, mgr, survey_world, sample_questions
    ):
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            mgr.update_template(MEMBER_A, "missing-id", "x", sample_questions)
        assert _stored(survey_world, "missing-id") is None

    def test_attaches_new_images_and_deletes_removed_ones(
        self, mgr, survey_world, sample_questions
    ):
        old = _image(FILE_1)
        template = survey_world.template("a", images=[old])

        mgr.update_template(
            MEMBER_A, template.id, "新", sample_questions, [_image(FILE_2)]
        )

        survey_world.files.attach_all.assert_called_once_with(
            MEMBER_A, [FILE_2], "survey_template", template.id
        )
        survey_world.files.delete_attached.assert_called_once_with(
            "survey_template", template.id, [FILE_1]
        )

    def test_keeping_the_image_attaches_nothing(
        self, mgr, survey_world, sample_questions
    ):
        old = _image(FILE_1)
        template = survey_world.template("a", images=[old])
        mgr.update_template(MEMBER_A, template.id, "新", sample_questions, [old])
        survey_world.files.attach_all.assert_not_called()
        survey_world.files.delete_attached.assert_not_called()

    def test_over_200kb_changes_nothing(self, mgr, survey_world):
        template = survey_world.template("a")
        questions = [Question.create_free_text("あ" * 3000) for _ in range(30)]
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_TOO_LARGE):
            mgr.update_template(MEMBER_A, template.id, "大", questions)
        stored = survey_world.template_store.get(template.id, consistent=True)
        assert stored.name == template.name


class TestDeleteTemplate:
    def test_owner_deletes_the_body_and_its_share_items(self, mgr, survey_world):
        template = survey_world.template("a", images=[_image(FILE_1)])
        survey_world.share(TEMPLATE, template.id, "sales")

        assert mgr.delete_template(MEMBER_A, template.id) is True

        assert _stored(survey_world, template.id) is None
        shares = survey_world.client.scan(TableName=survey_world.shares_table)
        assert shares["Count"] == 0
        survey_world.files.delete_attached.assert_called_once_with(
            "survey_template", template.id, [FILE_1]
        )

    def test_results_made_from_it_stay(self, mgr, survey_world):
        template = survey_world.template("a")
        survey = survey_world.survey("a", template)
        mgr.delete_template(MEMBER_A, template.id)
        assert survey_world.db.get_survey(survey.id) is not None

    def test_shared_reader_gets_403_and_stranger_404(self, mgr, survey_world):
        template = survey_world.template("a")
        survey_world.share(TEMPLATE, template.id, "sales")
        with pytest.raises(AccessDeniedError):
            mgr.delete_template(MEMBER_B, template.id)
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            mgr.delete_template(MEMBER_C, template.id)
        assert _stored(survey_world, template.id) is not None

    def test_missing_template_is_not_found(self, mgr):
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            mgr.delete_template(MEMBER_A, "missing-id")

    def test_cleanup_is_tried_three_times_then_logged(self, mgr, survey_world, logs):
        template = survey_world.template("a", images=[_image(FILE_1)])
        survey_world.files.delete_attached.return_value = [FILE_1]

        assert mgr.delete_template(MEMBER_A, template.id) is True

        assert survey_world.files.delete_attached.call_count == 3
        events = logs.events("survey.cleanup_failed")
        assert len(events) == 1
        assert events[0]["resource_type"] == "survey_template"
        assert events[0]["resource_id"] == template.id
        assert events[0]["step"] == "images"
        assert "s3://" not in str(events[0])

    def test_admin_deletes_an_ownerless_template(self, mgr, survey_world):
        legacy = survey_world.template(None)
        assert mgr.delete_template(ADMIN, legacy.id) is True


# =============================================================================
# AI 生成（BR2.3）
# =============================================================================


class TestGenerateAIChatResponse:
    def test_returns_reply(self, mgr, ai):
        ai.chat_for_survey.return_value = "ヒアリング回答"
        reply = mgr.generate_ai_chat_response(
            MEMBER_A, [{"role": "user", "content": "開始"}]
        )
        assert reply == "ヒアリング回答"

    def test_needs_ai_execute(self, mgr, ai):
        with pytest.raises(AccessDeniedError):
            mgr.generate_ai_chat_response(NO_ROLE, [{"role": "user", "content": "x"}])
        ai.chat_for_survey.assert_not_called()

    def test_no_ai_raises(self, mgr):
        mgr.ai_service = None  # type: ignore[assignment]
        with pytest.raises(SurveyTemplateManagerError):
            mgr.generate_ai_chat_response(MEMBER_A, [{"role": "user", "content": "hi"}])

    def test_empty_messages_raises(self, mgr):
        with pytest.raises(SurveyTemplateValidationError):
            mgr.generate_ai_chat_response(MEMBER_A, [])

    def test_last_message_not_user_raises(self, mgr):
        with pytest.raises(SurveyTemplateValidationError):
            mgr.generate_ai_chat_response(
                MEMBER_A, [{"role": "assistant", "content": "hi"}]
            )


class TestGenerateAIQuestionsDraft:
    def test_returns_questions(self, mgr, ai):
        ai.generate_survey_questions_draft.return_value = {
            "summary": "要約",
            "template_name": "テスト",
            "questions": [
                {"question_type": "free_text", "text": "Q1"},
                {
                    "question_type": "multiple_choice",
                    "text": "Q2",
                    "options": ["A", "B"],
                },
            ],
        }
        result = mgr.generate_ai_questions_draft(
            MEMBER_A, [{"role": "user", "content": "テスト"}]
        )
        assert result["summary"] == "要約"
        assert len(result["questions"]) == 2

    def test_needs_ai_execute(self, mgr, ai):
        with pytest.raises(AccessDeniedError):
            mgr.generate_ai_questions_draft(NO_ROLE, [{"role": "user", "content": "x"}])
        ai.generate_survey_questions_draft.assert_not_called()

    def test_no_questions_raises(self, mgr, ai):
        ai.generate_survey_questions_draft.return_value = {
            "summary": "",
            "questions": [],
        }
        with pytest.raises(SurveyTemplateManagerError):
            mgr.generate_ai_questions_draft(
                MEMBER_A, [{"role": "user", "content": "test"}]
            )


class TestValidateAIMessages:
    def test_too_long_message(self, mgr):
        with pytest.raises(SurveyTemplateValidationError):
            mgr._validate_ai_messages([{"role": "user", "content": "x" * 2001}])

    def test_too_many_messages(self, mgr):
        with pytest.raises(SurveyTemplateValidationError):
            mgr._validate_ai_messages([{"role": "user", "content": "hi"}] * 41)


def test_template_type_is_unchanged() -> None:
    """``SurveyTemplate`` stays the shape the screens read."""
    assert (
        SurveyTemplate.create_new(
            name="n", questions=[Question.create_free_text("q")]
        ).images
        == []
    )
