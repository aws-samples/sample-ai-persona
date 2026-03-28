"""KnowledgeBase / PersonaKBBinding モデルの単体テスト"""
from datetime import datetime

from src.models.knowledge_base import KnowledgeBase, PersonaKBBinding


class TestKnowledgeBase:
    def test_create_new(self):
        kb = KnowledgeBase.create_new("KB12345", "テストKB", "説明文")
        assert kb.knowledge_base_id == "KB12345"
        assert kb.name == "テストKB"
        assert kb.description == "説明文"
        assert kb.id  # UUID generated
        assert isinstance(kb.created_at, datetime)

    def test_create_new_default_description(self):
        kb = KnowledgeBase.create_new("KB99999", "名前のみ")
        assert kb.description == ""

    def test_to_dict_from_dict_roundtrip(self):
        kb = KnowledgeBase.create_new("KB12345", "テストKB", "説明文")
        data = kb.to_dict()
        restored = KnowledgeBase.from_dict(data)
        assert restored.id == kb.id
        assert restored.knowledge_base_id == kb.knowledge_base_id
        assert restored.name == kb.name
        assert restored.description == kb.description
        assert restored.created_at == kb.created_at
        assert restored.updated_at == kb.updated_at

    def test_to_dict_datetime_format(self):
        kb = KnowledgeBase.create_new("KB1", "test")
        data = kb.to_dict()
        # ISO 8601 string
        datetime.fromisoformat(data["created_at"])
        datetime.fromisoformat(data["updated_at"])


class TestPersonaKBBinding:
    def test_create_new(self):
        binding = PersonaKBBinding.create_new("persona-1", "kb-1")
        assert binding.persona_id == "persona-1"
        assert binding.kb_id == "kb-1"
        assert binding.metadata_filters == {}
        assert binding.id

    def test_create_new_with_filters(self):
        filters = {"category": "tech", "level": "advanced"}
        binding = PersonaKBBinding.create_new("p1", "kb1", metadata_filters=filters)
        assert binding.metadata_filters == filters

    def test_to_dict_from_dict_roundtrip(self):
        filters = {"key": "value"}
        binding = PersonaKBBinding.create_new("p1", "kb1", metadata_filters=filters)
        data = binding.to_dict()
        restored = PersonaKBBinding.from_dict(data)
        assert restored.id == binding.id
        assert restored.persona_id == binding.persona_id
        assert restored.kb_id == binding.kb_id
        assert restored.metadata_filters == filters
        assert restored.created_at == binding.created_at

    def test_from_dict_missing_metadata_filters(self):
        data = {
            "id": "test-id",
            "persona_id": "p1",
            "kb_id": "kb1",
            "created_at": datetime.now().isoformat(),
        }
        binding = PersonaKBBinding.from_dict(data)
        assert binding.metadata_filters == {}
