"""
Unit tests for DatasetManager.

Every settings method requires settings:manage (404 otherwise).
DynamoDB and S3 use moto (tests/unit/survey_world.py).
"""

import pytest
from unittest.mock import Mock
from datetime import datetime
from dataclasses import asdict

from src.services.stores.dataset_store import DatasetStore
from src.models.dataset import Dataset, DatasetColumn
from src.models.errors import CodedError, ErrorCode
from src.models.sharing import AssetScopeChange
from src.models.persona_binding import PersonaBinding
from src.services.stores.persona_binding_store import PersonaBindingStoreError
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.survey_world import ADMIN, MEMBER_A, NO_ROLE, SurveyWorld

NOW = datetime(2026, 10, 25, 0, 0, 0)


class TestDatasetModels:
    """Dataset model tests."""

    def test_dataset_column_creation(self):
        """DatasetColumn creation."""
        column = DatasetColumn(
            name="user_id", data_type="string", description="ユーザーID"
        )

        assert column.name == "user_id"
        assert column.data_type == "string"
        assert column.description == "ユーザーID"

    def test_dataset_column_asdict(self):
        """DatasetColumn to-dict conversion."""
        column = DatasetColumn(name="age", data_type="integer", description="年齢")

        data = asdict(column)

        assert data["name"] == "age"
        assert data["data_type"] == "integer"
        assert data["description"] == "年齢"

    def test_dataset_creation(self):
        """Dataset creation."""
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
        """Dataset.create_new."""
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
        """Dataset to-dict conversion."""
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
        """Dataset restore from dict."""
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


# The persona-to-dataset binding model (PersonaBinding) is covered by
# tests/unit/test_persona_u5_models.py.


class TestInterviewManagerDataset:
    """InterviewManager dataset integration tests."""

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
        """InterviewSession enable_dataset field."""
        assert self._session(enable_dataset=True).enable_dataset is True

    def test_interview_session_enable_dataset_default(self):
        """InterviewSession enable_dataset default value."""
        assert self._session().enable_dataset is False

    def test_enable_dataset_survives_storage(self):
        """enable_dataset is kept when a saved session is read back."""
        from src.models.interview_session import InterviewSession

        session = self._session(enable_dataset=True)
        assert InterviewSession.from_dict(session.to_dict()).enable_dataset is True

    def test_touching_preserves_enable_dataset(self):
        """Extending the expiry on each operation does not change enable_dataset."""
        from datetime import timedelta

        session = self._session(enable_dataset=True)
        later = session.touched(session.created_at + timedelta(hours=1))
        assert later.enable_dataset is True


class TestDatasetManagerSchemaAnalysis:
    """DatasetManager schema parsing tests."""

    @pytest.fixture
    def dataset_manager(self):
        """DatasetManager instance (with mocks)."""
        from src.managers.dataset_manager import DatasetManager

        return DatasetManager(
            survey_batch_service=Mock(),
            dataset_store=Mock(spec=DatasetStore),
            s3_service=Mock(),
            departments=Mock(),
        )

    def test_analyze_schema_rejects_empty_header(self, dataset_manager):
        """An empty header column is rejected with FILE_CSV_HEADER_INVALID (DuckDB renames all columns)."""
        from src.managers.dataset_manager import DatasetManagerError

        csv_content = b"user_id,,category\nU001,100,food\n"
        with raises_code(DatasetManagerError, ErrorCode.FILE_CSV_HEADER_INVALID):
            dataset_manager.analyze_schema(ADMIN, csv_content)

    def test_analyze_schema_rejects_duplicate_header(self, dataset_manager):
        """A duplicate header is rejected with FILE_CSV_HEADER_INVALID (DuckDB renames it to name_1)."""
        from src.managers.dataset_manager import DatasetManagerError

        csv_content = b"user_id,amount,amount\nU001,100,10\n"
        with raises_code(DatasetManagerError, ErrorCode.FILE_CSV_HEADER_INVALID):
            dataset_manager.analyze_schema(ADMIN, csv_content)

    def test_analyze_schema_lets_a_wrong_encoding_through(self, dataset_manager):
        """An encoding error is raised as is, not turned into a header error."""
        with pytest.raises(UnicodeDecodeError):
            dataset_manager.analyze_schema(
                ADMIN, "id,名前\n1,太郎\n".encode("shift_jis")
            )

    def test_analyze_schema_strips_whitespace_headers(self, dataset_manager):
        """Headers with surrounding whitespace are normalized and accepted (matches DuckDB trim)."""
        csv_content = b"user id, amount ,category\nU001,100,food\n"
        columns, _ = dataset_manager.analyze_schema(ADMIN, csv_content)
        assert [c.name for c in columns] == ["user id", "amount", "category"]


# =============================================================================
# Settings: admin only, partial update, usage scope
# =============================================================================


@pytest.fixture
def ds_mgr(survey_world: SurveyWorld):
    from src.managers.dataset_manager import DatasetManager

    return DatasetManager(
        survey_batch_service=Mock(),
        dataset_store=survey_world.dataset_store,
        s3_service=survey_world.s3,
        departments=survey_world.departments,
        binding_store=survey_world.binding_store,
    )


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


class TestDatasetManagerIsAdminOnly:
    @pytest.mark.parametrize(
        "call",
        [
            lambda m: m.analyze_schema(MEMBER_A, b"a\n1"),
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
        assert survey_world.raw(survey_world.datasets_table, "missing") is None

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

    def test_delete_removes_the_bindings_to_the_dataset(self, ds_mgr, survey_world):
        dataset = survey_world.dataset()
        bindings = survey_world.binding_store
        bindings.put(PersonaBinding.for_dataset("p1", dataset.id, now=NOW))
        bindings.put(PersonaBinding.for_dataset("p2", dataset.id, now=NOW))
        bindings.put(PersonaBinding.for_dataset("p1", "other", now=NOW))

        assert ds_mgr.delete_dataset(ADMIN, dataset.id) is True

        assert [b.target_id for b in bindings.list_for_persona("p1")] == ["other"]
        assert bindings.list_for_persona("p2") == []

    def test_delete_keeps_kb_bindings_with_the_same_id(self, ds_mgr, survey_world):
        dataset = survey_world.dataset()
        bindings = survey_world.binding_store
        bindings.put(PersonaBinding.for_kb("p1", dataset.id, now=NOW))

        assert ds_mgr.delete_dataset(ADMIN, dataset.id) is True

        assert [b.target_id for b in bindings.list_for_persona("p1")] == [dataset.id]

    def test_delete_succeeds_when_the_bindings_cannot_be_removed(
        self, ds_mgr, survey_world, monkeypatch
    ):
        """Remaining bindings are skipped on read as bindings without a body."""
        dataset = survey_world.dataset()
        monkeypatch.setattr(
            survey_world.binding_store,
            "delete_for_target",
            Mock(side_effect=PersonaBindingStoreError("down")),
        )
        assert ds_mgr.delete_dataset(ADMIN, dataset.id) is True
        assert ds_mgr.get_dataset(ADMIN, dataset.id) is None

    def test_a_missing_dataset_removes_no_bindings(self, ds_mgr, survey_world):
        survey_world.binding_store.put(
            PersonaBinding.for_dataset("p1", "missing", now=NOW)
        )
        assert ds_mgr.delete_dataset(ADMIN, "missing") is False
        assert len(survey_world.binding_store.list_for_persona("p1")) == 1


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
        assert survey_world.raw(survey_world.datasets_table, "missing") is None

    def test_member_in_scope_can_then_use_it(self, ds_mgr, survey_world):
        """A usage-scope change takes effect from the next check (AccessPolicy.can_use_asset)."""
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
