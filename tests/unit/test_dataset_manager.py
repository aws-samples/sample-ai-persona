"""
DatasetManager単体テスト

U7: BR3.1、BR3.3、BR5.1、BR5.2、BR5.6、NFR1.72、NFR1.77、NFR8.23、NFR8.24、AC5.2.1、
AC5.2.3、AC5.3.1、AC5.3.3。設定の全メソッドは settings:manage（なければ 404）。
DynamoDB と S3 は moto（tests/unit/survey_world.py）。
"""

import pytest
from unittest.mock import Mock
from datetime import datetime
from dataclasses import asdict

from src.models.dataset import Dataset, DatasetColumn
from src.models.errors import CodedError, ErrorCode
from src.models.sharing import AssetScopeChange
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.survey_world import ADMIN, MEMBER_A, NO_ROLE, SurveyWorld


class TestDatasetModels:
    """データセットモデルテスト"""

    def test_dataset_column_creation(self):
        """DatasetColumn作成テスト"""
        column = DatasetColumn(
            name="user_id", data_type="string", description="ユーザーID"
        )

        assert column.name == "user_id"
        assert column.data_type == "string"
        assert column.description == "ユーザーID"

    def test_dataset_column_asdict(self):
        """DatasetColumn辞書変換テスト"""
        column = DatasetColumn(name="age", data_type="integer", description="年齢")

        data = asdict(column)

        assert data["name"] == "age"
        assert data["data_type"] == "integer"
        assert data["description"] == "年齢"

    def test_dataset_creation(self):
        """Dataset作成テスト"""
        now = datetime.now()
        dataset = Dataset(
            id="ds-001",
            name="テストデータ",
            description="テスト用データセット",
            s3_path="s3://bucket/test.csv",
            columns=[DatasetColumn(name="id", data_type="integer", description="ID")],
            row_count=100,
            created_at=now,
            updated_at=now,
        )

        assert dataset.id == "ds-001"
        assert dataset.name == "テストデータ"
        assert len(dataset.columns) == 1
        assert dataset.row_count == 100

    def test_dataset_create_new(self):
        """Dataset.create_newテスト"""
        dataset = Dataset.create_new(
            name="新規データセット",
            description="説明",
            s3_path="s3://bucket/new.csv",
            columns=[DatasetColumn(name="col1", data_type="string")],
            row_count=50,
        )

        assert dataset.id is not None
        assert dataset.name == "新規データセット"
        assert dataset.created_at is not None
        assert dataset.updated_at is not None

    def test_dataset_to_dict(self):
        """Dataset辞書変換テスト"""
        now = datetime.now()
        dataset = Dataset(
            id="ds-002",
            name="購買データ",
            description="購買履歴",
            s3_path="s3://bucket/purchase.csv",
            columns=[
                DatasetColumn(
                    name="user_id", data_type="string", description="ユーザーID"
                )
            ],
            row_count=50,
            created_at=now,
            updated_at=now,
        )

        data = dataset.to_dict()

        assert data["id"] == "ds-002"
        assert data["name"] == "購買データ"
        assert len(data["columns"]) == 1
        assert data["row_count"] == 50

    def test_dataset_from_dict(self):
        """Dataset辞書からの復元テスト"""
        data = {
            "id": "ds-003",
            "name": "行動データ",
            "description": "ユーザー行動ログ",
            "s3_path": "s3://bucket/behavior.csv",
            "columns": [
                {
                    "name": "timestamp",
                    "data_type": "string",
                    "description": "タイムスタンプ",
                }
            ],
            "row_count": 1000,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        }

        dataset = Dataset.from_dict(data)

        assert dataset.id == "ds-003"
        assert dataset.name == "行動データ"
        assert len(dataset.columns) == 1
        assert dataset.row_count == 1000


# ペルソナとデータセットの紐付けのモデル（PersonaBinding）は
# tests/unit/test_persona_u5_models.py が確かめる（U5）。


class TestInterviewManagerDataset:
    """InterviewManagerデータセット連携テスト"""

    """U6: セッションはアプリの外に置き、発言は別のテーブルに書く（設定は保たれる）。"""

    @staticmethod
    def _session(**kwargs):
        from datetime import timezone

        from src.models.interview_session import InterviewSession

        return InterviewSession.create_new(
            owner_sub="a",
            persona_ids=["p1", "p2"],
            persona_names={"p1": "P1", "p2": "P2"},
            memory_mode="disabled",
            now=datetime(2026, 10, 15, tzinfo=timezone.utc),
            **kwargs,
        )

    def test_interview_session_enable_dataset_field(self):
        """InterviewSessionのenable_datasetフィールドテスト"""
        assert self._session(enable_dataset=True).enable_dataset is True

    def test_interview_session_enable_dataset_default(self):
        """InterviewSessionのenable_datasetデフォルト値テスト"""
        assert self._session().enable_dataset is False

    def test_enable_dataset_survives_storage(self):
        """保存した形から読み直しても enable_dataset が保たれる"""
        from src.models.interview_session import InterviewSession

        session = self._session(enable_dataset=True)
        assert InterviewSession.from_dict(session.to_dict()).enable_dataset is True

    def test_touching_preserves_enable_dataset(self):
        """操作のたびの期限の延長で enable_dataset が変わらない"""
        from datetime import timedelta

        session = self._session(enable_dataset=True)
        later = session.touched(session.created_at + timedelta(hours=1))
        assert later.enable_dataset is True


class TestDatasetManagerSchemaAnalysis:
    """DatasetManagerスキーマ解析テスト"""

    @pytest.fixture
    def dataset_manager(self):
        """DatasetManagerインスタンス（モック使用）"""
        from src.managers.dataset_manager import DatasetManager

        return DatasetManager(
            survey_batch_service=Mock(),
            database_service=Mock(),
            s3_service=Mock(),
            departments=Mock(),
        )

    def test_analyze_schema_basic(self, dataset_manager):
        """基本的なスキーマ解析テスト"""
        csv_content = b"id,name,age\n1,Alice,30\n2,Bob,25"

        columns, row_count = dataset_manager.analyze_schema(ADMIN, csv_content)

        assert len(columns) == 3
        assert columns[0].name == "id"
        assert columns[1].name == "name"
        assert columns[2].name == "age"
        assert row_count == 2

    def test_analyze_schema_type_inference(self, dataset_manager):
        """型推論テスト"""
        csv_content = b"int_col,float_col,str_col\n1,1.5,hello\n2,2.5,world"

        columns, _ = dataset_manager.analyze_schema(ADMIN, csv_content)

        assert columns[0].data_type == "integer"
        assert columns[1].data_type == "float"
        assert columns[2].data_type == "string"

    def test_analyze_schema_date_inference(self, dataset_manager):
        """日付型推論テスト"""
        csv_content = b"date_col,value\n2024-01-01,100\n2024-02-01,200"

        columns, _ = dataset_manager.analyze_schema(ADMIN, csv_content)

        assert columns[0].data_type == "date"
        assert columns[1].data_type == "integer"

    def test_analyze_schema_empty_file(self, dataset_manager):
        """空ファイルのスキーマ解析テスト"""
        csv_content = b""

        columns, row_count = dataset_manager.analyze_schema(ADMIN, csv_content)

        assert columns == []
        assert row_count == 0

    def test_analyze_schema_many_rows(self, dataset_manager):
        """sample_rows以上の行数カウントテスト"""
        header = "id,name\n"
        rows = "".join(f"{i},name{i}\n" for i in range(150))
        csv_content = (header + rows).encode("utf-8")

        columns, row_count = dataset_manager.analyze_schema(
            ADMIN, csv_content, sample_rows=50
        )

        assert len(columns) == 2
        assert row_count == 149

    def test_infer_type_empty_values(self, dataset_manager):
        """空値のみの場合はstring"""
        result = dataset_manager._infer_type(["", "  ", ""])
        assert result == "string"

    def test_analyze_schema_rejects_empty_header(self, dataset_manager):
        """空ヘッダー列は FILE_CSV_HEADER_INVALID で弾く（DuckDBが列名を全リネームする）。"""
        from src.managers.dataset_manager import DatasetManagerError

        csv_content = b"user_id,,category\nU001,100,food\n"
        with raises_code(DatasetManagerError, ErrorCode.FILE_CSV_HEADER_INVALID):
            dataset_manager.analyze_schema(ADMIN, csv_content)

    def test_analyze_schema_rejects_duplicate_header(self, dataset_manager):
        """重複ヘッダーは FILE_CSV_HEADER_INVALID で弾く（DuckDBが name_1 にリネーム）。"""
        from src.managers.dataset_manager import DatasetManagerError

        csv_content = b"user_id,amount,amount\nU001,100,10\n"
        with raises_code(DatasetManagerError, ErrorCode.FILE_CSV_HEADER_INVALID):
            dataset_manager.analyze_schema(ADMIN, csv_content)

    def test_analyze_schema_strips_whitespace_headers(self, dataset_manager):
        """前後空白ヘッダーは正規化して受け入れる（DuckDBのtrimと揃える）。"""
        csv_content = b"user id, amount ,category\nU001,100,food\n"
        columns, _ = dataset_manager.analyze_schema(ADMIN, csv_content)
        assert [c.name for c in columns] == ["user id", "amount", "category"]


# =============================================================================
# U7: 設定は admin だけ・部分更新・利用範囲
# =============================================================================


@pytest.fixture
def ds_mgr(survey_world: SurveyWorld):
    from src.managers.dataset_manager import DatasetManager

    return DatasetManager(
        survey_batch_service=Mock(),
        database_service=survey_world.db,
        s3_service=survey_world.s3,
        departments=survey_world.departments,
    )


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


class TestDatasetManagerIsAdminOnly:
    @pytest.mark.parametrize(
        "call",
        [
            lambda m: m.analyze_schema(MEMBER_A, b"a\n1"),
            lambda m: m.download_csv_from_url(MEMBER_A, "https://example.com/x.csv"),
            lambda m: m.upload_csv(MEMBER_A, b"a\n1", "a.csv", "名前"),
            lambda m: m.get_datasets(MEMBER_A),
            lambda m: m.get_dataset(MEMBER_A, "x"),
            lambda m: m.update_dataset(MEMBER_A, "x", name="y"),
            lambda m: m.delete_dataset(MEMBER_A, "x"),
            lambda m: m.scope_dialog(MEMBER_A, "x"),
            lambda m: m.update_scope(MEMBER_A, "x", AssetScopeChange()),
        ],
    )
    def test_member_gets_404_reads_and_writes_nothing(
        self, ds_mgr, survey_world, logs, call
    ):
        scan = survey_world.spy("scan")
        put = survey_world.spy("put_item")
        with raises_code(CodedError, ErrorCode.SETTINGS_NOT_FOUND):
            call(ds_mgr)
        assert scan.call_count == 0
        assert put.call_count == 0
        denied = logs.events("authz.denied")
        assert len(denied) == 1
        assert denied[0]["reason"] == "hidden"

    def test_no_role_too(self, ds_mgr):
        with raises_code(CodedError, ErrorCode.SETTINGS_NOT_FOUND):
            ds_mgr.get_datasets(NO_ROLE)


class TestDatasetManagerCRUD:
    def test_upload_registers_an_org_asset_with_an_ascii_key(
        self, ds_mgr, survey_world
    ):
        dataset = ds_mgr.upload_csv(
            ADMIN, b"id,name\n1,Alice\n2,Bob", "購買 データ.csv", "購買データ", "説明"
        )
        assert dataset.name == "購買データ"
        assert dataset.row_count == 2
        key = survey_world.s3.key_of(dataset.s3_path)
        assert key.startswith("datasets/") and "購買" not in key
        stored = ds_mgr.get_dataset(ADMIN, dataset.id)
        assert stored.shared_group_ids == frozenset()

    def test_upload_with_custom_columns(self, ds_mgr):
        columns = [DatasetColumn(name="a", data_type="string")]
        dataset = ds_mgr.upload_csv(
            ADMIN, b"a,b\n1,2", "t.csv", "テスト", columns=columns
        )
        assert dataset.columns == columns

    def test_listing_leaves_out_dedicated_and_labels_the_scope(
        self, ds_mgr, survey_world
    ):
        org = survey_world.dataset("sales", "gone")
        survey_world.dataset(owner_persona_id="p1")
        listing = ds_mgr.get_datasets(ADMIN)
        assert [d.id for d in listing.items] == [org.id]
        assert listing.scope_labels[org.id] == ["営業部", "（削除されたグループ）"]

    def test_dedicated_dataset_is_not_a_settings_target(self, ds_mgr, survey_world):
        dedicated = survey_world.dataset(owner_persona_id="p1")
        assert ds_mgr.get_dataset(ADMIN, dedicated.id) is None
        assert ds_mgr.update_dataset(ADMIN, dedicated.id, name="x") is None
        assert ds_mgr.delete_dataset(ADMIN, dedicated.id) is False
        with raises_code(CodedError, ErrorCode.DATASET_NOT_FOUND):
            ds_mgr.update_scope(
                ADMIN, dedicated.id, AssetScopeChange(add=frozenset({"sales"}))
            )
        with raises_code(CodedError, ErrorCode.DATASET_NOT_FOUND):
            ds_mgr.scope_dialog(ADMIN, dedicated.id)

    def test_update_is_partial_and_keeps_the_scope(self, ds_mgr, survey_world):
        dataset = survey_world.dataset("sales")
        put = survey_world.spy("put_item")
        updated = ds_mgr.update_dataset(
            ADMIN, dataset.id, name="新名", description="新説明", notes="メモ"
        )
        assert updated.name == "新名"
        assert put.call_count == 0
        stored = ds_mgr.get_dataset(ADMIN, dataset.id)
        assert stored.notes == "メモ"
        assert stored.shared_group_ids == frozenset({"sales"})

    def test_update_of_a_missing_dataset(self, ds_mgr, survey_world):
        assert ds_mgr.update_dataset(ADMIN, "missing", name="x") is None
        assert survey_world.raw(survey_world.db.datasets_table, "missing") is None

    def test_scope_change_then_rename_both_stay(self, ds_mgr, survey_world):
        dataset = survey_world.dataset()
        ds_mgr.update_scope(
            ADMIN, dataset.id, AssetScopeChange(add=frozenset({"plan"}))
        )
        ds_mgr.update_dataset(ADMIN, dataset.id, name="改名")
        stored = ds_mgr.get_dataset(ADMIN, dataset.id)
        assert stored.name == "改名"
        assert stored.shared_group_ids == frozenset({"plan"})

    def test_delete_removes_the_file_and_the_record(self, ds_mgr, survey_world):
        dataset = ds_mgr.upload_csv(ADMIN, b"a\n1", "a.csv", "消す")
        assert ds_mgr.delete_dataset(ADMIN, dataset.id) is True
        assert ds_mgr.get_dataset(ADMIN, dataset.id) is None
        assert not survey_world.s3.exists(dataset.s3_path)

    def test_delete_continues_when_the_file_cannot_be_removed(
        self, ds_mgr, survey_world, monkeypatch
    ):
        dataset = ds_mgr.upload_csv(ADMIN, b"a\n1", "a.csv", "消す")
        monkeypatch.setattr(
            survey_world.s3, "delete_file", Mock(side_effect=RuntimeError("down"))
        )
        assert ds_mgr.delete_dataset(ADMIN, dataset.id) is True
        assert ds_mgr.get_dataset(ADMIN, dataset.id) is None

    def test_delete_missing(self, ds_mgr):
        assert ds_mgr.delete_dataset(ADMIN, "missing") is False


class TestDatasetScope:
    def test_dialog(self, ds_mgr, survey_world):
        dataset = survey_world.dataset("sales")
        dialog = ds_mgr.scope_dialog(ADMIN, dataset.id)
        assert dialog.selected == frozenset({"sales"})
        assert {d.group_id for d in dialog.choices} == {"sales", "plan"}

    def test_add_and_remove_are_logged(self, ds_mgr, survey_world, logs):
        dataset = survey_world.dataset("sales")
        updated = ds_mgr.update_scope(
            ADMIN,
            dataset.id,
            AssetScopeChange(add=frozenset({"plan"}), remove=frozenset({"sales"})),
        )
        assert updated.shared_group_ids == frozenset({"plan"})
        event = logs.events("asset.scope_changed")[0]
        assert event["resource_type"] == "dataset"
        assert event["added"] == ["plan"]
        assert event["removed"] == ["sales"]
        assert "売上データ" not in str(event)

    def test_only_existing_departments_can_be_added(self, ds_mgr, survey_world):
        dataset = survey_world.dataset()
        with raises_code(CodedError, ErrorCode.SHARE_SCOPE_INVALID):
            ds_mgr.update_scope(
                ADMIN, dataset.id, AssetScopeChange(add=frozenset({"x"}))
            )

    def test_missing_dataset(self, ds_mgr, survey_world):
        with raises_code(CodedError, ErrorCode.DATASET_NOT_FOUND):
            ds_mgr.update_scope(
                ADMIN, "missing", AssetScopeChange(add=frozenset({"sales"}))
            )
        assert survey_world.raw(survey_world.db.datasets_table, "missing") is None

    def test_member_in_scope_can_then_use_it(self, ds_mgr, survey_world):
        """利用範囲の変更は次の判定から効く（AccessPolicy.can_use_asset、NFR9.36）。"""
        from src.managers.components.access_policy import AccessPolicy
        from src.models.authz import ResourceType

        dataset = survey_world.dataset()
        target = survey_world.lookup.get(ResourceType.DATASET, dataset.id)
        assert not AccessPolicy.can_use_asset(MEMBER_A, target)
        ds_mgr.update_scope(
            ADMIN, dataset.id, AssetScopeChange(add=frozenset({"sales"}))
        )
        target = survey_world.lookup.get(ResourceType.DATASET, dataset.id)
        assert AccessPolicy.can_use_asset(MEMBER_A, target)


class TestDatasetManagerBindingsMoved:
    """紐付けの 5 メソッドは PersonaManager へ移した（U5、ADR-007）。"""

    @pytest.mark.parametrize(
        "method",
        [
            "save_binding",
            "get_bindings_by_persona",
            "delete_binding",
            "set_persona_bindings",
            "preview_binding_data",
        ],
    )
    def test_binding_methods_are_gone(self, method):
        from src.managers.dataset_manager import DatasetManager

        assert not hasattr(DatasetManager, method)
