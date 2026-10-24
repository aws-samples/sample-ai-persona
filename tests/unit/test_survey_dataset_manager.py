"""SurveyDatasetManager のユニットテスト（U7）。

BR4.1〜BR4.7、BR5.1〜BR5.3、NFR1.73、NFR1.74、NFR1.76、NFR1.82、NFR1.85、NFR2.23、
NFR8.24、AC2.5.1〜AC2.5.3、AC5.2.4、AC5.2.5、AC5.3.1、AC5.3.4。DynamoDB と S3 は moto、
ResourceLookup・DepartmentDirectory・AccessPolicy は本物（tests/unit/survey_world.py）、
バッチ推論・エージェント・AI はモック。
"""

from unittest.mock import Mock

import polars as pl
import pytest

from src.managers.shared.asset_scope import (
    VANISHED_GROUP_LABEL,
    scope_dialog,
    scope_labels,
)
from src.managers.survey_dataset_manager import (
    SurveyDatasetManager,
    SurveyDatasetManagerError,
    SurveyDatasetValidationError,
)
from src.models.authz import Department, ResourceType
from src.models.custom_persona_data import ExtraColumn, reservation_id
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.sharing import AssetScopeChange
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.survey_world import (
    ADMIN,
    BUCKET,
    MEMBER_A,
    MEMBER_C,
    NO_DEPARTMENT,
    NO_ROLE,
    SurveyWorld,
)

CUSTOM = ResourceType.CUSTOM_PERSONA_DATA
ROWS = SurveyDatasetManager.MIN_SEGMENT_ROWS


@pytest.fixture
def batch() -> Mock:
    batch = Mock()
    batch.has_parquet_uri.return_value = True
    batch.get_filtered_count.return_value = 500
    batch.get_total_count.return_value = 800
    batch.get_available_filter_values.return_value = {"性別": {"values": ["男性"]}}
    batch.get_preview_stats.return_value = {"count": 500}
    batch.convert_csv_to_parquet.side_effect = lambda csv, mapping=None: (
        pl.DataFrame(
            {"persona": [f"p{i}" for i in range(ROWS)], "hobby": ["釣り"] * ROWS}
        ),
        b"parquet-bytes",
    )
    return batch


@pytest.fixture
def agent() -> Mock:
    return Mock()


@pytest.fixture
def ai() -> Mock:
    return Mock()


@pytest.fixture
def dm(survey_world: SurveyWorld, batch, agent, ai) -> SurveyDatasetManager:
    return SurveyDatasetManager(
        survey_batch_service=batch,
        agent_service=agent,
        ai_service=ai,
        s3_service=survey_world.s3,
        custom_persona_data_store=survey_world.custom_store,
        resource_lookup=survey_world.lookup,
        departments=survey_world.departments,
        sleep=lambda _s: None,
    )


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


def _objects(world: SurveyWorld, prefix: str = "persona-dataset/custom/") -> list[str]:
    listed = world.s3.s3_client.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return [o["Key"] for o in listed.get("Contents", [])]


def _upload(dm, principal=ADMIN, name="顧客データ", **kwargs):
    return dm.upload_custom_dataset(
        principal,
        b"csv",
        name,
        column_mapping={"persona": "persona"},
        extra_columns=[{"csv_column": "hobby", "description": "趣味"}],
        **kwargs,
    )


# =============================================================================
# 参照（全員、BR4.1、BR4.2、BR5.3）
# =============================================================================


class TestReferenceForEveryone:
    def test_nemotron_status_needs_no_permission(self, dm, survey_world):
        assert dm.check_nemotron_status() == {"exists": False, "size_mb": 0}
        survey_world.s3.upload_file(
            b"x" * 1024, "persona-dataset/nemotron-personas-japan.parquet"
        )
        assert dm.check_nemotron_status()["exists"] is True

    def test_member_lists_only_data_in_their_scope(self, dm, survey_world):
        sales = survey_world.custom_data("営業向け", "sales")
        survey_world.custom_data("企画向け", "plan")
        survey_world.custom_data("admin だけ")
        listing = dm.list_persona_data(MEMBER_A)
        assert [d.id for d in listing.custom] == [sales.id]
        assert listing.manageable is False

    def test_admin_lists_everything_with_scope_labels(self, dm, survey_world):
        sales = survey_world.custom_data("営業向け", "sales", "gone")
        only_admin = survey_world.custom_data("admin だけ")
        listing = dm.list_persona_data(ADMIN)
        assert {d.id for d in listing.custom} == {sales.id, only_admin.id}
        assert listing.manageable is True
        assert listing.scope_labels[sales.id] == ["営業部", VANISHED_GROUP_LABEL]
        assert listing.scope_labels[only_admin.id] == []

    def test_reservations_never_appear(self, dm, survey_world):
        survey_world.custom_data("営業向け", "sales")
        listing = dm.list_persona_data(ADMIN)
        assert all(not d.id.startswith("name#") for d in listing.custom)

    def test_member_without_department_sees_no_custom_data(self, dm, survey_world):
        survey_world.custom_data("営業向け", "sales")
        assert dm.list_persona_data(NO_DEPARTMENT).custom == ()

    def test_unreadable_table_is_not_an_empty_list(self, dm, survey_world):
        survey_world.custom_store.list_all = Mock(
            side_effect=__import__(
                "src.services.custom_persona_data_store",
                fromlist=["CustomPersonaDataStoreError"],
            ).CustomPersonaDataStoreError("down")
        )
        with raises_code(CodedError, ErrorCode.LOOKUP_UNAVAILABLE):
            dm.list_persona_data(MEMBER_A)

    def test_scope_change_shows_up_at_once(self, dm, survey_world):
        data = survey_world.custom_data("企画向け", "plan")
        assert dm.list_persona_data(MEMBER_A).custom == ()
        dm.update_scope(ADMIN, data.id, AssetScopeChange(add=frozenset({"sales"})))
        assert [d.id for d in dm.list_persona_data(MEMBER_A).custom] == [data.id]

    def test_detail_for_a_user_in_scope(self, dm, survey_world):
        data = survey_world.custom_data("営業向け", "sales")
        assert dm.get_custom_persona_data(MEMBER_A, data.id).name == "営業向け"

    @pytest.mark.parametrize("which", ["out_of_scope", "missing", "reservation"])
    def test_detail_outside_the_scope_is_not_found(self, dm, survey_world, which):
        data = survey_world.custom_data("企画向け", "plan")
        target = {
            "out_of_scope": data.id,
            "missing": "2b0f8d2e-0000-4000-8000-000000000000",
            "reservation": reservation_id("企画向け"),
        }[which]
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            dm.get_custom_persona_data(MEMBER_A, target)

    def test_filters_and_counts_judge_the_datasource(self, dm, survey_world, batch):
        data = survey_world.custom_data("企画向け", "plan")
        source = f"custom:{data.id}"
        for call in (
            lambda: dm.get_available_filter_values(MEMBER_A, source),
            lambda: dm.get_filtered_count(MEMBER_A, None, source),
            lambda: dm.get_preview_stats(MEMBER_A, None, source),
            lambda: dm.get_datasource_count(MEMBER_A, source),
        ):
            with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
                call()
        batch.get_filtered_count.assert_not_called()
        batch.get_available_filter_values.assert_not_called()
        assert dm.get_filtered_count(MEMBER_C, None, source) == 500
        assert dm.get_datasource_count(MEMBER_C, source) == 800

    def test_nemotron_needs_no_scope(self, dm):
        assert dm.get_filtered_count(MEMBER_A, None, "nemotron") == 500
        assert dm.get_preview_stats(MEMBER_A, None, "nemotron") == {"count": 500}

    def test_old_name_datasource_is_not_found(self, dm, batch):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            dm.get_filtered_count(MEMBER_A, None, "custom:顧客データ")
        batch.get_filtered_count.assert_not_called()

    def test_standard_columns(self, dm):
        assert "persona" in dm.get_standard_columns()

    def test_image_signing_by_path_is_gone(self):
        assert not hasattr(SurveyDatasetManager, "get_image_presigned_url")
        assert not hasattr(SurveyDatasetManager, "list_custom_datasets")
        assert not hasattr(SurveyDatasetManager, "load_dataset_metadata")


# =============================================================================
# 管理（admin だけ、BR4.1、NFR1.73）
# =============================================================================


class TestManagementIsAdminOnly:
    @pytest.mark.parametrize(
        "call",
        [
            lambda dm: dm.download_nemotron_dataset(MEMBER_A),
            lambda dm: _upload(dm, MEMBER_A),
            lambda dm: dm.delete_custom_dataset(MEMBER_A, "x"),
            lambda dm: dm.rename_custom_dataset(MEMBER_A, "x", "y"),
            lambda dm: dm.update_scope(MEMBER_A, "x", AssetScopeChange()),
            lambda dm: dm.scope_dialog(MEMBER_A, "x"),
            lambda dm: dm.extract_segment_from_dwh(MEMBER_A, "条件", Mock()),
            lambda dm: dm.suggest_column_mapping(MEMBER_A, ["a"], {}),
            lambda dm: dm.upload_temp_file(MEMBER_A, b"x", "temp/x.csv"),
            lambda dm: dm.download_temp_file(MEMBER_A, "temp/x.csv"),
            lambda dm: dm.delete_temp_file(MEMBER_A, "temp/x.csv"),
            lambda dm: dm.parse_csv_columns(MEMBER_A, b"a,b\n1,2"),
            lambda dm: dm.preview_system_prompt(MEMBER_A, b"a\n1", {"persona": "a"}),
            lambda dm: dm.ensure_manageable(MEMBER_A),
            lambda dm: dm.import_scope_choices(MEMBER_A),
        ],
    )
    def test_member_gets_404_and_nothing_changes(
        self, dm, survey_world, batch, logs, call
    ):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            call(dm)
        assert _objects(survey_world, "") == []
        batch.convert_csv_to_parquet.assert_not_called()
        denied = logs.events("authz.denied")
        assert denied and denied[-1]["reason"] == "hidden"

    def test_no_role_is_refused_too(self, dm):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            dm.download_nemotron_dataset(NO_ROLE)

    def test_admin_passes_the_management_check(self, dm):
        assert dm.ensure_manageable(ADMIN) is None

    def test_import_offers_every_department(self, dm):
        """取り込みの利用範囲の欄（admin は全部署から選ぶ、BR5.2）"""
        names = {d.display_name for d in dm.import_scope_choices(ADMIN)}
        assert names == {"営業部", "企画部"}

    def test_admin_temp_files_roundtrip(self, dm):
        dm.upload_temp_file(ADMIN, b"a,b\n", "temp/x.csv")
        assert dm.download_temp_file(ADMIN, "temp/x.csv") == b"a,b\n"
        dm.delete_temp_file(ADMIN, "temp/x.csv")

    def test_admin_parses_columns_and_previews_the_prompt(self, dm):
        assert "columns" in dm.parse_csv_columns(ADMIN, b"name,age\nA,30\nB,25")
        prompt = dm.preview_system_prompt(
            ADMIN, b"persona\nhello", {"persona": "persona"}
        )
        assert "hello" in prompt

    def test_admin_downloads_nemotron(self, dm, survey_world, batch):
        batch.download_nemotron_dataset.return_value = b"parquet"
        result = dm.download_nemotron_dataset(ADMIN)
        assert result["exists"] is True
        batch.set_parquet_s3_uri.assert_called_once()


# =============================================================================
# 取り込み（BR4.3〜BR4.5、NFR1.82、NFR1.85）
# =============================================================================


class TestUpload:
    def test_writes_the_file_under_a_new_id_then_the_record(self, dm, survey_world):
        data = _upload(dm)
        assert data.s3_key == f"persona-dataset/custom/{data.id}.parquet"
        assert _objects(survey_world) == [data.s3_key]
        stored = survey_world.custom_store.get(data.id)
        assert stored.name == "顧客データ"
        assert stored.row_count == ROWS
        assert stored.registered_by == "admin"
        assert stored.standard_mapping == {"persona": "persona"}
        assert stored.extra_columns == (
            ExtraColumn(csv_column="hobby", description="趣味"),
        )
        assert stored.columns == ("persona", "hobby")
        assert stored.shared_group_ids == frozenset()

    def test_no_meta_json_is_written(self, dm, survey_world):
        _upload(dm)
        assert not any(k.endswith(".meta.json") for k in _objects(survey_world))

    def test_scope_chosen_in_the_dialog(self, dm, survey_world):
        data = _upload(dm, shared_group_ids=frozenset({"sales"}))
        assert survey_world.custom_store.get(data.id).shared_group_ids == frozenset(
            {"sales"}
        )

    def test_unknown_department_in_the_dialog_is_refused(self, dm, survey_world):
        with raises_code(CodedError, ErrorCode.SHARE_SCOPE_INVALID):
            _upload(dm, shared_group_ids=frozenset({"no-such"}))
        assert _objects(survey_world) == []

    @pytest.mark.parametrize("name", ["", "   ", "あ" * 101])
    def test_invalid_name_writes_nothing(self, dm, survey_world, name):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NAME_INVALID):
            _upload(dm, name=name)
        assert _objects(survey_world) == []

    def test_taken_name_removes_the_file(self, dm, survey_world):
        _upload(dm, name="同じ名前")
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NAME_TAKEN):
            _upload(dm, name="同じ名前")
        assert len(_objects(survey_world)) == 1

    def test_the_name_does_not_shape_the_key(self, dm, survey_world):
        data = _upload(dm, name="../../etc/passwd")
        assert ".." not in data.s3_key
        assert data.name == "../../etc/passwd"

    def test_dwh_import_finds_a_free_name(self, dm, survey_world):
        _upload(dm, name="20代女性")
        _upload(dm, name="20代女性 (2)")
        data = _upload(dm, name="20代女性", avoid_name_conflict=True)
        assert data.name == "20代女性 (3)"

    def test_dwh_import_gives_up_after_ten_tries(self, dm, survey_world):
        _upload(dm, name="同じ")
        for n in range(2, 11):
            _upload(dm, name=f"同じ ({n})")
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NAME_TAKEN):
            _upload(dm, name="同じ", avoid_name_conflict=True)

    def test_too_few_rows_writes_nothing(self, dm, survey_world, batch):
        batch.convert_csv_to_parquet.side_effect = lambda csv, mapping=None: (
            pl.DataFrame({"persona": ["a"]}),
            b"pq",
        )
        with raises_code(
            SurveyDatasetValidationError, ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS
        ):
            _upload(dm)
        assert _objects(survey_world) == []

    def test_failed_record_write_removes_the_file(self, dm, survey_world):
        from src.services.custom_persona_data_store import CustomPersonaDataStoreError

        survey_world.custom_store.put_new = Mock(
            side_effect=CustomPersonaDataStoreError("down")
        )
        with raises_code(SurveyDatasetManagerError, ErrorCode.SURVEY_OPERATION_FAILED):
            _upload(dm)
        assert _objects(survey_world) == []

    def test_a_file_left_behind_is_logged(self, dm, survey_world, logs, monkeypatch):
        from src.services.custom_persona_data_store import CustomPersonaDataStoreError

        survey_world.custom_store.put_new = Mock(
            side_effect=CustomPersonaDataStoreError("down")
        )
        monkeypatch.setattr(
            survey_world.s3, "delete_file", Mock(side_effect=RuntimeError("down"))
        )
        with pytest.raises(SurveyDatasetManagerError):
            _upload(dm)
        events = logs.events("survey.cleanup_failed")
        assert events[0]["step"] == "custom_file"
        assert events[0]["resource_type"] == "custom_persona_data"


# =============================================================================
# 削除・名前の変更（BR4.4、BR4.6）
# =============================================================================


class TestDeleteAndRename:
    def test_delete_removes_record_reservation_and_file(self, dm, survey_world, batch):
        data = _upload(dm)
        dm.delete_custom_dataset(ADMIN, data.id)
        assert survey_world.custom_store.get(data.id) is None
        assert (
            survey_world.raw(survey_world.custom_table, reservation_id(data.name))
            is None
        )
        assert _objects(survey_world) == []
        batch.invalidate_datasource.assert_called_once_with(f"custom:{data.id}")

    def test_delete_missing_is_not_found(self, dm):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            dm.delete_custom_dataset(ADMIN, "2b0f8d2e-0000-4000-8000-000000000000")

    def test_file_delete_failure_is_logged_and_the_delete_succeeds(
        self, dm, survey_world, logs, monkeypatch
    ):
        data = _upload(dm)
        monkeypatch.setattr(
            survey_world.s3, "delete_file", Mock(side_effect=RuntimeError("down"))
        )
        dm.delete_custom_dataset(ADMIN, data.id)
        assert survey_world.custom_store.get(data.id) is None
        assert survey_world.s3.delete_file.call_count == 3
        assert logs.events("survey.cleanup_failed")[0]["step"] == "custom_file"
        assert dm.list_persona_data(ADMIN).custom == ()

    def test_results_made_from_it_stay(self, dm, survey_world):
        data = _upload(dm)
        survey = survey_world.survey("a", datasource=f"custom:{data.id}")
        dm.delete_custom_dataset(ADMIN, data.id)
        assert survey_world.survey_store.get(survey.id) is not None

    def test_rename(self, dm, survey_world):
        data = _upload(dm, name="旧")
        renamed = dm.rename_custom_dataset(ADMIN, data.id, " 新 ")
        assert renamed.name == "新"
        assert survey_world.custom_store.get(data.id).name == "新"

    def test_rename_to_a_taken_name(self, dm):
        _upload(dm, name="一")
        second = _upload(dm, name="二")
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NAME_TAKEN):
            dm.rename_custom_dataset(ADMIN, second.id, "一")

    def test_rename_invalid_or_missing(self, dm):
        data = _upload(dm)
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NAME_INVALID):
            dm.rename_custom_dataset(ADMIN, data.id, " ")
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            dm.rename_custom_dataset(ADMIN, "2b0f8d2e-0000-4000-8000-000000000000", "x")


# =============================================================================
# 利用範囲（BR5.2、NFR8.24）
# =============================================================================


class TestScope:
    def test_dialog_shows_every_department_and_the_current_scope(
        self, dm, survey_world
    ):
        data = survey_world.custom_data("営業向け", "sales", "gone")
        dialog = dm.scope_dialog(ADMIN, data.id)
        assert [d.group_id for d in dialog.choices] == ["plan", "sales"]
        assert dialog.selected == frozenset({"sales", "gone"})
        assert dialog.vanished == ("gone",)
        assert dialog.name == "営業向け"

    def test_dialog_of_a_missing_record(self, dm):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            dm.scope_dialog(ADMIN, "2b0f8d2e-0000-4000-8000-000000000000")

    def test_add_and_remove(self, dm, survey_world, logs):
        data = survey_world.custom_data("営業向け", "sales")
        updated = dm.update_scope(
            ADMIN,
            data.id,
            AssetScopeChange(add=frozenset({"plan"}), remove=frozenset({"sales"})),
        )
        assert updated.shared_group_ids == frozenset({"plan"})
        event = logs.events("asset.scope_changed")[0]
        assert event["resource_type"] == "custom_persona_data"
        assert event["resource_id"] == data.id
        assert event["added"] == ["plan"]
        assert event["removed"] == ["sales"]
        assert event["actor_sub"] == "admin"
        assert "営業向け" not in str(event)

    def test_a_deleted_group_can_be_removed(self, dm, survey_world):
        data = survey_world.custom_data("営業向け", "sales", "gone")
        updated = dm.update_scope(
            ADMIN, data.id, AssetScopeChange(remove=frozenset({"gone"}))
        )
        assert updated.shared_group_ids == frozenset({"sales"})

    def test_only_existing_departments_can_be_added(self, dm, survey_world):
        data = survey_world.custom_data("営業向け")
        with raises_code(CodedError, ErrorCode.SHARE_SCOPE_INVALID):
            dm.update_scope(ADMIN, data.id, AssetScopeChange(add=frozenset({"x"})))
        assert survey_world.custom_store.get(data.id).shared_group_ids == frozenset()

    def test_scope_of_a_missing_record_creates_nothing(self, dm, survey_world):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            dm.update_scope(
                ADMIN,
                "2b0f8d2e-0000-4000-8000-000000000000",
                AssetScopeChange(add=frozenset({"sales"})),
            )
        assert survey_world.custom_store.list_all() == []


# =============================================================================
# DWH・列の対応（admin、今の振る舞い）
# =============================================================================


class TestDwhAndMapping:
    def test_suggest_column_mapping_filters_invalid(self, dm, agent):
        agent.suggest_column_mapping_with_llm.return_value = {
            "mapping": {"persona": "desc", "bogus": "x"},
            "extra_columns": [{"csv_column": "hobby"}, {"csv_column": "nope"}],
        }
        result = dm.suggest_column_mapping(
            ADMIN, ["desc", "hobby"], {"desc": ["a"], "hobby": ["b"]}
        )
        assert result["mapping"] == {"persona": "desc"}
        assert result["extra_columns"] == [{"csv_column": "hobby"}]

    def test_suggest_column_mapping_failure_is_empty(self, dm, agent):
        agent.suggest_column_mapping_with_llm.side_effect = RuntimeError("x")
        assert dm.suggest_column_mapping(ADMIN, ["a"], {}) == {
            "mapping": {},
            "extra_columns": [],
        }

    def test_dwh_condition_is_required(self, dm):
        with raises_code(
            SurveyDatasetValidationError, ErrorCode.SEGMENT_CONDITION_REQUIRED
        ):
            dm.extract_segment_from_dwh(ADMIN, " ", Mock())

    def test_dataset_name_falls_back_to_the_condition(self, dm, ai):
        ai.invoke_model.side_effect = RuntimeError("x")
        assert dm._generate_dataset_name("20代女性で都内在住") == "20代女性で都内在住"

    def test_segment_row_count_limits(self, dm):
        with raises_code(
            SurveyDatasetValidationError, ErrorCode.SEGMENT_ROW_COUNT_TOO_LOW
        ):
            dm._validate_segment_row_count(10)
        with raises_code(
            SurveyDatasetValidationError, ErrorCode.SEGMENT_ROW_COUNT_TOO_HIGH
        ):
            dm._validate_segment_row_count(20000)
        dm._validate_segment_row_count(500)


# =============================================================================
# 利用範囲の部品（shared/asset_scope.py、判定を持たない）
# =============================================================================


class TestAssetScopeHelpers:
    DEPARTMENTS = [Department("sales", "営業部"), Department("plan", "企画部")]

    def test_change_from_form(self):
        change = AssetScopeChange.from_form(
            selected=["plan"], initial=["sales", "gone"]
        )
        assert change.add == frozenset({"plan"})
        assert change.remove == frozenset({"sales", "gone"})

    def test_kept_departments_are_not_removed(self):
        change = AssetScopeChange.from_form(selected=["sales", ""], initial=["sales"])
        assert change.add == frozenset({"sales"})
        assert change.remove == frozenset()

    def test_labels_mark_deleted_groups(self):
        assert scope_labels(["sales", "gone"], self.DEPARTMENTS) == [
            "営業部",
            VANISHED_GROUP_LABEL,
        ]
        assert VANISHED_GROUP_LABEL == "（削除されたグループ）"

    def test_dialog(self):
        dialog = scope_dialog(
            CUSTOM, "c1", "名前", frozenset({"sales", "gone"}), self.DEPARTMENTS
        )
        assert dialog.vanished == ("gone",)
        assert dialog.selected == frozenset({"sales", "gone"})
        assert [d.group_id for d in dialog.choices] == ["sales", "plan"]


def test_member_cannot_manage_even_through_an_access_denied_path(dm):
    """管理の経路は 403 ではなく 404（存在を知らせない、NFR1.79）。"""
    with pytest.raises(CodedError) as info:
        dm.delete_custom_dataset(MEMBER_A, "x")
    assert not isinstance(info.value, AccessDeniedError)


# =============================================================================
# U7 Loop-back 1: 別の実体での即時の反映（NFR9.35、NFR9.36）と数百件の規模（NFR9.37）
# =============================================================================


def _second_instance(world: SurveyWorld, batch: Mock) -> SurveyDatasetManager:
    """Another app task: its own clients, store, lookup and S3 service.

    Only the moto tables and the department copy are shared with ``dm``.
    """
    import boto3

    from src.managers.components.resource_lookup import build_resource_lookup
    from src.services.authz_lookup_service import AuthzLookupService, LookupTable
    from src.services.custom_persona_data_store import CustomPersonaDataStore
    from src.services.s3_service import S3Service
    from tests.unit.survey_world import REGION

    client = boto3.client("dynamodb", region_name=REGION)
    table = LookupTable(CUSTOM, world.custom_table, "name", hidden_id_prefix="name#")
    readers = AuthzLookupService(
        region=REGION, jobs_table_name="U7_Jobs", client=client
    )
    return SurveyDatasetManager(
        survey_batch_service=batch,
        agent_service=Mock(),
        ai_service=Mock(),
        s3_service=S3Service(bucket_name=BUCKET, region_name=REGION),
        custom_persona_data_store=CustomPersonaDataStore(
            region=REGION, table_name=world.custom_table, client=client
        ),
        resource_lookup=build_resource_lookup(
            [(CUSTOM, readers.reader(table))], sample_rate=0.0
        ),
        departments=world.departments,
        sleep=lambda _s: None,
    )


class TestScopeChangesReachAnotherInstance:
    """Decisions read DynamoDB every time; nothing is kept per task (NFR9.35/36)."""

    def test_decision_on_b_changes_right_after_a_changes_the_scope(
        self, dm, survey_world, batch
    ):
        other = _second_instance(survey_world, batch)
        data = survey_world.custom_data("顧客データ")
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            other.get_custom_persona_data(MEMBER_A, data.id)

        dm.update_scope(ADMIN, data.id, AssetScopeChange(add=frozenset({"sales"})))
        assert other.get_custom_persona_data(MEMBER_A, data.id).id == data.id

        dm.update_scope(ADMIN, data.id, AssetScopeChange(remove=frozenset({"sales"})))
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            other.get_custom_persona_data(MEMBER_A, data.id)

    def test_department_removed_on_a_leaves_the_list_on_b(
        self, dm, survey_world, batch
    ):
        other = _second_instance(survey_world, batch)
        both = survey_world.custom_data("両方", "sales", "plan")
        sales_only = survey_world.custom_data("営業だけ", "sales")
        assert {d.id for d in other.list_persona_data(MEMBER_A).custom} == {
            both.id,
            sales_only.id,
        }

        dm.update_scope(ADMIN, both.id, AssetScopeChange(remove=frozenset({"sales"})))

        assert [d.id for d in other.list_persona_data(MEMBER_A).custom] == [
            sales_only.id
        ]
        assert [d.id for d in other.list_persona_data(MEMBER_C).custom] == [both.id]


class TestHundredsOfAssets:
    """250 records (with their name reservations) are read page by page (NFR9.37)."""

    def test_member_gets_exactly_the_usable_ones_from_every_page(
        self, dm, survey_world
    ):
        from src.models.custom_persona_data import CustomPersonaData
        from tests.unit.survey_world import NOW, bulk_put_custom_data

        scopes = [{"sales"}, {"plan"}, set()]
        made = [
            CustomPersonaData.create_new(
                name=f"データ{i:03d}",
                columns=["sex", "age"],
                row_count=150,
                registered_by="admin",
                shared_group_ids=frozenset(scopes[i % 3]),
                now=NOW,
            )
            for i in range(250)
        ]
        bulk_put_custom_data(survey_world.client, survey_world.custom_table, made)
        real_scan = survey_world.client.scan
        scanned: list = []

        def paged_scan(**kwargs):
            scanned.append(kwargs["TableName"])
            return real_scan(**{**kwargs, "Limit": 7})

        survey_world.client.scan = paged_scan

        member = dm.list_persona_data(MEMBER_A).custom
        sales = {d.id for d in made if "sales" in d.shared_group_ids}
        assert len(member) == len(sales) == 84
        assert {d.id for d in member} == sales
        # 名前の押さえを含めて 500 項目を、何ページにも分けて読む
        assert len(scanned) > 500 // 7
        # member の操作で読むのは組織資産のテーブルだけ（テンプレート・結果は Scan しない）
        assert set(scanned) == {survey_world.custom_table}

        assert {d.id for d in dm.list_persona_data(ADMIN).custom} == {
            d.id for d in made
        }
