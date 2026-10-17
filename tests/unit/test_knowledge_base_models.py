"""KnowledgeBase / KB の紐付け（PersonaBinding）モデルの単体テスト"""

from datetime import datetime

from src.models.knowledge_base import KnowledgeBase
from src.models.persona_binding import PersonaBinding


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
    """KB の紐付けは U5 から PersonaBinding（固定キー KB）が持つ。"""

    def test_create_new(self):
        binding = PersonaBinding.for_kb("persona-1", "kb-1")
        assert binding.persona_id == "persona-1"
        assert binding.target_id == "kb-1"
        assert binding.target_key == "KB"
        assert binding.metadata_filters == {}

    def test_create_new_with_filters(self):
        filters = {"category": "tech", "level": "advanced"}
        binding = PersonaBinding.for_kb("p1", "kb1", metadata_filters=filters)
        assert binding.metadata_filters == filters

    def test_to_dict_from_dict_roundtrip(self):
        filters = {"key": "value"}
        binding = PersonaBinding.for_kb("p1", "kb1", metadata_filters=filters)
        restored = PersonaBinding.from_dict(binding.to_dict())
        assert restored == binding

    def test_from_dict_missing_metadata_filters(self):
        data = {
            "persona_id": "p1",
            "target_key": "KB",
            "target_type": "KB",
            "target_id": "kb1",
            "created_at": datetime.now().isoformat(),
        }
        binding = PersonaBinding.from_dict(data)
        assert binding.metadata_filters == {}


class TestKnowledgeBaseScope:
    """U7: 利用範囲（shared_group_ids、BR5.1、約束 C8）。"""

    def test_new_kb_is_admin_only(self):
        kb = KnowledgeBase.create_new("KB1", "n")
        assert kb.shared_group_ids == frozenset()
        assert "shared_group_ids" not in kb.to_dict()

    def test_scope_roundtrip_as_a_sorted_list(self):
        data = {
            **KnowledgeBase.create_new("KB1", "n").to_dict(),
            "shared_group_ids": {"sales", "plan"},
        }
        kb = KnowledgeBase.from_dict(data)
        assert kb.shared_group_ids == frozenset({"sales", "plan"})
        assert kb.to_dict()["shared_group_ids"] == ["plan", "sales"]

    def test_old_kb_without_scope(self):
        kb = KnowledgeBase.from_dict(KnowledgeBase.create_new("KB1", "n").to_dict())
        assert kb.shared_group_ids == frozenset()
