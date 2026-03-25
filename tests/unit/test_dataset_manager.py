"""
DatasetManager単体テスト
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime
from dataclasses import asdict

from src.models.dataset import Dataset, DatasetColumn, PersonaDatasetBinding


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


class TestPersonaDatasetBinding:
    """ペルソナ-データセット紐付けモデルテスト"""

    def test_binding_creation(self):
        """PersonaDatasetBinding作成テスト"""
        binding = PersonaDatasetBinding(
            id="bind-001",
            persona_id="persona-123",
            dataset_id="dataset-456",
            binding_keys={"user_id": "U001"},
            created_at=datetime.now(),
        )

        assert binding.id == "bind-001"
        assert binding.persona_id == "persona-123"
        assert binding.dataset_id == "dataset-456"
        assert binding.binding_keys["user_id"] == "U001"

    def test_binding_create_new(self):
        """PersonaDatasetBinding.create_newテスト"""
        binding = PersonaDatasetBinding.create_new(
            persona_id="persona-abc",
            dataset_id="dataset-xyz",
            binding_keys={"customer_id": "C123"},
        )

        assert binding.id is not None
        assert binding.persona_id == "persona-abc"
        assert binding.dataset_id == "dataset-xyz"
        assert binding.created_at is not None

    def test_binding_to_dict(self):
        """PersonaDatasetBinding辞書変換テスト"""
        now = datetime.now()
        binding = PersonaDatasetBinding(
            id="bind-002",
            persona_id="persona-abc",
            dataset_id="dataset-xyz",
            binding_keys={"customer_id": "C123", "region": "Tokyo"},
            created_at=now,
        )

        data = binding.to_dict()

        assert data["id"] == "bind-002"
        assert data["persona_id"] == "persona-abc"
        assert data["dataset_id"] == "dataset-xyz"
        assert data["binding_keys"]["customer_id"] == "C123"
        assert data["binding_keys"]["region"] == "Tokyo"

    def test_binding_from_dict(self):
        """PersonaDatasetBinding辞書からの復元テスト"""
        data = {
            "id": "bind-003",
            "persona_id": "persona-def",
            "dataset_id": "dataset-ghi",
            "binding_keys": {"member_id": "M999"},
            "created_at": "2024-02-01T12:00:00",
        }

        binding = PersonaDatasetBinding.from_dict(data)

        assert binding.id == "bind-003"
        assert binding.persona_id == "persona-def"
        assert binding.dataset_id == "dataset-ghi"
        assert binding.binding_keys["member_id"] == "M999"


class TestMCPServerManager:
    """MCPサーバーマネージャーテスト"""

    def test_mcp_manager_singleton(self):
        """MCPマネージャーシングルトンテスト"""
        from src.services.mcp_server_manager import get_mcp_manager

        manager1 = get_mcp_manager()
        manager2 = get_mcp_manager()

        assert manager1 is manager2

    def test_mcp_manager_initial_state(self):
        """MCPマネージャー初期状態テスト"""
        from src.services.mcp_server_manager import MCPServerManager

        manager = MCPServerManager()

        assert manager.is_running() is False
        assert manager.enabled is False
        assert manager.get_mcp_client() is None

    def test_mcp_manager_stop_when_not_running(self):
        """MCPマネージャー停止（未起動時）テスト"""
        from src.services.mcp_server_manager import MCPServerManager

        manager = MCPServerManager()
        result = manager.stop()

        assert result is True  # 未起動時はTrueを返す

    def test_mcp_manager_toggle_off(self):
        """MCPマネージャートグルOFFテスト"""
        from src.services.mcp_server_manager import MCPServerManager

        manager = MCPServerManager()
        result = manager.toggle(False)

        assert result is True
        assert manager.is_running() is False

    def test_mcp_manager_get_tools_when_not_running(self):
        """MCPマネージャーツール取得（未起動時）テスト"""
        from src.services.mcp_server_manager import MCPServerManager

        manager = MCPServerManager()
        tools = manager.get_tools()

        assert tools == []


class TestInterviewManagerDataset:
    """InterviewManagerデータセット連携テスト"""

    def test_interview_session_enable_dataset_field(self):
        """InterviewSessionのenable_datasetフィールドテスト"""
        from src.managers.interview_manager import InterviewSession

        session = InterviewSession(
            id="session-001",
            participants=["p1", "p2"],
            messages=[],
            created_at=datetime.now(),
            enable_dataset=True,
        )

        assert session.enable_dataset is True

    def test_interview_session_enable_dataset_default(self):
        """InterviewSessionのenable_datasetデフォルト値テスト"""
        from src.managers.interview_manager import InterviewSession

        session = InterviewSession(
            id="session-002",
            participants=["p1"],
            messages=[],
            created_at=datetime.now(),
        )

        assert session.enable_dataset is False

    def test_interview_session_add_message_preserves_enable_dataset(self):
        """add_user_messageでenable_datasetが保持されるテスト"""
        from src.managers.interview_manager import InterviewSession

        session = InterviewSession(
            id="session-003",
            participants=["p1"],
            messages=[],
            created_at=datetime.now(),
            enable_dataset=True,
        )

        new_session = session.add_user_message("テストメッセージ")

        assert new_session.enable_dataset is True

    def test_interview_session_add_response_preserves_enable_dataset(self):
        """add_persona_responseでenable_datasetが保持されるテスト"""
        from src.managers.interview_manager import InterviewSession

        session = InterviewSession(
            id="session-004",
            participants=["p1"],
            messages=[],
            created_at=datetime.now(),
            enable_dataset=True,
        )

        new_session = session.add_persona_response("p1", "Persona1", "応答")

        assert new_session.enable_dataset is True

    def test_interview_session_add_document_preserves_enable_dataset(self):
        """add_documentでenable_datasetが保持されるテスト"""
        from src.managers.interview_manager import InterviewSession

        session = InterviewSession(
            id="session-005",
            participants=["p1"],
            messages=[],
            created_at=datetime.now(),
            enable_dataset=True,
        )

        new_session = session.add_document({"filename": "test.pdf"})

        assert new_session.enable_dataset is True


class TestDatasetManagerSchemaAnalysis:
    """DatasetManagerスキーマ解析テスト"""

    @pytest.fixture
    def dataset_manager(self):
        """DatasetManagerインスタンス（モック使用）"""
        with patch("src.managers.dataset_manager.service_factory") as mock_factory:
            mock_factory.get_database_service.return_value = Mock()
            mock_factory.get_s3_service.return_value = Mock()
            from src.managers.dataset_manager import DatasetManager

            return DatasetManager()

    def test_analyze_schema_basic(self, dataset_manager):
        """基本的なスキーマ解析テスト"""
        csv_content = b"id,name,age\n1,Alice,30\n2,Bob,25"

        columns, row_count = dataset_manager.analyze_schema(csv_content)

        assert len(columns) == 3
        assert columns[0].name == "id"
        assert columns[1].name == "name"
        assert columns[2].name == "age"
        assert row_count == 2

    def test_analyze_schema_type_inference(self, dataset_manager):
        """型推論テスト"""
        csv_content = b"int_col,float_col,str_col\n1,1.5,hello\n2,2.5,world"

        columns, _ = dataset_manager.analyze_schema(csv_content)

        assert columns[0].data_type == "integer"
        assert columns[1].data_type == "float"
        assert columns[2].data_type == "string"

    def test_analyze_schema_date_inference(self, dataset_manager):
        """日付型推論テスト"""
        csv_content = b"date_col,value\n2024-01-01,100\n2024-02-01,200"

        columns, _ = dataset_manager.analyze_schema(csv_content)

        assert columns[0].data_type == "date"
        assert columns[1].data_type == "integer"

    def test_analyze_schema_empty_file(self, dataset_manager):
        """空ファイルのスキーマ解析テスト"""
        csv_content = b""

        columns, row_count = dataset_manager.analyze_schema(csv_content)

        assert columns == []
        assert row_count == 0
