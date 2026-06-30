"""
Tests for knowledge file upload functionality
"""

import pytest
from pathlib import Path
from unittest.mock import Mock

from src.managers.file_manager import FileUploadError
from src.managers.persona_memory_manager import (
    PersonaMemoryManager,
    PersonaMemoryManagerError,
)


class TestKnowledgeFileUpload:
    """Test knowledge file upload and conversion"""

    def test_unsupported_file_format(self, file_manager):
        """Test that unsupported file formats are rejected"""
        # Test format validation before markitdown import
        file_ext = Path("test.jpg").suffix.lower()
        assert file_ext not in file_manager.KNOWLEDGE_FILE_FORMATS

    def test_upload_knowledge_file_size_limit(self, file_manager):
        """Test that files exceeding 10MB are rejected"""
        # Create 11MB content
        large_content = b"x" * (11 * 1024 * 1024)

        with pytest.raises(FileUploadError) as exc_info:
            file_manager.upload_knowledge_file(large_content, "large.txt")

        assert "制限を超えています" in str(exc_info.value)

    def test_upload_empty_file(self, file_manager):
        """Test that empty files are rejected"""
        with pytest.raises(FileUploadError) as exc_info:
            file_manager.upload_knowledge_file(b"", "empty.txt")

        assert "空です" in str(exc_info.value)


@pytest.mark.unit
class TestPersonaKnowledgeAddition:
    """Test persona knowledge addition with character limits"""

    @pytest.fixture
    def mock_db(self, sample_persona):
        mock = Mock()
        mock.get_persona.return_value = sample_persona
        return mock

    @pytest.fixture
    def mock_memory_svc(self):
        mock = Mock()
        mock.is_semantic_enabled = True
        mock.save_knowledge.return_value = "mem-123"
        return mock

    @pytest.fixture
    def memory_manager(self, mock_db, mock_memory_svc):
        return PersonaMemoryManager(
            database_service=mock_db, memory_service=mock_memory_svc
        )

    def test_add_knowledge_exceeds_limit(self, memory_manager, sample_persona):
        """Test that knowledge exceeding 10000 characters is rejected"""
        topic_name = "Test Topic"
        topic_content = "A" * 10001

        with pytest.raises(PersonaMemoryManagerError) as exc_info:
            memory_manager.add_knowledge(sample_persona.id, topic_name, topic_content)

        assert "10000文字以内" in str(exc_info.value)

    def test_add_knowledge_topic_name_limit(self, memory_manager, sample_persona):
        """Test that topic name exceeding 100 characters is rejected"""
        topic_name = "A" * 101
        topic_content = "Test content"

        with pytest.raises(PersonaMemoryManagerError) as exc_info:
            memory_manager.add_knowledge(sample_persona.id, topic_name, topic_content)

        assert "100文字以内" in str(exc_info.value)

    def test_add_knowledge_empty_content(self, memory_manager, sample_persona):
        """Test that empty content is rejected"""
        with pytest.raises(PersonaMemoryManagerError) as exc_info:
            memory_manager.add_knowledge(sample_persona.id, "Test Topic", "")

        assert "内容を入力してください" in str(exc_info.value)

    def test_add_knowledge_within_limit(self, memory_manager, sample_persona):
        """Test that knowledge within 10000 character limit passes validation"""
        topic_name = "Test Topic"
        topic_content = "A" * 5000

        result = memory_manager.add_knowledge(
            sample_persona.id, topic_name, topic_content
        )
        assert result == "mem-123"

    def test_add_knowledge_memory_disabled(self, mock_db, sample_persona):
        """Test that disabled memory service raises error"""
        mgr = PersonaMemoryManager(database_service=mock_db, memory_service=None)

        with pytest.raises(PersonaMemoryManagerError) as exc_info:
            mgr.add_knowledge(sample_persona.id, "Test Topic", "content")

        assert "長期記憶機能が無効" in str(exc_info.value)
