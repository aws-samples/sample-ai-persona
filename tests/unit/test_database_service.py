"""
Unit tests for DatabaseService (DynamoDB).
Tests initialization and the table health checks; the table reads and writes
are tested with their stores (``tests/integration/test_*_store.py``).
"""

import pytest
from unittest.mock import Mock, patch
from botocore.exceptions import NoCredentialsError
from datetime import datetime

# hypothesisモジュールがない場合はテストをスキップ
hypothesis = pytest.importorskip(
    "hypothesis", reason="hypothesis is required for property-based tests"
)
from hypothesis import given, strategies as st  # noqa: E402
from hypothesis import settings  # noqa: E402
from src.services.database_service import DatabaseService, DatabaseError  # noqa: E402


class TestDatabaseServiceInitialization:
    """Test DatabaseService initialization and connection management."""

    @patch("boto3.client")
    def test_successful_initialization(self, mock_boto3_client):
        """Test successful DatabaseService initialization."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client
        # Initialize service
        service = DatabaseService(table_prefix="Test", region="us-east-1")

        # Verify client was created with correct config
        mock_boto3_client.assert_called_once_with("dynamodb", region_name="us-east-1")

        # Verify connection test was performed
        mock_client.list_tables.assert_called_once_with(Limit=1)

        # Verify table names are set correctly
        assert service.personas_table == "Test_Personas"
        assert service.discussions_table == "Test_Discussions"
        assert service.files_table == "Test_UploadedFiles"

    @patch("boto3.client")
    def test_initialization_with_missing_credentials(self, mock_boto3_client):
        """Test initialization fails with missing AWS credentials."""
        # Mock boto3 to raise NoCredentialsError
        mock_boto3_client.side_effect = NoCredentialsError()

        # Verify DatabaseError is raised with helpful message
        with pytest.raises(DatabaseError) as exc_info:
            DatabaseService()

        assert "AWS credentials are invalid or missing" in str(exc_info.value)
        assert "AWS_ACCESS_KEY_ID" in str(exc_info.value)

    @patch("boto3.client")
    def test_initialization_with_connection_error(self, mock_boto3_client):
        """Test initialization fails gracefully with connection error."""
        # Mock boto3 client that fails on list_tables
        mock_client = Mock()
        mock_client.list_tables.side_effect = Exception("Connection failed")
        mock_boto3_client.return_value = mock_client

        # Verify DatabaseError is raised
        with pytest.raises(DatabaseError) as exc_info:
            DatabaseService()

        assert "Failed to initialize DynamoDB client" in str(exc_info.value)


class TestDatabaseServiceHealthCheck:
    """Test health check and database info methods."""

    @patch("boto3.client")
    def test_health_check_success(self, mock_boto3_client):
        """Test successful health check."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {
            "TableNames": ["Test_Personas", "Test_Discussions", "Test_UploadedFiles"]
        }
        mock_client.describe_table.return_value = {"Table": {"TableStatus": "ACTIVE"}}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Check health
        is_healthy = service.check_database_health()

        assert is_healthy is True
        mock_client.describe_table.assert_called_once()

    @patch("boto3.client")
    def test_health_check_missing_tables(self, mock_boto3_client):
        """Test health check fails when tables are missing."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {
            "TableNames": ["Test_Personas"]  # Missing other tables
        }
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Check health
        is_healthy = service.check_database_health()

        assert is_healthy is False

    @patch("boto3.client")
    def test_get_database_info(self, mock_boto3_client):
        """Test getting database information."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.describe_table.return_value = {
            "Table": {
                "TableStatus": "ACTIVE",
                "ItemCount": 10,
                "TableSizeBytes": 1024,
                "CreationDateTime": "2024-01-01",
            }
        }
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test", region="us-west-2")

        # Get database info
        info = service.get_database_info()

        assert info["backend"] == "dynamodb"
        assert info["region"] == "us-west-2"
        assert info["table_prefix"] == "Test"
        assert "tables" in info


class TestSerializationRoundTripProperties:
    """
    Property-based tests for serialization round-trip.
    **Feature: dynamodb-migration, Property 5: Serialization round-trip**
    **Validates: Requirements 2.5**
    """

    @given(
        filename=st.text(
            min_size=1,
            max_size=100,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        file_size=st.integers(min_value=0, max_value=1000000000),
        mime_type=st.sampled_from(
            ["text/plain", "application/json", "image/png", "application/pdf"]
        ),
        uploaded_at=st.datetimes(
            min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31)
        ),
        attached=st.booleans(),
    )
    @settings(max_examples=100, deadline=None)
    def test_file_info_serialization_round_trip(
        self, filename, file_size, mime_type, uploaded_at, attached
    ):
        """
        Property test: upload metadata (U5: ``UploadedFile``, written by
        ``UploadedFileStore``) survives ``to_dict`` / ``from_dict``.

        **Feature: dynamodb-migration, Property 5: Serialization round-trip**
        """
        from src.models.uploaded_file import UploadedFile

        uploaded = UploadedFile.create_new(
            owner_sub="sub-a",
            file_type="discussion_document",
            original_filename=filename,
            file_size=file_size,
            file_hash="h",
            mime_type=mime_type,
            now=uploaded_at,
        ).with_path("s3://bucket/key")
        if attached:
            uploaded = uploaded.attached_to("discussion", "d1")

        assert UploadedFile.from_dict(uploaded.to_dict()) == uploaded


# Knowledge bases are KnowledgeBaseStore (tests/integration/test_knowledge_base_store.py),
# KB bindings are PersonaBindings (tests/integration/test_persona_binding_store.py)
# and upload metadata is UploadedFileStore (tests/integration/test_uploaded_file_store.py).


# Job records are written by JobStore (tests/integration/test_job_store.py) and
# persona bodies by PersonaStore (tests/integration/test_persona_store.py).


# =========================================================================
# U5: 使われていない読み出しの整理と専用データセット（BR6.2、BR8.2、NFR2.9）
# =========================================================================

_PERSONA_FILE_GSIS = (
    "CreatedAtIndex",
    "NameIndex",
    "OccupationIndex",
    "UploadedAtIndex",
)


def _database_service_functions() -> dict:
    import ast
    import inspect

    import src.services.database_service as module
    import src.services.persona_store as personas

    functions = {}
    for mod in (module, personas):
        tree = ast.parse(inspect.getsource(mod))
        source = inspect.getsource(mod).splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                functions[f"{mod.__name__}.{node.name}"] = "\n".join(
                    source[node.lineno - 1 : node.end_lineno]
                )
    return functions


class TestNoTypeFixedIndexReads:
    """Personas・UploadedFiles の type 固定の GSI を読むコードがない（BR8.2）。

    Discussions の CreatedAtIndex・ModeIndex は U6 の担当のため対象外。
    """

    def test_persona_and_file_reads_do_not_use_type_fixed_indexes(self):
        for name, body in _database_service_functions().items():
            if any(t in body for t in ("personas_table", "files_table", "table_name")):
                for index in _PERSONA_FILE_GSIS:
                    assert index not in body, f"{name} reads {index}"

    def test_persona_only_indexes_are_not_named_anywhere(self):
        import inspect

        import src.services.database_service as module
        import src.services.persona_store as personas

        for mod in (module, personas):
            source = inspect.getsource(mod)
            for index in ("NameIndex", "OccupationIndex", "UploadedAtIndex"):
                assert index not in source

    @pytest.mark.parametrize(
        "method",
        [
            "get_persona_count",
            "get_personas_by_name",
            "get_personas_by_occupation",
            "persona_exists",
            "get_all_personas",
            "save_binding",
            "get_bindings_by_persona",
            "delete_binding",
            "delete_bindings_by_persona",
            "save_kb_binding",
            "get_kb_binding_by_persona",
            "delete_kb_binding",
            "delete_kb_bindings_by_persona",
            "save_uploaded_file_info",
            "get_uploaded_file_info",
            "save_persona",
            "update_persona",
            "update_persona_avatar",
            "delete_persona_returning_old",
            "get_persona",
            "get_personas_by_ids",
            "scan_all_personas",
        ],
    )
    def test_unused_and_legacy_reads_are_gone(self, method):
        assert not hasattr(DatabaseService, method)


# Datasets are DatasetStore (tests/integration/test_dataset_store.py).


class TestDiscussionsMovedToDiscussionStore:
    """議論の読み書きは DiscussionStore に移した（U6、NFR Design Q1 A、NFR2.15）。

    型固定の GSI（CreatedAtIndex・TopicIndex・ModeIndex）と、議論の全件 Scan を
    する読み手が DatabaseService に残らない。
    """

    @pytest.mark.parametrize(
        "method",
        [
            "save_discussion",
            "get_discussion",
            "get_discussions",
            "delete_discussion",
            "get_discussions_by_topic",
            "get_discussions_by_participant",
            "get_discussions_by_date_range",
            "get_discussions_by_mode",
            "discussion_exists",
            "get_discussion_count",
            "update_discussion_insights",
            "get_insights_by_category",
            "_serialize_discussion",
            "_deserialize_discussion",
        ],
    )
    def test_discussion_methods_are_gone(self, method):
        assert not hasattr(DatabaseService, method)

    def test_no_function_reads_the_discussions_table(self):
        for name, body in _database_service_functions().items():
            if name.endswith(("__init__", "_get_table_name")):
                continue
            assert "self.discussions_table" not in body or name.rsplit(".", 1)[1] in {
                "check_database_health",
                "initialize_database",
                "get_database_info",
            }, f"{name} still touches Discussions"
