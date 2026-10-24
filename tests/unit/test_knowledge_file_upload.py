"""
Tests for knowledge file upload functionality

U5 (BR5.9): the knowledge file is converted inside the request and never
stored; the memory screen is for people who manage the persona (BR4.1).
"""

import pytest
from pathlib import Path
from unittest.mock import Mock

from src.managers.components.resource_lookup import ResourceLookup
from src.managers.file_manager import FileUploadError
from src.managers.persona_memory_manager import (
    PersonaMemoryManager,
    PersonaMemoryManagerError,
)
from src.models.authz import AccessTarget, ResourceType
from src.models.errors import ErrorCode
from tests.conftest import ADMIN
from tests.error_helpers import raises_code

#: Manages every persona (the sample persona has no owner)
MANAGER = ADMIN.principal()


class TestKnowledgeFileUpload:
    """Test knowledge file conversion (nothing is saved)"""

    def test_unsupported_file_format(self, file_manager):
        """Test that unsupported file formats are rejected"""
        # Test format validation before markitdown import
        file_ext = Path("test.jpg").suffix.lower()
        assert file_ext not in file_manager.KNOWLEDGE_FILE_FORMATS

    def test_upload_knowledge_file_size_limit(self, file_manager):
        """Test that files exceeding 10MB are rejected"""
        large_content = b"x" * (11 * 1024 * 1024)

        with pytest.raises(FileUploadError) as exc_info:
            file_manager.convert_file_to_markdown(large_content, "large.txt")

        assert exc_info.value.code is ErrorCode.FILE_TOO_LARGE

    def test_upload_empty_file(self, file_manager):
        """Test that empty files are rejected"""
        with pytest.raises(FileUploadError) as exc_info:
            file_manager.convert_file_to_markdown(b"", "empty.txt")

        assert exc_info.value.code is ErrorCode.FILE_EMPTY


@pytest.mark.unit
class TestKnowledgeFileContentLimit:
    """The converted file obeys the same length rule as a typed knowledge entry."""

    @pytest.fixture
    def memory_manager(self):
        return PersonaMemoryManager(database_service=Mock(), memory_service=Mock())

    def test_over_the_limit_is_rejected_with_the_lengths(self, memory_manager):
        with raises_code(
            PersonaMemoryManagerError,
            ErrorCode.KNOWLEDGE_FILE_CONTENT_TOO_LONG,
            max_length=10000,
            content_length=10001,
        ):
            memory_manager.check_knowledge_file_content("A" * 10001)

    def test_exactly_the_limit_passes(self, memory_manager):
        memory_manager.check_knowledge_file_content("A" * 10000)


class _Reader:
    def __init__(self, db: Mock) -> None:
        self.db = db

    def read(self, ids, deadline):
        found = {}
        for i in ids:
            persona = self.db.get(i)
            if persona is not None:
                found[i] = AccessTarget(ResourceType.PERSONA, i, persona.owner_sub)
        return found


def _lookup(db: Mock) -> ResourceLookup:
    return ResourceLookup({ResourceType.PERSONA: _Reader(db)}, sample_rate=0)


@pytest.mark.unit
class TestPersonaKnowledgeAddition:
    """Test persona knowledge addition with character limits"""

    @pytest.fixture
    def mock_db(self, sample_persona):
        mock = Mock()
        mock.get.return_value = sample_persona
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
            database_service=mock_db,
            memory_service=mock_memory_svc,
            resource_lookup=_lookup(mock_db),
        )

    def test_add_knowledge_exceeds_limit(self, memory_manager, sample_persona):
        """Test that knowledge exceeding 10000 characters is rejected"""
        topic_name = "Test Topic"
        topic_content = "A" * 10001

        with raises_code(
            PersonaMemoryManagerError,
            ErrorCode.MEMORY_CONTENT_TOO_LONG,
            max_length=10000,
        ):
            memory_manager.add_knowledge(
                MANAGER, sample_persona.id, topic_name, topic_content
            )

    def test_add_knowledge_topic_name_limit(self, memory_manager, sample_persona):
        """Test that topic name exceeding 100 characters is rejected"""
        topic_name = "A" * 101
        topic_content = "Test content"

        with raises_code(
            PersonaMemoryManagerError,
            ErrorCode.MEMORY_TOPIC_NAME_TOO_LONG,
            max_length=100,
        ):
            memory_manager.add_knowledge(
                MANAGER, sample_persona.id, topic_name, topic_content
            )

    def test_add_knowledge_empty_content(self, memory_manager, sample_persona):
        """Test that empty content is rejected"""
        with raises_code(PersonaMemoryManagerError, ErrorCode.MEMORY_CONTENT_REQUIRED):
            memory_manager.add_knowledge(MANAGER, sample_persona.id, "Test Topic", "")

    def test_add_knowledge_within_limit(self, memory_manager, sample_persona):
        """Test that knowledge within 10000 character limit passes validation"""
        topic_name = "Test Topic"
        topic_content = "A" * 5000

        result = memory_manager.add_knowledge(
            MANAGER, sample_persona.id, topic_name, topic_content
        )
        assert result == "mem-123"

    def test_add_knowledge_memory_disabled(self, mock_db, sample_persona):
        """Test that disabled memory service raises error"""
        mgr = PersonaMemoryManager(
            database_service=mock_db,
            memory_service=None,
            resource_lookup=_lookup(mock_db),
        )

        with raises_code(PersonaMemoryManagerError, ErrorCode.MEMORY_FEATURE_DISABLED):
            mgr.add_knowledge(MANAGER, sample_persona.id, "Test Topic", "content")
