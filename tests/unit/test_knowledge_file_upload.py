"""
Tests for knowledge file upload functionality
"""

import pytest
from pathlib import Path
from src.managers.file_manager import FileUploadError


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


class TestPersonaKnowledgeAddition:
    """Test persona knowledge addition with character limits"""

    def test_add_knowledge_exceeds_limit(
        self, persona_manager, sample_persona, mock_database_service
    ):
        """Test that knowledge exceeding 10000 characters is rejected"""
        from src.managers.persona_manager import PersonaManagerError

        # Mock get_persona to return the sample persona
        mock_database_service.get_persona.return_value = sample_persona

        topic_name = "Test Topic"
        topic_content = "A" * 10001  # 10001 chars, exceeds limit

        with pytest.raises(PersonaManagerError) as exc_info:
            persona_manager.add_persona_knowledge(
                sample_persona.id, topic_name, topic_content
            )

        assert "10000文字以内" in str(exc_info.value)

    def test_add_knowledge_topic_name_limit(
        self, persona_manager, sample_persona, mock_database_service
    ):
        """Test that topic name exceeding 100 characters is rejected"""
        from src.managers.persona_manager import PersonaManagerError

        # Mock get_persona to return the sample persona
        mock_database_service.get_persona.return_value = sample_persona

        topic_name = "A" * 101  # 101 chars, exceeds limit
        topic_content = "Test content"

        with pytest.raises(PersonaManagerError) as exc_info:
            persona_manager.add_persona_knowledge(
                sample_persona.id, topic_name, topic_content
            )

        assert "100文字以内" in str(exc_info.value)

    def test_add_knowledge_empty_content(
        self, persona_manager, sample_persona, mock_database_service
    ):
        """Test that empty content is rejected"""
        from src.managers.persona_manager import PersonaManagerError

        # Mock get_persona to return the sample persona
        mock_database_service.get_persona.return_value = sample_persona

        with pytest.raises(PersonaManagerError) as exc_info:
            persona_manager.add_persona_knowledge(sample_persona.id, "Test Topic", "")

        assert "内容を入力してください" in str(exc_info.value)

    def test_add_knowledge_within_limit(
        self, persona_manager, sample_persona, mock_database_service
    ):
        """Test that knowledge within 10000 character limit passes validation"""
        from src.managers.persona_manager import PersonaManagerError

        # Mock get_persona to return the sample persona
        mock_database_service.get_persona.return_value = sample_persona

        topic_name = "Test Topic"
        topic_content = "A" * 5000  # 5000 chars, within limit

        # This will fail at memory service check, but validation should pass
        with pytest.raises(PersonaManagerError) as exc_info:
            persona_manager.add_persona_knowledge(
                sample_persona.id, topic_name, topic_content
            )

        # Should fail at memory service, not at validation
        assert "長期記憶機能が無効" in str(exc_info.value)
