"""v0 のアイテムを v1 の形へ変換する純粋関数のテスト（移行スクリプト）。

v0（v0.19.1）の DynamoDB アイテムの形をフィクスチャにし、v1 のモデルで読める形に
なること、S3 の参照が v1 のバケットへ書き換わること、移せないものが理由付きで
スキップされることを確かめる。
"""

from decimal import Decimal

import pytest

from scripts.v0_migration import convert
from src.models.custom_persona_data import custom_file_key
from src.models.errors import ErrorCode
from src.models.persona_binding import BindingTargetType
from src.models.uploaded_file import file_id_from_key

SRC = "aipersonadev-uploads-111111111111-us-east-1"
DST = "aipersonadevdata-uploadbucket-abc"
OWNER = "sub-owner"
NOW = 1_800_000_000
FILE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _persona(**extra):
    item = {
        "id": "p1",
        "name": "山田",
        "age": Decimal("35"),
        "occupation": "会社員",
        "background": "背景",
        "values": ["a"],
        "pain_points": ["b"],
        "goals": ["c"],
        "created_at": "2026-01-01T09:00:00",
        "updated_at": "2026-01-02T09:00:00.123456",
        "type": "persona",
    }
    item.update(extra)
    return item


class TestPlain:
    def test_decimal_and_sets(self):
        value = {
            "a": Decimal("1"),
            "b": Decimal("0.5"),
            "c": {"y", "x"},
            "d": [Decimal("2")],
        }
        assert convert.plain(value) == {"a": 1, "b": 0.5, "c": ["x", "y"], "d": [2]}


class TestRebucket:
    def test_source_bucket_path(self):
        moved = convert.rebucket(f"s3://{SRC}/datasets/a.csv", SRC, DST)
        assert moved == convert.Moved(
            path=f"s3://{DST}/datasets/a.csv",
            src_key="datasets/a.csv",
            dst_key="datasets/a.csv",
        )

    @pytest.mark.parametrize(
        "path",
        [
            "local://datasets/a.csv",
            "/tmp/a.png",
            "s3://other/a",
            "",
            None,
            f"s3://{SRC}/",
        ],
    )
    def test_other_paths(self, path):
        assert convert.rebucket(path, SRC, DST) is None


class TestPersona:
    def test_minimal(self):
        plan = convert.persona(
            _persona(), owner_sub=OWNER, src_bucket=SRC, dst_bucket=DST
        )
        assert plan.persona.owner_sub == OWNER
        assert plan.persona.age == 35
        assert plan.persona.shared_group_ids == frozenset()
        assert plan.copies == []
        assert plan.warnings == []

    def test_avatar_moves_to_the_target_bucket(self):
        key = "persona_avatars/p1/x.webp"
        plan = convert.persona(
            _persona(avatar_path=f"s3://{SRC}/{key}"),
            owner_sub=OWNER,
            src_bucket=SRC,
            dst_bucket=DST,
        )
        assert plan.persona.avatar_path == f"s3://{DST}/{key}"
        assert plan.copies == [convert.Copy(key, key)]

    def test_local_avatar_is_dropped_with_a_warning(self):
        plan = convert.persona(
            _persona(avatar_path="/data/avatars/p1.webp"),
            owner_sub=OWNER,
            src_bucket=SRC,
            dst_bucket=DST,
        )
        assert plan.persona.avatar_path is None
        assert plan.warnings and "avatar" in plan.warnings[0]

    def test_unknown_columns_and_nulls_are_ignored(self):
        plan = convert.persona(
            _persona(gender=None, future_column="x", generation_log=[{"step": "s"}]),
            owner_sub=OWNER,
            src_bucket=SRC,
            dst_bucket=DST,
        )
        assert plan.persona.gender is None
        assert plan.persona.generation_log == [{"step": "s"}]


def _message(n, **extra):
    data = {
        "persona_id": "p1",
        "persona_name": "山田",
        "content": f"発言{n}",
        "timestamp": f"2026-01-01T09:0{n}:00",
        "message_type": "statement",
    }
    data.update(extra)
    return data


def _discussion(**extra):
    item = {
        "id": "d1",
        "topic": "議題",
        "participants": ["p1"],
        "messages": [_message(1, round_number=Decimal("1")), _message(2)],
        "insights": [
            {
                "category": "c",
                "description": "d",
                "supporting_messages": [],
                "confidence_score": Decimal("0.8"),
            }
        ],
        "created_at": "2026-01-01T09:00:00",
        "mode": "classic",
        "type": "discussion",
        "reports": [
            {
                "id": "r1",
                "template_type": "summary",
                "custom_prompt": None,
                "content": "要約",
                "created_at": "2026-01-01T10:00:00",
            }
        ],
    }
    item.update(extra)
    return item


def _file(file_type="discussion_document", **extra):
    item = {
        "id": FILE_ID,
        "filename": f"{FILE_ID}_資料.pdf",
        "original_filename": "資料.pdf",
        "file_path": f"s3://{SRC}/discussion_documents/{FILE_ID}_資料.pdf",
        "uploaded_at": "2026-01-01T08:00:00",
        "file_type": file_type,
        "type": "file",
        "file_size": Decimal("10"),
        "file_hash": "h",
        "mime_type": "application/pdf",
    }
    item.update(extra)
    return item


class TestDiscussion:
    def test_messages_and_reports_are_split_out(self):
        plan = convert.discussion(
            _discussion(),
            owner_sub=OWNER,
            files={},
            src_bucket=SRC,
            dst_bucket=DST,
            now_epoch=NOW,
        )
        body = plan.discussion
        assert body.owner_sub == OWNER
        assert body.message_count == 2
        assert body.report_count == 1
        assert body.participant_names == {"p1": "山田"}
        assert [m.seq for m in plan.messages] == [1, 2]
        assert plan.messages[0].round_number == 1
        assert plan.messages[0].discussion_id == "d1"
        assert [r.report_id for r in plan.reports] == ["r1"]
        assert plan.reports[0].discussion_id == "d1"
        assert body.insights[0].confidence_score == pytest.approx(0.8)
        assert "messages" not in body.to_dict()

    def test_empty_discussion(self):
        plan = convert.discussion(
            _discussion(messages=[], reports=[], insights=[]),
            owner_sub=OWNER,
            files={},
            src_bucket=SRC,
            dst_bucket=DST,
            now_epoch=NOW,
        )
        assert plan.discussion.message_count == 0
        assert plan.messages == []
        assert plan.reports == []

    def test_attached_document_is_rekeyed_under_the_owner(self):
        doc = {
            "id": FILE_ID,
            "filename": "資料.pdf",
            "file_path": "ignored",
            "file_size": Decimal("10"),
            "mime_type": "application/pdf",
            "uploaded_at": "2026-01-01T08:00:00",
        }
        plan = convert.discussion(
            _discussion(documents=[doc]),
            owner_sub=OWNER,
            files={FILE_ID: _file()},
            src_bucket=SRC,
            dst_bucket=DST,
            now_epoch=NOW,
        )
        [attached] = plan.files
        uploaded = attached.uploaded
        assert uploaded.owner_sub == OWNER
        assert uploaded.parent_type == "discussion"
        assert uploaded.parent_id == "d1"
        assert uploaded.expires_at is None
        assert uploaded.attached_at == NOW
        assert (
            uploaded.s3_key == f"uploads/{OWNER}/discussion_document/{FILE_ID}_資料.pdf"
        )
        assert file_id_from_key(uploaded.s3_key) == FILE_ID
        assert uploaded.file_path == f"s3://{DST}/{uploaded.s3_key}"
        assert attached.copy == convert.Copy(
            f"discussion_documents/{FILE_ID}_資料.pdf", uploaded.s3_key
        )
        assert plan.discussion.documents[0].id == FILE_ID

    def test_document_without_a_stored_file_keeps_only_the_reference(self):
        doc = {
            "filename": "a.pdf",
            "mime_type": "application/pdf",
            "file_size": 1,
            "uploaded_at": "x",
        }
        plan = convert.discussion(
            _discussion(mode="interview", documents=[doc]),
            owner_sub=OWNER,
            files={},
            src_bucket=SRC,
            dst_bucket=DST,
            now_epoch=NOW,
        )
        assert plan.files == []
        assert plan.discussion.documents[0].filename == "a.pdf"


class TestTemplate:
    def _template(self, images):
        return {
            "id": "t1",
            "name": "テンプレート",
            "created_at": "2026-01-01T09:00:00",
            "updated_at": "2026-01-01T09:00:00",
            "questions": [
                {
                    "id": "q1",
                    "text": "質問",
                    "question_type": "multiple_choice",
                    "options": ["a", "b"],
                    "scale_min": Decimal("1"),
                    "scale_max": Decimal("5"),
                }
            ],
            "images": images,
        }

    def test_image_is_rekeyed(self):
        path = f"s3://{SRC}/survey_images/{FILE_ID}_画像.png"
        image = {
            "id": "i1",
            "name": "画像",
            "file_path": path,
            "mime_type": "image/png",
            "original_filename": "画像.png",
        }
        record = _file("survey_image", file_path=path, original_filename="画像.png")
        plan = convert.template(
            self._template([image]),
            owner_sub=OWNER,
            files_by_path={path: record},
            src_bucket=SRC,
            dst_bucket=DST,
            now_epoch=NOW,
        )
        [moved] = plan.files
        assert moved.uploaded.file_type == "survey_image"
        assert moved.uploaded.parent_type == "survey_template"
        assert moved.uploaded.parent_id == "t1"
        assert plan.template.images[0].file_path == moved.uploaded.file_path
        assert (
            moved.uploaded.s3_key == f"uploads/{OWNER}/survey_image/{FILE_ID}_画像.png"
        )
        assert plan.template.owner_sub == OWNER
        assert plan.template.questions[0].scale_max == 5

    def test_image_without_a_record_gets_a_stable_id(self):
        path = f"s3://{SRC}/survey_images/no-uuid.png"
        image = {
            "id": "i1",
            "name": "画像",
            "file_path": path,
            "mime_type": "image/png",
            "original_filename": "no-uuid.png",
        }
        kwargs = dict(
            owner_sub=OWNER,
            files_by_path={},
            src_bucket=SRC,
            dst_bucket=DST,
            now_epoch=NOW,
        )
        first = convert.template(self._template([image]), **kwargs)
        second = convert.template(self._template([image]), **kwargs)
        assert first.files[0].uploaded.id == second.files[0].uploaded.id
        assert (
            file_id_from_key(first.files[0].uploaded.s3_key)
            == first.files[0].uploaded.id
        )

    def test_local_image_is_dropped_with_a_warning(self):
        image = {
            "id": "i1",
            "name": "画像",
            "file_path": "/tmp/x.png",
            "mime_type": "image/png",
            "original_filename": "x.png",
        }
        plan = convert.template(
            self._template([image]),
            owner_sub=OWNER,
            files_by_path={},
            src_bucket=SRC,
            dst_bucket=DST,
            now_epoch=NOW,
        )
        assert plan.template.images == []
        assert plan.files == []
        assert plan.warnings


def _survey(**extra):
    item = {
        "id": "s1",
        "name": "調査",
        "description": "説明",
        "template_id": "t1",
        "persona_count": Decimal("10"),
        "status": "completed",
        "s3_result_path": f"s3://{SRC}/survey-results/s1/results.csv",
        "created_at": "2026-01-01T09:00:00",
        "updated_at": "2026-01-01T09:30:00",
        "datasource": "nemotron",
    }
    item.update(extra)
    return item


class TestSurvey:
    def test_completed(self):
        plan = convert.survey(
            _survey(), owner_sub=OWNER, custom_ids={}, src_bucket=SRC, dst_bucket=DST
        )
        assert plan.survey.owner_sub == OWNER
        assert plan.survey.persona_count == 10
        assert plan.survey.s3_result_path == f"s3://{DST}/survey-results/s1/results.csv"
        assert plan.copies == [
            convert.Copy(
                "survey-results/s1/results.csv", "survey-results/s1/results.csv"
            )
        ]
        assert plan.survey.run_queue is None

    @pytest.mark.parametrize("status", ["pending", "running", "queued"])
    def test_unfinished_survey_becomes_an_error(self, status):
        plan = convert.survey(
            _survey(status=status, s3_result_path=None),
            owner_sub=OWNER,
            custom_ids={},
            src_bucket=SRC,
            dst_bucket=DST,
        )
        assert plan.survey.status == "error"
        assert plan.survey.error_code == ErrorCode.SURVEY_EXECUTION_INTERRUPTED.value
        assert plan.warnings

    def test_custom_datasource_points_to_the_new_record(self):
        plan = convert.survey(
            _survey(datasource="custom:営業"),
            owner_sub=OWNER,
            custom_ids={"営業": "cid"},
            src_bucket=SRC,
            dst_bucket=DST,
        )
        assert plan.survey.datasource == "custom:cid"
        assert plan.survey.datasource_name == "営業"

    def test_unmigrated_custom_datasource_keeps_the_name(self):
        plan = convert.survey(
            _survey(datasource="custom:営業"),
            owner_sub=OWNER,
            custom_ids={},
            src_bucket=SRC,
            dst_bucket=DST,
        )
        assert plan.survey.datasource_name == "営業"
        assert plan.survey.datasource_label == "営業"
        assert plan.warnings


class TestAssets:
    def test_dataset(self):
        item = {
            "id": "ds1",
            "name": "購買",
            "description": "",
            "s3_path": f"s3://{SRC}/datasets/ds1.csv",
            "columns": [{"name": "c", "data_type": "string", "description": ""}],
            "row_count": Decimal("3"),
            "created_at": "2026-01-01T09:00:00",
            "updated_at": "2026-01-01T09:00:00",
            "notes": "",
        }
        plan = convert.dataset(item, src_bucket=SRC, dst_bucket=DST)
        assert plan is not None
        assert plan.dataset.s3_path == f"s3://{DST}/datasets/ds1.csv"
        assert plan.dataset.row_count == 3
        assert plan.copies == [convert.Copy("datasets/ds1.csv", "datasets/ds1.csv")]

    def test_local_dataset_is_skipped(self):
        item = {
            "id": "ds1",
            "name": "購買",
            "description": "",
            "s3_path": "local://datasets/ds1.csv",
            "columns": [],
            "created_at": "2026-01-01T09:00:00",
            "updated_at": "2026-01-01T09:00:00",
        }
        assert convert.dataset(item, src_bucket=SRC, dst_bucket=DST) is None

    def test_knowledge_base(self):
        kb = convert.knowledge_base(
            {
                "id": "k1",
                "knowledge_base_id": "KB123",
                "name": "社内",
                "description": "",
                "created_at": "2026-01-01T09:00:00",
                "updated_at": "2026-01-01T09:00:00",
            }
        )
        assert kb.knowledge_base_id == "KB123"

    def test_bindings(self):
        ds = convert.dataset_binding(
            {
                "id": "b1",
                "persona_id": "p1",
                "dataset_id": "ds1",
                "binding_keys": {"customer_id": "C1"},
                "created_at": "2026-01-01T09:00:00",
            }
        )
        assert ds.target_type is BindingTargetType.DATASET
        assert ds.target_key == "DATASET#ds1"
        assert ds.binding_keys == {"customer_id": "C1"}
        kb = convert.kb_binding(
            {
                "id": "b2",
                "persona_id": "p1",
                "kb_id": "k1",
                "metadata_filters": {"dept": "sales"},
                "created_at": "2026-01-01T09:00:00",
            }
        )
        assert kb.target_key == "KB"
        assert kb.metadata_filters == {"dept": "sales"}


class TestCustomPersonaData:
    def test_stable_id_and_key(self):
        first = convert.custom_persona_data(
            "営業",
            meta={
                "standard_mapping": {"age": "年齢"},
                "extra_columns": [{"name": "x", "description": "d"}],
            },
            columns=["age", "x"],
            row_count=100,
            registered_by=OWNER,
            registered_at="2026-01-01T09:00:00",
        )
        second = convert.custom_persona_data(
            "営業",
            meta=None,
            columns=[],
            row_count=0,
            registered_by=OWNER,
            registered_at="2026-01-01T09:00:00",
        )
        assert first.id == second.id
        assert first.s3_key == custom_file_key(first.id)
        assert first.standard_mapping == {"age": "年齢"}
        assert first.extra_columns[0].csv_column == "x"
        assert first.shared_group_ids == frozenset()

    def test_custom_name_from_key(self):
        assert (
            convert.custom_name_from_key("persona-dataset/custom/営業.parquet")
            == "営業"
        )
        assert (
            convert.custom_name_from_key("persona-dataset/custom/営業.meta.json")
            is None
        )
        assert (
            convert.custom_name_from_key("persona-dataset/custom/sub/a.parquet") is None
        )
