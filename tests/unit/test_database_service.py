"""
Unit tests for DatabaseService (DynamoDB).
Tests initialization, connection management, and retry logic.
"""

import pytest
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError, NoCredentialsError
from datetime import datetime

# hypothesisモジュールがない場合はテストをスキップ
hypothesis = pytest.importorskip(
    "hypothesis", reason="hypothesis is required for property-based tests"
)
from hypothesis import given, strategies as st  # noqa: E402
from hypothesis import settings  # noqa: E402
from src.services.database_service import DatabaseService, DatabaseError  # noqa: E402
from src.models.persona import Persona  # noqa: E402
from src.models.message import Message  # noqa: E402
from src.models.insight import Insight  # noqa: E402


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


class TestDatabaseServiceRetryLogic:
    """Test retry logic and error handling."""

    @patch("time.sleep")
    @patch("boto3.client")
    def test_retry_on_throttling_with_exponential_backoff(
        self, mock_boto3_client, mock_sleep
    ):
        """Test retry logic with exponential backoff for throttling errors."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create mock operation that fails twice with throttling, then succeeds
        mock_operation = Mock()
        throttle_error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "test_operation",
        )
        mock_operation.side_effect = [throttle_error, throttle_error, {"success": True}]

        # Execute with retry
        result = service._execute_with_retry(mock_operation, operation_name="test")

        # Verify operation was called 3 times
        assert mock_operation.call_count == 3

        # Verify exponential backoff delays (1s, 2s)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)  # 2^0 = 1
        mock_sleep.assert_any_call(2)  # 2^1 = 2

        # Verify result
        assert result == {"success": True}

    @patch("time.sleep")
    @patch("boto3.client")
    def test_retry_on_network_error(self, mock_boto3_client, mock_sleep):
        """Test retry logic for network errors."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create mock operation that fails once with network error, then succeeds
        mock_operation = Mock()
        mock_operation.side_effect = [
            ConnectionError("Network error"),
            {"success": True},
        ]

        # Execute with retry
        result = service._execute_with_retry(mock_operation, operation_name="test")

        # Verify operation was called twice
        assert mock_operation.call_count == 2

        # Verify fixed delay (1s)
        mock_sleep.assert_called_once_with(1)

        # Verify result
        assert result == {"success": True}

    @patch("time.sleep")
    @patch("boto3.client")
    def test_max_retries_exceeded(self, mock_boto3_client, mock_sleep):
        """Test that DatabaseError is raised after max retries."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create mock operation that always fails with throttling
        mock_operation = Mock()
        throttle_error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "test_operation",
        )
        mock_operation.side_effect = throttle_error

        # Verify DatabaseError is raised after max retries
        with pytest.raises(DatabaseError) as exc_info:
            service._execute_with_retry(
                mock_operation, max_retries=3, operation_name="test"
            )

        # Verify operation was called 3 times
        assert mock_operation.call_count == 3

        # Verify error message
        assert "failed after 3 attempts" in str(exc_info.value)
        assert "throttling" in str(exc_info.value)

    @patch("boto3.client")
    def test_non_retryable_error(self, mock_boto3_client):
        """Test that non-retryable errors are not retried."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create mock operation that fails with validation error
        mock_operation = Mock()
        validation_error = ClientError(
            {"Error": {"Code": "ValidationException"}}, "test_operation"
        )
        mock_operation.side_effect = validation_error

        # Verify DatabaseError is raised immediately without retry
        with pytest.raises(DatabaseError) as exc_info:
            service._execute_with_retry(mock_operation, operation_name="test")

        # Verify operation was called only once (no retry)
        assert mock_operation.call_count == 1

        # Verify error message
        assert "validation error" in str(exc_info.value)

    @patch("boto3.client")
    def test_resource_not_found_error(self, mock_boto3_client):
        """Test handling of ResourceNotFoundException."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create mock operation that fails with resource not found
        mock_operation = Mock()
        not_found_error = ClientError(
            {"Error": {"Code": "ResourceNotFoundException"}}, "test_operation"
        )
        mock_operation.side_effect = not_found_error

        # Verify DatabaseError is raised with helpful message
        with pytest.raises(DatabaseError) as exc_info:
            service._execute_with_retry(mock_operation, operation_name="test")

        # Verify error message mentions table creation
        assert "Table not found" in str(exc_info.value)
        assert "ensure DynamoDB tables are created" in str(exc_info.value)


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


class TestDatabaseServiceSerialization:
    """Test serialization and deserialization methods."""

    @patch("boto3.client")
    def test_serialize_persona(self, mock_boto3_client):
        """Test persona serialization to DynamoDB format."""
        from datetime import datetime
        from src.models.persona import Persona

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create test persona
        persona = Persona(
            id="test-id",
            name="Test User",
            age=30,
            occupation="Engineer",
            background="Test background",
            values=["value1", "value2"],
            pain_points=["pain1", "pain2"],
            goals=["goal1", "goal2"],
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            updated_at=datetime(2024, 1, 2, 12, 0, 0),
        )

        # Serialize
        serialized = service._serialize_persona(persona)

        # Verify structure (DynamoDB format has type descriptors)
        assert "id" in serialized
        assert "name" in serialized
        assert "age" in serialized
        assert "values" in serialized
        assert "created_at" in serialized
        assert "type" in serialized

        # Verify type descriptors exist (DynamoDB format)
        assert "S" in serialized["id"]  # String type
        assert "N" in serialized["age"]  # Number type
        assert "L" in serialized["values"]  # List type

    @patch("boto3.client")
    def test_deserialize_persona(self, mock_boto3_client):
        """Test persona deserialization from DynamoDB format."""
        from datetime import datetime

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create DynamoDB format item
        item = {
            "id": {"S": "test-id"},
            "name": {"S": "Test User"},
            "age": {"N": "30"},
            "occupation": {"S": "Engineer"},
            "background": {"S": "Test background"},
            "values": {"L": [{"S": "value1"}, {"S": "value2"}]},
            "pain_points": {"L": [{"S": "pain1"}, {"S": "pain2"}]},
            "goals": {"L": [{"S": "goal1"}, {"S": "goal2"}]},
            "created_at": {"S": "2024-01-01T12:00:00"},
            "updated_at": {"S": "2024-01-02T12:00:00"},
            "type": {"S": "persona"},
        }

        # Deserialize
        persona = service._deserialize_persona(item)

        # Verify persona object
        assert persona.id == "test-id"
        assert persona.name == "Test User"
        assert persona.age == 30
        assert persona.occupation == "Engineer"
        assert persona.values == ["value1", "value2"]
        assert persona.pain_points == ["pain1", "pain2"]
        assert persona.goals == ["goal1", "goal2"]
        assert persona.created_at == datetime(2024, 1, 1, 12, 0, 0)
        assert persona.updated_at == datetime(2024, 1, 2, 12, 0, 0)

    # U6: discussions moved to DiscussionStore (tests/integration/test_discussion_store.py).

    # U5: upload metadata moved to UploadedFileStore
    # (tests/integration/test_uploaded_file_store.py).

    @patch("boto3.client")
    def test_round_trip_persona_serialization(self, mock_boto3_client):
        """Test round-trip serialization/deserialization for persona."""
        from datetime import datetime
        from src.models.persona import Persona

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create original persona
        original = Persona(
            id="test-id",
            name="Test User",
            age=30,
            occupation="Engineer",
            background="Test background",
            values=["value1", "value2"],
            pain_points=["pain1", "pain2"],
            goals=["goal1", "goal2"],
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            updated_at=datetime(2024, 1, 2, 12, 0, 0),
        )

        # Serialize then deserialize
        serialized = service._serialize_persona(original)
        deserialized = service._deserialize_persona(serialized)

        # Verify round-trip preserves data
        assert deserialized.id == original.id
        assert deserialized.name == original.name
        assert deserialized.age == original.age
        assert deserialized.occupation == original.occupation
        assert deserialized.background == original.background
        assert deserialized.values == original.values
        assert deserialized.pain_points == original.pain_points
        assert deserialized.goals == original.goals
        assert deserialized.created_at == original.created_at
        assert deserialized.updated_at == original.updated_at

    @patch("boto3.client")
    def test_round_trip_persona_with_demographics(self, mock_boto3_client):
        """gender/country/city/tags を含むペルソナの往復シリアライズ"""
        from src.models.persona import Persona

        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        original = Persona.create_new(
            name="John Smith",
            age=42,
            occupation="Designer",
            background="bg",
            values=["v1"],
            pain_points=["p1"],
            goals=["g1"],
            gender="male",
            country="US",
            city="New York",
            tags=["premium", "early-adopter"],
        )

        serialized = service._serialize_persona(original)
        deserialized = service._deserialize_persona(serialized)

        assert deserialized.gender == "male"
        assert deserialized.country == "US"
        assert deserialized.city == "New York"
        assert deserialized.tags == ["premium", "early-adopter"]

    @patch("boto3.client")
    def test_round_trip_preserves_every_persona_field(self, mock_boto3_client):
        """Persona の全フィールドが DynamoDB 往復で失われないこと。

        シリアライズはフィールドを明示列挙しているため、モデルに項目を足しても
        ここを更新し忘れると保存されない（avatar_path で発生）。全項目に値を
        入れた dataclass の等価比較で、列挙漏れを検出する。
        """
        from dataclasses import fields

        from src.models.persona import Persona

        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        original = Persona(
            id="p1",
            name="n",
            age=40,
            occupation="o",
            background="b",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            updated_at=datetime(2024, 1, 2, 12, 0, 0),
            gender="female",
            country="JP",
            city="Tokyo",
            tags=["t"],
            generation_log=[{"type": "text", "content": "c"}],
            generation_context={"source": "file"},
            avatar_path="s3://bucket/persona_avatars/p1/x.webp",
            owner_sub="sub-1",
            # shared_group_ids は ResourceSharing だけが書く（BR1.6）ため、
            # save のシリアライズに載らない。既定の空集合で往復を確かめる
        )
        unset = [
            f.name for f in fields(Persona) if getattr(original, f.name) in (None, [])
        ]
        assert unset == [], f"テストデータに値が無いフィールド: {unset}"

        restored = service._deserialize_persona(service._serialize_persona(original))

        assert restored == original

    @patch("boto3.client")
    def test_deserialize_persona_without_demographics(self, mock_boto3_client):
        """新フィールドを持たないDynamoDB itemを後方互換でデシリアライズできる"""
        from boto3.dynamodb.types import TypeSerializer

        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        serializer = TypeSerializer()
        legacy_item = {
            "id": serializer.serialize("legacy-id"),
            "name": serializer.serialize("旧ペルソナ"),
            "age": serializer.serialize(50),
            "occupation": serializer.serialize("教師"),
            "background": serializer.serialize("経験豊富"),
            "values": serializer.serialize(["誠実"]),
            "pain_points": serializer.serialize(["多忙"]),
            "goals": serializer.serialize(["成長"]),
            "created_at": serializer.serialize("2024-01-01T12:00:00"),
            "updated_at": serializer.serialize("2024-01-01T12:00:00"),
        }

        persona = service._deserialize_persona(legacy_item)
        assert persona.gender is None
        assert persona.country is None
        assert persona.city is None
        assert persona.tags == []


# Define strategies outside the class to avoid circular reference issues
def _message_strategy():
    """Generate random Message objects."""
    return st.builds(
        Message,
        persona_id=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        persona_name=st.text(
            min_size=1,
            max_size=100,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        content=st.text(
            min_size=1,
            max_size=500,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        timestamp=st.datetimes(
            min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31)
        ),
        message_type=st.sampled_from(
            ["statement", "question", "response", "facilitator"]
        ),
        round_number=st.one_of(st.none(), st.integers(min_value=1, max_value=100)),
    )


def _insight_strategy():
    """Generate random Insight objects."""
    return st.builds(
        Insight,
        category=st.text(
            min_size=1,
            max_size=100,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        description=st.text(
            min_size=1,
            max_size=500,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        supporting_messages=st.lists(
            st.text(
                min_size=1,
                max_size=50,
                alphabet=st.characters(blacklist_categories=("Cs",)),
            ),
            min_size=0,
            max_size=10,
        ),
        # Avoid very small floats that cause decimal underflow in DynamoDB
        confidence_score=st.floats(
            min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False
        ),
    )


def _persona_strategy():
    """Generate random Persona objects."""
    return st.builds(
        Persona,
        id=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        name=st.text(
            min_size=1,
            max_size=100,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        age=st.integers(min_value=18, max_value=100),
        occupation=st.text(
            min_size=1,
            max_size=100,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        background=st.text(
            min_size=1,
            max_size=500,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        values=st.lists(
            st.text(
                min_size=1,
                max_size=100,
                alphabet=st.characters(blacklist_categories=("Cs",)),
            ),
            min_size=1,
            max_size=10,
        ),
        pain_points=st.lists(
            st.text(
                min_size=1,
                max_size=100,
                alphabet=st.characters(blacklist_categories=("Cs",)),
            ),
            min_size=1,
            max_size=10,
        ),
        goals=st.lists(
            st.text(
                min_size=1,
                max_size=100,
                alphabet=st.characters(blacklist_categories=("Cs",)),
            ),
            min_size=1,
            max_size=10,
        ),
        created_at=st.datetimes(
            min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31)
        ),
        updated_at=st.datetimes(
            min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31)
        ),
    )


class TestSerializationRoundTripProperties:
    """
    Property-based tests for serialization round-trip.
    **Feature: dynamodb-migration, Property 5: Serialization round-trip**
    **Validates: Requirements 2.5**
    """

    @patch("boto3.client")
    @given(persona=_persona_strategy())
    @settings(max_examples=100, deadline=None)
    def test_persona_serialization_round_trip(self, mock_boto3_client, persona):
        """
        Property test: For any valid Persona object, serializing to DynamoDB format
        and then deserializing should produce an equivalent object.

        **Feature: dynamodb-migration, Property 5: Serialization round-trip**
        **Validates: Requirements 2.5**
        """
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Serialize then deserialize
        serialized = service._serialize_persona(persona)
        deserialized = service._deserialize_persona(serialized)

        # Verify all fields are preserved
        assert deserialized.id == persona.id
        assert deserialized.name == persona.name
        assert deserialized.age == persona.age
        assert deserialized.occupation == persona.occupation
        assert deserialized.background == persona.background
        assert deserialized.values == persona.values
        assert deserialized.pain_points == persona.pain_points
        assert deserialized.goals == persona.goals
        assert deserialized.created_at == persona.created_at
        assert deserialized.updated_at == persona.updated_at

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


class TestPersonaCRUDOperations:
    """Test Persona CRUD operations."""

    @patch("boto3.client")
    def test_save_persona(self, mock_boto3_client):
        """Test saving a persona to DynamoDB."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.put_item.return_value = {}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Create test persona
        persona = Persona(
            id="test-id",
            name="Test User",
            age=30,
            occupation="Engineer",
            background="Test background",
            values=["value1", "value2"],
            pain_points=["pain1", "pain2"],
            goals=["goal1", "goal2"],
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            updated_at=datetime(2024, 1, 2, 12, 0, 0),
        )

        # Save persona
        result = service.save_persona(persona)

        # Verify put_item was called
        mock_client.put_item.assert_called_once()
        call_args = mock_client.put_item.call_args
        assert call_args[1]["TableName"] == "Test_Personas"
        assert "Item" in call_args[1]

        # Verify result is persona ID
        assert result == "test-id"

    @patch("boto3.client")
    def test_update_persona(self, mock_boto3_client):
        """Test updating a persona in DynamoDB."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.update_item.return_value = {}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Create test persona
        persona = Persona(
            id="test-id",
            name="Updated User",
            age=31,
            occupation="Senior Engineer",
            background="Updated background",
            values=["value1", "value2", "value3"],
            pain_points=["pain1"],
            goals=["goal1", "goal2", "goal3"],
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            updated_at=datetime(2024, 1, 3, 12, 0, 0),
        )

        # Update persona
        result = service.update_persona(persona)

        # 編集は部分更新（BR6.7）。項目全体を置き換える put_item は使わない
        mock_client.put_item.assert_not_called()
        mock_client.update_item.assert_called_once()
        call_args = mock_client.update_item.call_args
        assert call_args[1]["TableName"] == "Test_Personas"
        assert call_args[1]["ConditionExpression"] == "attribute_exists(id)"
        written = set(call_args[1]["ExpressionAttributeNames"].values())
        assert not written & {"owner_sub", "shared_group_ids", "avatar_path"}

        # Verify result is True
        assert result is True

    @patch("boto3.client")
    def test_delete_persona_returning_old(self, mock_boto3_client):
        """Test deleting a persona returns the item removed by the same write."""
        from boto3.dynamodb.types import TypeSerializer

        from src.models.persona import Persona

        persona = Persona.create_new(
            name="n",
            age=30,
            occupation="o",
            background="b",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client
        service = DatabaseService(table_prefix="Test")
        mock_client.delete_item.return_value = {
            "Attributes": service._serialize_persona(persona)
        }

        deleted = service.delete_persona_returning_old(persona.id)

        call_args = mock_client.delete_item.call_args
        assert call_args[1]["TableName"] == "Test_Personas"
        assert call_args[1]["Key"] == {"id": TypeSerializer().serialize(persona.id)}
        assert call_args[1]["ReturnValues"] == "ALL_OLD"
        assert deleted == persona

    @patch("boto3.client")
    def test_delete_persona_returning_old_when_absent(self, mock_boto3_client):
        """Test deleting a missing persona returns None."""
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.delete_item.return_value = {}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        assert service.delete_persona_returning_old("missing") is None

    @patch("boto3.client")
    def test_get_persona_found(self, mock_boto3_client):
        """Test retrieving a persona by ID when it exists."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Mock get_item response
        mock_client.get_item.return_value = {
            "Item": {
                "id": {"S": "test-id"},
                "name": {"S": "Test User"},
                "age": {"N": "30"},
                "occupation": {"S": "Engineer"},
                "background": {"S": "Test background"},
                "values": {"L": [{"S": "value1"}, {"S": "value2"}]},
                "pain_points": {"L": [{"S": "pain1"}, {"S": "pain2"}]},
                "goals": {"L": [{"S": "goal1"}, {"S": "goal2"}]},
                "created_at": {"S": "2024-01-01T12:00:00"},
                "updated_at": {"S": "2024-01-02T12:00:00"},
                "type": {"S": "persona"},
            }
        }
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Get persona
        persona = service.get_persona("test-id")

        # Verify get_item was called
        mock_client.get_item.assert_called_once()

        # Verify persona was returned
        assert persona is not None
        assert persona.id == "test-id"
        assert persona.name == "Test User"
        assert persona.age == 30

    @patch("boto3.client")
    def test_get_persona_not_found(self, mock_boto3_client):
        """Test retrieving a persona by ID when it doesn't exist."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.get_item.return_value = {}  # No 'Item' key
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Get persona
        persona = service.get_persona("nonexistent-id")

        # Verify None was returned
        assert persona is None

    @patch("boto3.client")
    def test_scan_all_personas(self, mock_boto3_client):
        """U5: the admin tab reads every persona with a scan (no type GSI)."""
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        mock_client.scan.return_value = {
            "Items": [
                {
                    "id": {"S": "persona-1"},
                    "name": {"S": "User 1"},
                    "age": {"N": "30"},
                    "occupation": {"S": "Engineer"},
                    "background": {"S": "Background 1"},
                    "values": {"L": [{"S": "value1"}]},
                    "pain_points": {"L": [{"S": "pain1"}]},
                    "goals": {"L": [{"S": "goal1"}]},
                    "created_at": {"S": "2024-01-01T12:00:00"},
                    "updated_at": {"S": "2024-01-02T12:00:00"},
                    "type": {"S": "persona"},
                },
                {
                    "id": {"S": "persona-2"},
                    "name": {"S": "User 2"},
                    "age": {"N": "25"},
                    "occupation": {"S": "Designer"},
                    "background": {"S": "Background 2"},
                    "values": {"L": [{"S": "value2"}]},
                    "pain_points": {"L": [{"S": "pain2"}]},
                    "goals": {"L": [{"S": "goal2"}]},
                    "created_at": {"S": "2024-01-01T12:00:00"},
                    "updated_at": {"S": "2024-01-02T12:00:00"},
                    "type": {"S": "persona"},
                },
            ]
        }
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        personas = service.scan_all_personas()

        mock_client.scan.assert_called_once()
        mock_client.query.assert_not_called()
        assert [p.id for p in personas] == ["persona-1", "persona-2"]


def _in_memory_update_item(saved_items: dict, deserializer):
    """A tiny ``update_item`` for the in-memory mocks (SET / REMOVE, BR6.7).

    ``update_persona`` writes a partial update instead of ``put_item``. Returns
    the error a real table gives when the item is missing.
    """

    def update_item(**kwargs):
        item_id = deserializer.deserialize(kwargs["Key"]["id"])
        if item_id not in saved_items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
            )
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        expression = kwargs["UpdateExpression"]
        set_part, _, remove_part = expression.partition(" REMOVE ")
        item = dict(saved_items[item_id])
        for assignment in set_part.removeprefix("SET ").split(", "):
            name, _, placeholder = assignment.partition(" = ")
            item[names.get(name, name)] = values[placeholder]
        for name in filter(None, remove_part.split(", ")):
            item.pop(names.get(name, name), None)
        saved_items[item_id] = item
        return {}

    return update_item


class TestPersonaCRUDOperationsProperties:
    """
    Property-based tests for Persona CRUD operations.
    **Feature: dynamodb-migration, Property 3: CRUD operation equivalence**
    **Validates: Requirements 2.2, 2.4**
    """

    @patch("boto3.client")
    @given(persona=_persona_strategy())
    @settings(max_examples=100, deadline=None)
    def test_persona_crud_cycle(self, mock_boto3_client, persona):
        """
        Property test: For any valid Persona, performing a create-read-update-delete cycle
        should work correctly - saved data can be retrieved, updated, and deleted.

        This tests that:
        1. Saving a persona returns the persona ID
        2. Reading the saved persona returns equivalent data
        3. Updating the persona succeeds
        4. Reading the updated persona returns the new data
        5. Deleting the persona succeeds
        6. Reading after delete returns None

        **Feature: dynamodb-migration, Property 3: CRUD operation equivalence**
        **Validates: Requirements 2.2, 2.4**
        """
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Track saved items in memory to simulate DynamoDB behavior
        saved_items = {}

        def mock_put_item(**kwargs):
            """Mock put_item to store items in memory."""
            item = kwargs["Item"]
            # Extract ID from the serialized item
            item_id = mock_client._deserializer.deserialize(item["id"])
            saved_items[item_id] = item
            return {}

        def mock_get_item(**kwargs):
            """Mock get_item to retrieve items from memory."""
            key = kwargs["Key"]
            item_id = mock_client._deserializer.deserialize(key["id"])
            if item_id in saved_items:
                return {"Item": saved_items[item_id]}
            return {}

        def mock_delete_item(**kwargs):
            """Mock delete_item to remove items from memory."""
            key = kwargs["Key"]
            item_id = mock_client._deserializer.deserialize(key["id"])
            old = saved_items.pop(item_id, None)
            if old is not None and kwargs.get("ReturnValues") == "ALL_OLD":
                return {"Attributes": old}
            return {}

        # Set up deserializer for mock client
        from boto3.dynamodb.types import TypeDeserializer

        mock_client._deserializer = TypeDeserializer()

        # Configure mock methods
        mock_client.put_item = Mock(side_effect=mock_put_item)
        mock_client.get_item = Mock(side_effect=mock_get_item)
        mock_client.delete_item = Mock(side_effect=mock_delete_item)
        mock_client.update_item = Mock(
            side_effect=_in_memory_update_item(saved_items, mock_client._deserializer)
        )

        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Step 1: CREATE - Save the persona
        saved_id = service.save_persona(persona)
        assert saved_id == persona.id, "Save should return the persona ID"

        # Step 2: READ - Retrieve the saved persona
        retrieved = service.get_persona(persona.id)
        assert retrieved is not None, "Saved persona should be retrievable"
        assert retrieved.id == persona.id
        assert retrieved.name == persona.name
        assert retrieved.age == persona.age
        assert retrieved.occupation == persona.occupation
        assert retrieved.background == persona.background
        assert retrieved.values == persona.values
        assert retrieved.pain_points == persona.pain_points
        assert retrieved.goals == persona.goals
        assert retrieved.created_at == persona.created_at
        assert retrieved.updated_at == persona.updated_at

        # Step 3: UPDATE - Modify the persona
        updated_persona = persona.update(
            name=persona.name + " Updated", age=persona.age + 1
        )
        update_result = service.update_persona(updated_persona)
        assert update_result is True, "Update should succeed"

        # Step 4: READ - Retrieve the updated persona
        retrieved_updated = service.get_persona(persona.id)
        assert retrieved_updated is not None, "Updated persona should be retrievable"
        assert retrieved_updated.name == persona.name + " Updated"
        assert retrieved_updated.age == persona.age + 1
        # Other fields should remain the same
        assert retrieved_updated.occupation == persona.occupation
        assert retrieved_updated.background == persona.background

        # Step 5: DELETE - Remove the persona
        deleted = service.delete_persona_returning_old(persona.id)
        assert deleted is not None, "Delete should succeed"
        assert deleted.id == persona.id

        # Step 6: READ - Verify persona is deleted
        retrieved_after_delete = service.get_persona(persona.id)
        assert retrieved_after_delete is None, (
            "Deleted persona should not be retrievable"
        )

    @patch("boto3.client")
    @given(persona1=_persona_strategy(), persona2=_persona_strategy())
    @settings(max_examples=50, deadline=None)
    def test_multiple_personas_crud(self, mock_boto3_client, persona1, persona2):
        """
        Property test: For any two valid Personas, CRUD operations should work
        independently without interference.

        This tests that:
        1. Multiple personas can be saved
        2. Each persona can be retrieved independently
        3. Updating one persona doesn't affect the other
        4. Deleting one persona doesn't affect the other

        **Feature: dynamodb-migration, Property 3: CRUD operation equivalence**
        **Validates: Requirements 2.2, 2.4**
        """
        # Ensure personas have different IDs
        if persona1.id == persona2.id:
            persona2 = Persona(
                id=persona1.id + "_different",
                name=persona2.name,
                age=persona2.age,
                occupation=persona2.occupation,
                background=persona2.background,
                values=persona2.values,
                pain_points=persona2.pain_points,
                goals=persona2.goals,
                created_at=persona2.created_at,
                updated_at=persona2.updated_at,
            )

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Track saved items in memory
        saved_items = {}

        def mock_put_item(**kwargs):
            item = kwargs["Item"]
            from boto3.dynamodb.types import TypeDeserializer

            deserializer = TypeDeserializer()
            item_id = deserializer.deserialize(item["id"])
            saved_items[item_id] = item
            return {}

        def mock_get_item(**kwargs):
            key = kwargs["Key"]
            from boto3.dynamodb.types import TypeDeserializer

            deserializer = TypeDeserializer()
            item_id = deserializer.deserialize(key["id"])
            if item_id in saved_items:
                return {"Item": saved_items[item_id]}
            return {}

        def mock_delete_item(**kwargs):
            key = kwargs["Key"]
            from boto3.dynamodb.types import TypeDeserializer

            deserializer = TypeDeserializer()
            item_id = deserializer.deserialize(key["id"])
            old = saved_items.pop(item_id, None)
            if old is not None and kwargs.get("ReturnValues") == "ALL_OLD":
                return {"Attributes": old}
            return {}

        mock_client.put_item = Mock(side_effect=mock_put_item)
        mock_client.get_item = Mock(side_effect=mock_get_item)
        mock_client.delete_item = Mock(side_effect=mock_delete_item)
        from boto3.dynamodb.types import TypeDeserializer

        mock_client.update_item = Mock(
            side_effect=_in_memory_update_item(saved_items, TypeDeserializer())
        )

        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Save both personas
        service.save_persona(persona1)
        service.save_persona(persona2)

        # Retrieve both personas independently
        retrieved1 = service.get_persona(persona1.id)
        retrieved2 = service.get_persona(persona2.id)

        assert retrieved1 is not None
        assert retrieved2 is not None
        assert retrieved1.id == persona1.id
        assert retrieved2.id == persona2.id
        assert retrieved1.name == persona1.name
        assert retrieved2.name == persona2.name

        # Update persona1
        updated_persona1 = persona1.update(name=persona1.name + " Modified")
        service.update_persona(updated_persona1)

        # Verify persona1 is updated but persona2 is unchanged
        retrieved1_after = service.get_persona(persona1.id)
        retrieved2_after = service.get_persona(persona2.id)

        assert retrieved1_after.name == persona1.name + " Modified"
        assert retrieved2_after.name == persona2.name  # Should be unchanged

        # Delete persona1
        service.delete_persona_returning_old(persona1.id)

        # Verify persona1 is deleted but persona2 still exists
        retrieved1_deleted = service.get_persona(persona1.id)
        retrieved2_still_exists = service.get_persona(persona2.id)

        assert retrieved1_deleted is None
        assert retrieved2_still_exists is not None
        assert retrieved2_still_exists.id == persona2.id


# Knowledge bases are KnowledgeBaseStore (tests/integration/test_knowledge_base_store.py),
# KB bindings are PersonaBindings (tests/integration/test_persona_binding_store.py)
# and upload metadata is UploadedFileStore (tests/integration/test_uploaded_file_store.py).


# Job records are written by JobStore (tests/integration/test_job_store.py).


# =============================================================================
# 所有者と共有先の列（v1.0.0 U4。BR6.1、BR6.7、NFR1.15）。moto で条件と部分更新を確かめる
# =============================================================================


@pytest.fixture
def moto_service(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        service = DatabaseService(table_prefix="OwnerTest", region="us-east-1")
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=service.personas_table,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield service


def _new_persona(**kwargs) -> Persona:
    return Persona.create_new(
        name="佐藤 太郎",
        age=35,
        occupation="会社員",
        background="背景",
        values=["v"],
        pain_points=["p"],
        goals=["g"],
        **kwargs,
    )


def _raw_item(service: DatabaseService, persona_id: str) -> dict:
    return service.dynamodb_client.get_item(
        TableName=service.personas_table, Key={"id": {"S": persona_id}}
    )["Item"]


class TestPersonaOwnerColumns:
    def test_save_writes_owner_sub_but_not_shares(self, moto_service):
        persona = _new_persona().owned_by("sub-a")
        persona = Persona.from_dict(
            {**persona.to_dict(), "shared_group_ids": ["sales"]}
        )

        moto_service.save_persona(persona)

        item = _raw_item(moto_service, persona.id)
        assert item["owner_sub"] == {"S": "sub-a"}
        assert "shared_group_ids" not in item

    def test_deserialize_reads_owner_and_shares(self, moto_service):
        persona = _new_persona().owned_by("sub-a")
        moto_service.save_persona(persona)
        moto_service.dynamodb_client.update_item(
            TableName=moto_service.personas_table,
            Key={"id": {"S": persona.id}},
            UpdateExpression="SET shared_group_ids = :s",
            ExpressionAttributeValues={":s": {"SS": ["sales", "plan"]}},
        )

        stored = moto_service.get_persona(persona.id, consistent=True)

        assert stored.owner_sub == "sub-a"
        assert stored.shared_group_ids == frozenset({"sales", "plan"})

    def test_update_is_partial_and_keeps_owner_shares_and_avatar(self, moto_service):
        persona = _new_persona().owned_by("sub-a").with_avatar("s3://a.webp")
        moto_service.save_persona(persona)
        moto_service.dynamodb_client.update_item(
            TableName=moto_service.personas_table,
            Key={"id": {"S": persona.id}},
            UpdateExpression="SET shared_group_ids = :s",
            ExpressionAttributeValues={":s": {"SS": ["sales"]}},
        )
        # 編集画面から届いた値（所有者と共有先を持たない古い形）で更新する
        edited = Persona.from_dict(
            {
                **{
                    k: v
                    for k, v in persona.to_dict().items()
                    if k not in ("owner_sub", "avatar_path")
                },
                "name": "編集後",
                "gender": None,
            }
        )

        assert moto_service.update_persona(edited) is True

        item = _raw_item(moto_service, persona.id)
        assert item["name"] == {"S": "編集後"}
        assert item["owner_sub"] == {"S": "sub-a"}
        assert item["shared_group_ids"] == {"SS": ["sales"]}
        assert item["avatar_path"] == {"S": "s3://a.webp"}

    def test_update_clears_emptied_optional_fields(self, moto_service):
        persona = _new_persona(gender="female", city="大阪").owned_by("sub-a")
        moto_service.save_persona(persona)

        moto_service.update_persona(persona.update(gender="", city=""))

        item = _raw_item(moto_service, persona.id)
        assert "gender" not in item and "city" not in item

    def test_update_of_a_missing_persona_returns_false(self, moto_service):
        assert moto_service.update_persona(_new_persona()) is False
        with pytest.raises(KeyError):
            _raw_item(moto_service, "never-created")

    def test_get_personas_by_ids_reads_consistently(self, moto_service):
        personas = [_new_persona().owned_by("sub-a") for _ in range(3)]
        for p in personas:
            moto_service.save_persona(p)

        found = moto_service.get_personas_by_ids([p.id for p in personas] + ["nope"])

        assert set(found) == {p.id for p in personas}
        assert found[personas[0].id].owner_sub == "sub-a"

    def test_saving_an_existing_id_fails_and_keeps_the_body_and_the_index(
        self, moto_service
    ):
        """R-02: a new save never overwrites a body (BR6.1 owner, BR1 shares)."""
        from src.managers.persona_manager import PersonaManager, PersonaManagerError
        from src.models.authz import Principal, ResourceType, Role
        from src.models.errors import ErrorCode
        from src.services.resource_share_store import ResourceShareStore
        from tests.error_helpers import raises_code

        client = moto_service.dynamodb_client
        client.create_table(
            TableName="OwnerTest_ResourceShares",
            KeySchema=[
                {"AttributeName": "group_id", "KeyType": "HASH"},
                {"AttributeName": "share_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "group_id", "AttributeType": "S"},
                {"AttributeName": "share_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        shares = ResourceShareStore(
            region="us-east-1",
            shares_table="OwnerTest_ResourceShares",
            client=client,
            sleep=lambda _s: None,
        )
        original = _new_persona().owned_by("sub-a")
        moto_service.save_persona(original)
        shares.write_shares(
            moto_service.personas_table,
            ResourceType.PERSONA,
            original.id,
            expected=frozenset(),
            new=frozenset({"sales"}),
            created_at=_raw_item(moto_service, original.id)["created_at"]["S"],
            owner_sub="sub-a",
        )
        body_before = _raw_item(moto_service, original.id)
        index_before = client.scan(TableName="OwnerTest_ResourceShares")["Items"]

        # Service: the same id cannot be written as a new item
        with pytest.raises(DatabaseError):
            moto_service.save_persona(original.update(name="上書き"))

        # Manager: another user's save of the same id is the existing failure
        manager = PersonaManager(
            database_service=moto_service,
            s3_service=Mock(),
            resource_sharing=Mock(),
            owners=Mock(),
        )
        saver = Principal("sub-b", frozenset({Role.MEMBER}), frozenset({"sales"}))
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.save_persona(saver, original.update(name="上書き"))

        assert _raw_item(moto_service, original.id) == body_before
        assert body_before["owner_sub"] == {"S": "sub-a"}
        assert body_before["shared_group_ids"] == {"SS": ["sales"]}
        assert (
            client.scan(TableName="OwnerTest_ResourceShares")["Items"] == index_before
        )
        assert len(index_before) == 1


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

    tree = ast.parse(inspect.getsource(module))
    source = inspect.getsource(module).splitlines()
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            functions[node.name] = "\n".join(source[node.lineno - 1 : node.end_lineno])
    return functions


class TestNoTypeFixedIndexReads:
    """Personas・UploadedFiles の type 固定の GSI を読むコードがない（BR8.2）。

    Discussions の CreatedAtIndex・ModeIndex は U6 の担当のため対象外。
    """

    def test_persona_and_file_reads_do_not_use_type_fixed_indexes(self):
        for name, body in _database_service_functions().items():
            if "personas_table" in body or "files_table" in body:
                for index in _PERSONA_FILE_GSIS:
                    assert index not in body, f"{name} reads {index}"

    def test_persona_only_indexes_are_not_named_anywhere(self):
        import inspect

        import src.services.database_service as module

        source = inspect.getsource(module)
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
        ],
    )
    def test_unused_and_legacy_reads_are_gone(self, method):
        assert not hasattr(DatabaseService, method)

    def test_scan_of_every_persona_is_the_admin_tab_only(self, moto_service):
        """admin の「すべて」タブの Scan（U4 BR3.5）は残る。"""
        first = _new_persona()
        second = _new_persona()
        moto_service.save_persona(first)
        moto_service.save_persona(second)

        scanned = moto_service.scan_all_personas()

        assert {p.id for p in scanned} == {first.id, second.id}


@pytest.fixture
def moto_dataset_service(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        service = DatabaseService(table_prefix="DsTest", region="us-east-1")
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=service.datasets_table,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield service


class TestDedicatedDatasets:
    """ペルソナ専用のデータセット（BR6.1、BR6.2）。"""

    def _dataset(self, **kwargs):
        from src.models.dataset import Dataset, DatasetColumn

        return Dataset.create_new(
            name="n",
            description="",
            s3_path="s3://b/k.csv",
            columns=[DatasetColumn(name="user_id", data_type="string")],
            row_count=1,
            **kwargs,
        )

    def test_owner_persona_id_is_saved_and_read(self, moto_dataset_service):
        dataset = self._dataset(owner_persona_id="p1")
        moto_dataset_service.save_dataset(dataset)

        stored = moto_dataset_service.get_dataset(dataset.id)
        assert stored.owner_persona_id == "p1"

    def test_org_assets_are_saved_without_the_column(self, moto_dataset_service):
        dataset = self._dataset()
        moto_dataset_service.save_dataset(dataset)
        item = moto_dataset_service.dynamodb_client.get_item(
            TableName=moto_dataset_service.datasets_table,
            Key={"id": {"S": dataset.id}},
        )["Item"]
        assert "owner_persona_id" not in item

    def test_listing_leaves_out_dedicated_datasets(self, moto_dataset_service):
        org = self._dataset()
        dedicated = self._dataset(owner_persona_id="p1")
        moto_dataset_service.save_dataset(org)
        moto_dataset_service.save_dataset(dedicated)

        listed = moto_dataset_service.get_all_datasets()

        assert [d.id for d in listed] == [org.id]

    def test_listing_filters_in_the_scan(self, moto_dataset_service):
        spy = Mock(wraps=moto_dataset_service.dynamodb_client.scan)
        moto_dataset_service.dynamodb_client.scan = spy
        moto_dataset_service.get_all_datasets()
        assert (
            "attribute_not_exists(owner_persona_id)"
            in spy.call_args.kwargs["FilterExpression"]
        )

    def test_dedicated_dataset_is_deleted(self, moto_dataset_service):
        dataset = self._dataset(owner_persona_id="p1")
        moto_dataset_service.save_dataset(dataset)
        assert moto_dataset_service.delete_dataset(dataset.id)
        assert moto_dataset_service.get_dataset(dataset.id) is None


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
            if name in {"__init__", "_get_table_name"}:
                continue
            assert "self.discussions_table" not in body or name in {
                "check_database_health",
                "initialize_database",
                "get_database_info",
            }, f"{name} still touches Discussions"


# =============================================================================
# U7: 組織資産の部分更新と利用範囲（BR1.5、BR5.2、BR5.6、NFR1.77、NFR1.87）
# =============================================================================


@pytest.fixture
def moto_asset_service(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        service = DatabaseService(table_prefix="AsTest", region="us-east-1")
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=service.datasets_table,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield service


def _org_dataset(**kwargs):
    from src.models.dataset import Dataset, DatasetColumn

    return Dataset.create_new(
        name="売上",
        description="説明",
        s3_path="s3://b/datasets/x.csv",
        columns=[DatasetColumn(name="a", data_type="string")],
        row_count=3,
        **kwargs,
    )


def _item(service, table, item_id):
    return service.dynamodb_client.get_item(
        TableName=table, Key={"id": {"S": item_id}}
    ).get("Item")


class TestAssetScope:
    def test_add_then_remove_as_two_set_updates(self, moto_asset_service):
        asset = _org_dataset()
        moto_asset_service.save_dataset(asset)
        spy = Mock(wraps=moto_asset_service.dynamodb_client.update_item)
        moto_asset_service.dynamodb_client.update_item = spy

        moto_asset_service.update_asset_scope(
            "dataset", asset.id, add=frozenset({"sales", "plan"}), remove=frozenset()
        )
        moto_asset_service.update_asset_scope(
            "dataset", asset.id, add=frozenset({"dev"}), remove=frozenset({"plan"})
        )

        expressions = [c.kwargs["UpdateExpression"] for c in spy.call_args_list]
        assert len(expressions) == 3
        assert expressions[1].startswith("ADD") and expressions[2].startswith("DELETE")
        table = moto_asset_service.datasets_table
        assert set(
            _item(moto_asset_service, table, asset.id)["shared_group_ids"]["SS"]
        ) == {
            "sales",
            "dev",
        }

    def test_removing_every_department_drops_the_column(self, moto_asset_service):
        dataset = _org_dataset()
        moto_asset_service.save_dataset(dataset)
        moto_asset_service.update_asset_scope(
            "dataset", dataset.id, add=frozenset({"sales"}), remove=frozenset()
        )
        moto_asset_service.update_asset_scope(
            "dataset", dataset.id, add=frozenset(), remove=frozenset({"sales"})
        )
        stored = moto_asset_service.get_dataset(dataset.id, consistent=True)
        assert stored.shared_group_ids == frozenset()

    def test_empty_change_sends_nothing(self, moto_asset_service):
        spy = Mock(wraps=moto_asset_service.dynamodb_client.update_item)
        moto_asset_service.dynamodb_client.update_item = spy
        moto_asset_service.update_asset_scope(
            "dataset", "any", add=frozenset(), remove=frozenset()
        )
        assert spy.call_count == 0

    def test_missing_asset_creates_nothing(self, moto_asset_service):
        from src.services.database_service import DatabaseConditionFailed

        with pytest.raises(DatabaseConditionFailed):
            moto_asset_service.update_asset_scope(
                "dataset", "gone", add=frozenset({"sales"}), remove=frozenset()
            )
        assert (
            _item(moto_asset_service, moto_asset_service.datasets_table, "gone") is None
        )

    def test_persona_dedicated_dataset_is_refused(self, moto_asset_service):
        from src.services.database_service import DatabaseConditionFailed

        dedicated = _org_dataset(owner_persona_id="p1")
        moto_asset_service.save_dataset(dedicated)
        with pytest.raises(DatabaseConditionFailed):
            moto_asset_service.update_asset_scope(
                "dataset", dedicated.id, add=frozenset({"sales"}), remove=frozenset()
            )
        assert "shared_group_ids" not in _item(
            moto_asset_service, moto_asset_service.datasets_table, dedicated.id
        )

    @pytest.mark.parametrize("kind", ["persona", "knowledge_base"])
    def test_unknown_kind_is_a_caller_bug(self, moto_asset_service, kind):
        """Knowledge bases are scoped by KnowledgeBaseStore.update_scope."""
        with pytest.raises(ValueError):
            moto_asset_service.update_asset_scope(
                kind, "x", add=frozenset({"a"}), remove=frozenset()
            )


class TestAssetPartialUpdates:
    def test_dataset_update_keeps_scope(self, moto_asset_service):
        from src.models.dataset import DatasetColumn

        dataset = _org_dataset()
        moto_asset_service.save_dataset(dataset)
        moto_asset_service.update_asset_scope(
            "dataset", dataset.id, add=frozenset({"sales"}), remove=frozenset()
        )
        spy = Mock(wraps=moto_asset_service.dynamodb_client.put_item)
        moto_asset_service.dynamodb_client.put_item = spy

        assert moto_asset_service.update_dataset_fields(
            dataset.id,
            name="新しい名前",
            description="d2",
            notes="n2",
            columns=[DatasetColumn(name="b", data_type="integer")],
            updated_at=datetime(2030, 1, 1),
        )

        assert spy.call_count == 0
        stored = moto_asset_service.get_dataset(dataset.id, consistent=True)
        assert stored.name == "新しい名前"
        assert stored.notes == "n2"
        assert stored.columns[0].name == "b"
        assert stored.shared_group_ids == frozenset({"sales"})
        assert stored.created_at == dataset.created_at

    def test_dataset_update_only_given_columns(self, moto_asset_service):
        dataset = _org_dataset()
        moto_asset_service.save_dataset(dataset)
        moto_asset_service.update_dataset_fields(
            dataset.id, name="x", updated_at=datetime(2030, 1, 1)
        )
        stored = moto_asset_service.get_dataset(dataset.id)
        assert stored.description == "説明"

    def test_dataset_update_of_a_deleted_dataset_is_false(self, moto_asset_service):
        assert (
            moto_asset_service.update_dataset_fields(
                "gone", name="x", updated_at=datetime(2030, 1, 1)
            )
            is False
        )
        assert (
            _item(moto_asset_service, moto_asset_service.datasets_table, "gone") is None
        )

    def test_save_dataset_never_overwrites(self, moto_asset_service):
        dataset = _org_dataset()
        moto_asset_service.save_dataset(dataset)
        moto_asset_service.update_asset_scope(
            "dataset", dataset.id, add=frozenset({"sales"}), remove=frozenset()
        )
        with pytest.raises(DatabaseError):
            moto_asset_service.save_dataset(dataset)
        assert moto_asset_service.get_dataset(dataset.id).shared_group_ids == frozenset(
            {"sales"}
        )

    def test_scope_change_and_rename_both_survive(self, moto_asset_service):
        dataset = _org_dataset()
        moto_asset_service.save_dataset(dataset)
        moto_asset_service.update_asset_scope(
            "dataset", dataset.id, add=frozenset({"sales"}), remove=frozenset()
        )
        moto_asset_service.update_dataset_fields(
            dataset.id, name="改名", updated_at=datetime(2030, 1, 1)
        )
        stored = moto_asset_service.get_dataset(dataset.id)
        assert stored.name == "改名"
        assert stored.shared_group_ids == frozenset({"sales"})


class TestAssetListings:
    def test_dataset_listing_reads_scope_and_leaves_out_dedicated(
        self, moto_asset_service
    ):
        org = _org_dataset()
        moto_asset_service.save_dataset(org)
        moto_asset_service.save_dataset(_org_dataset(owner_persona_id="p1"))
        moto_asset_service.update_asset_scope(
            "dataset", org.id, add=frozenset({"plan"}), remove=frozenset()
        )
        listed = moto_asset_service.get_all_datasets()
        assert [d.id for d in listed] == [org.id]
        assert listed[0].shared_group_ids == frozenset({"plan"})
        assert isinstance(listed[0].shared_group_ids, frozenset)
