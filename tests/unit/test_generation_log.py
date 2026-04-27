"""Tests for persona generation log persistence."""

from datetime import datetime
from unittest.mock import Mock, patch

from src.models.persona import Persona


SAMPLE_LOG = [
    {"type": "thinking", "content": "# データ分析\nCSVを確認します"},
    {"type": "tool_call", "content": "query: SELECT * FROM data LIMIT 5"},
    {"type": "tool_result", "tool_name": "query", "content": "| col1 | col2 |\n|---|---|\n| a | b |"},
]

SAMPLE_CONTEXT = {
    "data_type": "interview",
    "data_description": "活動ログ",
    "custom_prompt": "30代のエンジニアを生成",
    "source_files": ["interview.pdf"],
    "persona_count": 1,
    "generated_at": "2026-04-27T17:00:00",
}


class TestPersonaGenerationLogModel:
    """Persona モデルの generation_log / generation_context テスト"""

    def test_create_new_defaults_to_none(self):
        persona = Persona.create_new(
            name="テスト", age=30, occupation="エンジニア",
            background="bg", values=["v"], pain_points=["p"], goals=["g"],
        )
        assert persona.generation_log is None
        assert persona.generation_context is None

    def test_to_dict_omits_none_fields(self):
        persona = Persona.create_new(
            name="テスト", age=30, occupation="エンジニア",
            background="bg", values=["v"], pain_points=["p"], goals=["g"],
        )
        d = persona.to_dict()
        assert "generation_log" not in d
        assert "generation_context" not in d

    def test_to_dict_includes_when_set(self):
        persona = Persona.create_new(
            name="テスト", age=30, occupation="エンジニア",
            background="bg", values=["v"], pain_points=["p"], goals=["g"],
        )
        persona.generation_log = SAMPLE_LOG
        persona.generation_context = SAMPLE_CONTEXT
        d = persona.to_dict()
        assert d["generation_log"] == SAMPLE_LOG
        assert d["generation_context"] == SAMPLE_CONTEXT

    def test_from_dict_without_log_fields(self):
        """既存ペルソナ（フィールドなし）からの復元"""
        d = {
            "id": "test-id", "name": "テスト", "age": 30, "occupation": "エンジニア",
            "background": "bg", "values": ["v"], "pain_points": ["p"], "goals": ["g"],
            "created_at": "2026-04-27T12:00:00", "updated_at": "2026-04-27T12:00:00",
        }
        persona = Persona.from_dict(d)
        assert persona.generation_log is None
        assert persona.generation_context is None

    def test_roundtrip_with_log(self):
        persona = Persona.create_new(
            name="テスト", age=30, occupation="エンジニア",
            background="bg", values=["v"], pain_points=["p"], goals=["g"],
        )
        persona.generation_log = SAMPLE_LOG
        persona.generation_context = SAMPLE_CONTEXT
        restored = Persona.from_dict(persona.to_dict())
        assert restored.generation_log == SAMPLE_LOG
        assert restored.generation_context == SAMPLE_CONTEXT


class TestDatabaseServiceGenerationLog:
    """DatabaseService の serialize/deserialize での generation_log テスト"""

    @patch("boto3.client")
    def test_serialize_with_generation_log(self, mock_boto3_client):
        from src.services.database_service import DatabaseService

        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client
        service = DatabaseService()

        persona = Persona(
            id="test-id", name="Test", age=30, occupation="Eng",
            background="bg", values=["v"], pain_points=["p"], goals=["g"],
            created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
            generation_log=SAMPLE_LOG, generation_context=SAMPLE_CONTEXT,
        )
        serialized = service._serialize_persona(persona)
        assert "generation_log" in serialized
        assert "generation_context" in serialized

    @patch("boto3.client")
    def test_serialize_without_generation_log(self, mock_boto3_client):
        from src.services.database_service import DatabaseService

        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client
        service = DatabaseService()

        persona = Persona(
            id="test-id", name="Test", age=30, occupation="Eng",
            background="bg", values=["v"], pain_points=["p"], goals=["g"],
            created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        )
        serialized = service._serialize_persona(persona)
        assert "generation_log" not in serialized
        assert "generation_context" not in serialized

    @patch("boto3.client")
    def test_deserialize_with_generation_log(self, mock_boto3_client):
        from src.services.database_service import DatabaseService

        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client
        service = DatabaseService()

        item = {
            "id": {"S": "test-id"}, "name": {"S": "Test"}, "age": {"N": "30"},
            "occupation": {"S": "Eng"}, "background": {"S": "bg"},
            "values": {"L": [{"S": "v"}]}, "pain_points": {"L": [{"S": "p"}]},
            "goals": {"L": [{"S": "g"}]},
            "created_at": {"S": "2026-01-01T00:00:00"},
            "updated_at": {"S": "2026-01-01T00:00:00"},
            "type": {"S": "persona"},
            "generation_log": {"L": [
                {"M": {"type": {"S": "thinking"}, "content": {"S": "分析中"}}},
            ]},
            "generation_context": {"M": {
                "data_type": {"S": "interview"},
                "persona_count": {"N": "1"},
            }},
        }
        persona = service._deserialize_persona(item)
        assert persona.generation_log == [{"type": "thinking", "content": "分析中"}]
        assert persona.generation_context["data_type"] == "interview"

    @patch("boto3.client")
    def test_deserialize_without_generation_log(self, mock_boto3_client):
        """既存データ（フィールドなし）のデシリアライズ"""
        from src.services.database_service import DatabaseService

        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client
        service = DatabaseService()

        item = {
            "id": {"S": "test-id"}, "name": {"S": "Test"}, "age": {"N": "30"},
            "occupation": {"S": "Eng"}, "background": {"S": "bg"},
            "values": {"L": [{"S": "v"}]}, "pain_points": {"L": [{"S": "p"}]},
            "goals": {"L": [{"S": "g"}]},
            "created_at": {"S": "2026-01-01T00:00:00"},
            "updated_at": {"S": "2026-01-01T00:00:00"},
            "type": {"S": "persona"},
        }
        persona = service._deserialize_persona(item)
        assert persona.generation_log is None
        assert persona.generation_context is None
