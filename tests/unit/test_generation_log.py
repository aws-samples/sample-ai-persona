"""Tests for persona generation log persistence."""

from datetime import datetime
from unittest.mock import Mock

from src.models.persona import Persona


SAMPLE_LOG = [
    {"type": "thinking", "content": "# データ分析\nCSVを確認します"},
    {"type": "tool_call", "content": "query: SELECT * FROM data LIMIT 5"},
    {
        "type": "tool_result",
        "tool_name": "query",
        "content": "| col1 | col2 |\n|---|---|\n| a | b |",
    },
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
            name="テスト",
            age=30,
            occupation="エンジニア",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        assert persona.generation_log is None
        assert persona.generation_context is None

    def test_to_dict_omits_none_fields(self):
        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="エンジニア",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        d = persona.to_dict()
        assert "generation_log" not in d
        assert "generation_context" not in d

    def test_to_dict_includes_when_set(self):
        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="エンジニア",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        persona.generation_log = SAMPLE_LOG
        persona.generation_context = SAMPLE_CONTEXT
        d = persona.to_dict()
        assert d["generation_log"] == SAMPLE_LOG
        assert d["generation_context"] == SAMPLE_CONTEXT

    def test_from_dict_without_log_fields(self):
        """既存ペルソナ（フィールドなし）からの復元"""
        d = {
            "id": "test-id",
            "name": "テスト",
            "age": 30,
            "occupation": "エンジニア",
            "background": "bg",
            "values": ["v"],
            "pain_points": ["p"],
            "goals": ["g"],
            "created_at": "2026-04-27T12:00:00",
            "updated_at": "2026-04-27T12:00:00",
        }
        persona = Persona.from_dict(d)
        assert persona.generation_log is None
        assert persona.generation_context is None

    def test_roundtrip_with_log(self):
        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="エンジニア",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        persona.generation_log = SAMPLE_LOG
        persona.generation_context = SAMPLE_CONTEXT
        restored = Persona.from_dict(persona.to_dict())
        assert restored.generation_log == SAMPLE_LOG
        assert restored.generation_context == SAMPLE_CONTEXT


class TestPersonaStoreGenerationLog:
    """PersonaStore が書く項目・読み戻す値での generation_log のテスト"""

    @staticmethod
    def _written(persona: Persona) -> dict:
        from src.services.stores.persona_store import PersonaStore

        client = Mock()
        PersonaStore(region="us-east-1", table_name="T", client=client).put_new(persona)
        return client.put_item.call_args.kwargs["Item"]

    @staticmethod
    def _persona(**kwargs) -> Persona:
        return Persona(
            id="test-id",
            name="Test",
            age=30,
            occupation="Eng",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
            created_at=datetime(2026, 1, 1),
            updated_at=datetime(2026, 1, 1),
            **kwargs,
        )

    def test_written_with_generation_log(self):
        item = self._written(
            self._persona(generation_log=SAMPLE_LOG, generation_context=SAMPLE_CONTEXT)
        )
        assert "generation_log" in item
        assert "generation_context" in item

    def test_written_without_generation_log(self):
        item = self._written(self._persona())
        assert "generation_log" not in item
        assert "generation_context" not in item

    def test_read_back_with_generation_log(self):
        from src.services.stores.persona_store import PersonaStore

        store = PersonaStore(region="us-east-1", table_name="T", client=Mock())
        persona = self._persona(
            generation_log=SAMPLE_LOG, generation_context=SAMPLE_CONTEXT
        )
        restored = store._persona(self._written(persona))
        assert restored.generation_log == SAMPLE_LOG
        assert restored.generation_context == SAMPLE_CONTEXT

    def test_read_back_without_generation_log(self):
        from src.services.stores.persona_store import PersonaStore

        store = PersonaStore(region="us-east-1", table_name="T", client=Mock())
        restored = store._persona(self._written(self._persona()))
        assert restored.generation_log is None
        assert restored.generation_context is None
