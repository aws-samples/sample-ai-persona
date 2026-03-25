"""
Unit tests for DatabaseService (DynamoDB).
Tests initialization, connection management, and retry logic.
"""

import pytest
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError, NoCredentialsError
from datetime import datetime, timedelta

# hypothesisモジュールがない場合はテストをスキップ
hypothesis = pytest.importorskip(
    "hypothesis", reason="hypothesis is required for property-based tests"
)
from hypothesis import given, strategies as st
from hypothesis import settings
from src.services.database_service import DatabaseService, DatabaseError
from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.models.insight import Insight


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

    @patch("boto3.client")
    def test_serialize_discussion(self, mock_boto3_client):
        """Test discussion serialization to DynamoDB format."""
        from datetime import datetime
        from src.models.discussion import Discussion
        from src.models.message import Message
        from src.models.insight import Insight

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create test discussion with messages and insights
        message = Message(
            persona_id="persona-1",
            persona_name="Test Persona",
            content="Test message",
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            message_type="statement",
            round_number=1,
        )

        insight = Insight(
            category="test-category",
            description="Test insight",
            supporting_messages=["msg-1"],
            confidence_score=0.9,
        )

        discussion = Discussion(
            id="discussion-id",
            topic="Test Topic",
            participants=["persona-1", "persona-2"],
            messages=[message],
            insights=[insight],
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            mode="agent",
            agent_config={"rounds": 3},
        )

        # Serialize
        serialized = service._serialize_discussion(discussion)

        # Verify structure
        assert "id" in serialized
        assert "topic" in serialized
        assert "participants" in serialized
        assert "messages" in serialized
        assert "insights" in serialized
        assert "mode" in serialized
        assert "agent_config" in serialized
        assert "type" in serialized

        # Verify type descriptors
        assert "S" in serialized["id"]
        assert "L" in serialized["participants"]
        assert "L" in serialized["messages"]

    @patch("boto3.client")
    def test_deserialize_discussion(self, mock_boto3_client):
        """Test discussion deserialization from DynamoDB format."""

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create DynamoDB format item
        item = {
            "id": {"S": "discussion-id"},
            "topic": {"S": "Test Topic"},
            "participants": {"L": [{"S": "persona-1"}, {"S": "persona-2"}]},
            "messages": {
                "L": [
                    {
                        "M": {
                            "persona_id": {"S": "persona-1"},
                            "persona_name": {"S": "Test Persona"},
                            "content": {"S": "Test message"},
                            "timestamp": {"S": "2024-01-01T12:00:00"},
                            "message_type": {"S": "statement"},
                            "round_number": {"N": "1"},
                        }
                    }
                ]
            },
            "insights": {
                "L": [
                    {
                        "M": {
                            "category": {"S": "test-category"},
                            "description": {"S": "Test insight"},
                            "supporting_messages": {"L": [{"S": "msg-1"}]},
                            "confidence_score": {"N": "0.9"},
                        }
                    }
                ]
            },
            "created_at": {"S": "2024-01-01T12:00:00"},
            "mode": {"S": "agent"},
            "agent_config": {"M": {"rounds": {"N": "3"}}},
            "type": {"S": "discussion"},
        }

        # Deserialize
        discussion = service._deserialize_discussion(item)

        # Verify discussion object
        assert discussion.id == "discussion-id"
        assert discussion.topic == "Test Topic"
        assert discussion.participants == ["persona-1", "persona-2"]
        assert len(discussion.messages) == 1
        assert discussion.messages[0].persona_id == "persona-1"
        assert discussion.messages[0].round_number == 1
        assert len(discussion.insights) == 1
        assert discussion.insights[0].category == "test-category"
        assert discussion.mode == "agent"
        assert discussion.agent_config == {"rounds": 3}

    @patch("boto3.client")
    def test_serialize_file_info(self, mock_boto3_client):
        """Test file info serialization to DynamoDB format."""
        from datetime import datetime

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Serialize file info
        serialized = service._serialize_file_info(
            file_id="file-id",
            filename="test.txt",
            file_path="/path/to/test.txt",
            file_size=1024,
            file_hash="abc123",
            mime_type="text/plain",
            uploaded_at=datetime(2024, 1, 1, 12, 0, 0),
        )

        # Verify structure
        assert "id" in serialized
        assert "filename" in serialized
        assert "file_path" in serialized
        assert "file_size" in serialized
        assert "file_hash" in serialized
        assert "mime_type" in serialized
        assert "uploaded_at" in serialized
        assert "type" in serialized

        # Verify type descriptors
        assert "S" in serialized["id"]
        assert "N" in serialized["file_size"]

    @patch("boto3.client")
    def test_deserialize_file_info(self, mock_boto3_client):
        """Test file info deserialization from DynamoDB format."""
        from datetime import datetime

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create DynamoDB format item
        item = {
            "id": {"S": "file-id"},
            "filename": {"S": "test.txt"},
            "original_filename": {"S": "test.txt"},
            "file_path": {"S": "/path/to/test.txt"},
            "file_size": {"N": "1024"},
            "file_hash": {"S": "abc123"},
            "mime_type": {"S": "text/plain"},
            "uploaded_at": {"S": "2024-01-01T12:00:00"},
            "type": {"S": "file"},
        }

        # Deserialize
        file_info = service._deserialize_file_info(item)

        # Verify file info dictionary
        assert file_info["id"] == "file-id"
        assert file_info["filename"] == "test.txt"
        assert file_info["file_path"] == "/path/to/test.txt"
        assert file_info["file_size"] == 1024
        assert file_info["file_hash"] == "abc123"
        assert file_info["mime_type"] == "text/plain"
        assert file_info["uploaded_at"] == datetime(2024, 1, 1, 12, 0, 0)

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
    def test_round_trip_discussion_serialization(self, mock_boto3_client):
        """Test round-trip serialization/deserialization for discussion."""
        from datetime import datetime
        from src.models.discussion import Discussion
        from src.models.message import Message
        from src.models.insight import Insight

        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Create original discussion
        message = Message(
            persona_id="persona-1",
            persona_name="Test Persona",
            content="Test message",
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            message_type="statement",
            round_number=1,
        )

        insight = Insight(
            category="test-category",
            description="Test insight",
            supporting_messages=["msg-1"],
            confidence_score=0.9,
        )

        original = Discussion(
            id="discussion-id",
            topic="Test Topic",
            participants=["persona-1", "persona-2"],
            messages=[message],
            insights=[insight],
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            mode="agent",
            agent_config={"rounds": 3},
        )

        # Serialize then deserialize
        serialized = service._serialize_discussion(original)
        deserialized = service._deserialize_discussion(serialized)

        # Verify round-trip preserves data
        assert deserialized.id == original.id
        assert deserialized.topic == original.topic
        assert deserialized.participants == original.participants
        assert len(deserialized.messages) == len(original.messages)
        assert deserialized.messages[0].persona_id == original.messages[0].persona_id
        assert deserialized.messages[0].content == original.messages[0].content
        assert len(deserialized.insights) == len(original.insights)
        assert deserialized.insights[0].category == original.insights[0].category
        assert deserialized.mode == original.mode
        assert deserialized.agent_config == original.agent_config


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


def _discussion_strategy():
    """Generate random Discussion objects."""
    return st.builds(
        Discussion,
        id=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        topic=st.text(
            min_size=1,
            max_size=200,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        participants=st.lists(
            st.text(
                min_size=1,
                max_size=50,
                alphabet=st.characters(blacklist_categories=("Cs",)),
            ),
            min_size=1,
            max_size=10,
        ),
        messages=st.lists(_message_strategy(), min_size=0, max_size=20),
        insights=st.lists(_insight_strategy(), min_size=0, max_size=10),
        created_at=st.datetimes(
            min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31)
        ),
        mode=st.sampled_from(["classic", "agent", "traditional"]),
        # agent_config should only contain integers and strings, not floats
        # DynamoDB TypeSerializer doesn't support raw floats in nested structures
        agent_config=st.one_of(
            st.none(),
            st.dictionaries(
                keys=st.text(
                    min_size=1,
                    max_size=20,
                    alphabet=st.characters(blacklist_categories=("Cs",)),
                ),
                values=st.one_of(
                    st.integers(min_value=1, max_value=100),
                    st.text(
                        min_size=1,
                        max_size=50,
                        alphabet=st.characters(blacklist_categories=("Cs",)),
                    ),
                ),
                min_size=0,
                max_size=5,
            ),
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

    @patch("boto3.client")
    @given(discussion=_discussion_strategy())
    @settings(max_examples=100, deadline=None)
    def test_discussion_serialization_round_trip(self, mock_boto3_client, discussion):
        """
        Property test: For any valid Discussion object, serializing to DynamoDB format
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
        serialized = service._serialize_discussion(discussion)
        deserialized = service._deserialize_discussion(serialized)

        # Verify all fields are preserved
        assert deserialized.id == discussion.id
        assert deserialized.topic == discussion.topic
        assert deserialized.participants == discussion.participants
        assert deserialized.created_at == discussion.created_at
        assert deserialized.mode == discussion.mode
        assert deserialized.agent_config == discussion.agent_config

        # Verify messages are preserved
        assert len(deserialized.messages) == len(discussion.messages)
        for orig_msg, deser_msg in zip(discussion.messages, deserialized.messages):
            assert deser_msg.persona_id == orig_msg.persona_id
            assert deser_msg.persona_name == orig_msg.persona_name
            assert deser_msg.content == orig_msg.content
            assert deser_msg.timestamp == orig_msg.timestamp
            assert deser_msg.message_type == orig_msg.message_type
            assert deser_msg.round_number == orig_msg.round_number

        # Verify insights are preserved
        assert len(deserialized.insights) == len(discussion.insights)
        for orig_insight, deser_insight in zip(
            discussion.insights, deserialized.insights
        ):
            assert deser_insight.category == orig_insight.category
            assert deser_insight.description == orig_insight.description
            assert deser_insight.supporting_messages == orig_insight.supporting_messages
            # Use approximate comparison for floats
            assert (
                abs(deser_insight.confidence_score - orig_insight.confidence_score)
                < 0.0001
            )

    @patch("boto3.client")
    @given(
        file_id=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        filename=st.text(
            min_size=1,
            max_size=100,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        file_path=st.text(
            min_size=1,
            max_size=200,
            alphabet=st.characters(blacklist_categories=("Cs",)),
        ),
        file_size=st.one_of(st.none(), st.integers(min_value=0, max_value=1000000000)),
        file_hash=st.one_of(
            st.none(),
            st.text(
                min_size=1,
                max_size=64,
                alphabet=st.characters(blacklist_categories=("Cs",)),
            ),
        ),
        mime_type=st.one_of(
            st.none(),
            st.sampled_from(
                ["text/plain", "application/json", "image/png", "application/pdf"]
            ),
        ),
        uploaded_at=st.datetimes(
            min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31)
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_file_info_serialization_round_trip(
        self,
        mock_boto3_client,
        file_id,
        filename,
        file_path,
        file_size,
        file_hash,
        mime_type,
        uploaded_at,
    ):
        """
        Property test: For any valid file metadata, serializing to DynamoDB format
        and then deserializing should produce an equivalent dictionary.

        **Feature: dynamodb-migration, Property 5: Serialization round-trip**
        **Validates: Requirements 2.5**
        """
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService()

        # Serialize then deserialize
        serialized = service._serialize_file_info(
            file_id=file_id,
            filename=filename,
            file_path=file_path,
            file_size=file_size,
            file_hash=file_hash,
            mime_type=mime_type,
            uploaded_at=uploaded_at,
        )
        deserialized = service._deserialize_file_info(serialized)

        # Verify all fields are preserved
        assert deserialized["id"] == file_id
        assert deserialized["filename"] == filename
        assert deserialized["file_path"] == file_path
        assert deserialized["uploaded_at"] == uploaded_at

        # Verify optional fields
        if file_size is not None:
            assert deserialized["file_size"] == file_size
        if file_hash is not None:
            assert deserialized["file_hash"] == file_hash
        if mime_type is not None:
            assert deserialized["mime_type"] == mime_type


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
        mock_client.put_item.return_value = {}
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

        # Verify put_item was called
        mock_client.put_item.assert_called_once()
        call_args = mock_client.put_item.call_args
        assert call_args[1]["TableName"] == "Test_Personas"

        # Verify result is True
        assert result is True

    @patch("boto3.client")
    def test_delete_persona(self, mock_boto3_client):
        """Test deleting a persona from DynamoDB."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.delete_item.return_value = {}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Delete persona
        result = service.delete_persona("test-id")

        # Verify delete_item was called
        mock_client.delete_item.assert_called_once()
        call_args = mock_client.delete_item.call_args
        assert call_args[1]["TableName"] == "Test_Personas"
        assert "Key" in call_args[1]

        # Verify result is True
        assert result is True

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
    def test_get_all_personas(self, mock_boto3_client):
        """Test retrieving all personas."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Mock scan response with two personas
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

        # Get all personas
        personas = service.get_all_personas()

        # Verify scan was called
        mock_client.scan.assert_called_once()

        # Verify personas were returned
        assert len(personas) == 2
        assert personas[0].id == "persona-1"
        assert personas[1].id == "persona-2"

    @patch("boto3.client")
    def test_persona_exists_true(self, mock_boto3_client):
        """Test checking if a persona exists when it does."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.get_item.return_value = {"Item": {"id": {"S": "test-id"}}}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Check if persona exists
        exists = service.persona_exists("test-id")

        # Verify result
        assert exists is True

    @patch("boto3.client")
    def test_persona_exists_false(self, mock_boto3_client):
        """Test checking if a persona exists when it doesn't."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.get_item.return_value = {}  # No 'Item' key
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Check if persona exists
        exists = service.persona_exists("nonexistent-id")

        # Verify result
        assert exists is False

    @patch("boto3.client")
    def test_get_persona_count(self, mock_boto3_client):
        """Test getting the count of personas."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.scan.return_value = {"Count": 5}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Get persona count
        count = service.get_persona_count()

        # Verify scan was called with Select='COUNT'
        mock_client.scan.assert_called_once()
        call_args = mock_client.scan.call_args
        assert call_args[1]["Select"] == "COUNT"

        # Verify count
        assert count == 5

    @patch("boto3.client")
    def test_get_personas_by_name(self, mock_boto3_client):
        """Test querying personas by name pattern."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Mock scan response
        mock_client.scan.return_value = {
            "Items": [
                {
                    "id": {"S": "persona-1"},
                    "name": {"S": "John Smith"},
                    "age": {"N": "30"},
                    "occupation": {"S": "Engineer"},
                    "background": {"S": "Background"},
                    "values": {"L": [{"S": "value1"}]},
                    "pain_points": {"L": [{"S": "pain1"}]},
                    "goals": {"L": [{"S": "goal1"}]},
                    "created_at": {"S": "2024-01-01T12:00:00"},
                    "updated_at": {"S": "2024-01-02T12:00:00"},
                    "type": {"S": "persona"},
                }
            ]
        }
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Query by name
        personas = service.get_personas_by_name("John")

        # Verify scan was called with filter
        mock_client.scan.assert_called_once()
        call_args = mock_client.scan.call_args
        assert "FilterExpression" in call_args[1]

        # Verify personas were returned
        assert len(personas) == 1
        assert personas[0].name == "John Smith"

    @patch("boto3.client")
    def test_get_personas_by_occupation(self, mock_boto3_client):
        """Test querying personas by occupation pattern."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Mock scan response
        mock_client.scan.return_value = {
            "Items": [
                {
                    "id": {"S": "persona-1"},
                    "name": {"S": "User 1"},
                    "age": {"N": "30"},
                    "occupation": {"S": "Software Engineer"},
                    "background": {"S": "Background"},
                    "values": {"L": [{"S": "value1"}]},
                    "pain_points": {"L": [{"S": "pain1"}]},
                    "goals": {"L": [{"S": "goal1"}]},
                    "created_at": {"S": "2024-01-01T12:00:00"},
                    "updated_at": {"S": "2024-01-02T12:00:00"},
                    "type": {"S": "persona"},
                }
            ]
        }
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Query by occupation
        personas = service.get_personas_by_occupation("Engineer")

        # Verify scan was called with filter
        mock_client.scan.assert_called_once()
        call_args = mock_client.scan.call_args
        assert "FilterExpression" in call_args[1]

        # Verify personas were returned
        assert len(personas) == 1
        assert personas[0].occupation == "Software Engineer"


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
            if item_id in saved_items:
                del saved_items[item_id]
            return {}

        # Set up deserializer for mock client
        from boto3.dynamodb.types import TypeDeserializer

        mock_client._deserializer = TypeDeserializer()

        # Configure mock methods
        mock_client.put_item = Mock(side_effect=mock_put_item)
        mock_client.get_item = Mock(side_effect=mock_get_item)
        mock_client.delete_item = Mock(side_effect=mock_delete_item)

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
        delete_result = service.delete_persona(persona.id)
        assert delete_result is True, "Delete should succeed"

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
            if item_id in saved_items:
                del saved_items[item_id]
            return {}

        mock_client.put_item = Mock(side_effect=mock_put_item)
        mock_client.get_item = Mock(side_effect=mock_get_item)
        mock_client.delete_item = Mock(side_effect=mock_delete_item)

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
        service.delete_persona(persona1.id)

        # Verify persona1 is deleted but persona2 still exists
        retrieved1_deleted = service.get_persona(persona1.id)
        retrieved2_still_exists = service.get_persona(persona2.id)

        assert retrieved1_deleted is None
        assert retrieved2_still_exists is not None
        assert retrieved2_still_exists.id == persona2.id


class TestDiscussionCRUDOperations:
    """Test Discussion CRUD operations in DatabaseService."""

    @patch("boto3.client")
    def test_save_discussion(self, mock_boto3_client):
        """Test saving a discussion to DynamoDB."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.put_item.return_value = {}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Create test discussion
        discussion = Discussion.create_new(
            topic="Test Topic", participants=["persona1", "persona2"]
        )

        # Save discussion
        result = service.save_discussion(discussion)

        # Verify result
        assert result == discussion.id

        # Verify put_item was called
        mock_client.put_item.assert_called_once()
        call_args = mock_client.put_item.call_args
        assert call_args[1]["TableName"] == "Test_Discussions"
        assert "Item" in call_args[1]

    @patch("boto3.client")
    def test_get_discussion_found(self, mock_boto3_client):
        """Test retrieving an existing discussion."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Create test discussion
        discussion = Discussion.create_new(
            topic="Test Topic", participants=["persona1", "persona2"]
        )

        # Mock get_item to return serialized discussion
        service = DatabaseService(table_prefix="Test")
        serialized = service._serialize_discussion(discussion)
        mock_client.get_item.return_value = {"Item": serialized}
        mock_boto3_client.return_value = mock_client

        # Re-initialize service with mocked client
        service = DatabaseService(table_prefix="Test")

        # Get discussion
        result = service.get_discussion(discussion.id)

        # Verify result
        assert result is not None
        assert result.id == discussion.id
        assert result.topic == discussion.topic
        assert result.participants == discussion.participants

        # Verify get_item was called
        mock_client.get_item.assert_called_once()

    @patch("boto3.client")
    def test_get_discussion_not_found(self, mock_boto3_client):
        """Test retrieving a non-existent discussion."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.get_item.return_value = {}  # No Item in response
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Get non-existent discussion
        result = service.get_discussion("non-existent-id")

        # Verify result is None
        assert result is None

    @patch("boto3.client")
    def test_get_discussions(self, mock_boto3_client):
        """Test retrieving all discussions."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Create test discussions
        discussion1 = Discussion.create_new(topic="Topic 1", participants=["persona1"])
        discussion2 = Discussion.create_new(topic="Topic 2", participants=["persona2"])

        # Mock scan to return serialized discussions
        service = DatabaseService(table_prefix="Test")
        serialized1 = service._serialize_discussion(discussion1)
        serialized2 = service._serialize_discussion(discussion2)
        mock_client.scan.return_value = {"Items": [serialized1, serialized2]}
        mock_boto3_client.return_value = mock_client

        # Re-initialize service with mocked client
        service = DatabaseService(table_prefix="Test")

        # Get all discussions
        result = service.get_discussions()

        # Verify result
        assert len(result) == 2
        assert any(d.id == discussion1.id for d in result)
        assert any(d.id == discussion2.id for d in result)

    @patch("boto3.client")
    def test_delete_discussion(self, mock_boto3_client):
        """Test deleting a discussion."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.delete_item.return_value = {}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Delete discussion
        result = service.delete_discussion("test-id")

        # Verify result
        assert result is True

        # Verify delete_item was called
        mock_client.delete_item.assert_called_once()

    @patch("boto3.client")
    def test_discussion_exists_true(self, mock_boto3_client):
        """Test checking if a discussion exists (exists)."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Mock get_item to return an item
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        mock_client.get_item.return_value = {
            "Item": {"id": serializer.serialize("test-id")}
        }
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Check if discussion exists
        result = service.discussion_exists("test-id")

        # Verify result
        assert result is True

    @patch("boto3.client")
    def test_discussion_exists_false(self, mock_boto3_client):
        """Test checking if a discussion exists (doesn't exist)."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.get_item.return_value = {}  # No Item in response
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Check if discussion exists
        result = service.discussion_exists("non-existent-id")

        # Verify result
        assert result is False

    @patch("boto3.client")
    def test_get_discussion_count(self, mock_boto3_client):
        """Test getting discussion count."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.scan.return_value = {"Count": 5}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Get discussion count
        result = service.get_discussion_count()

        # Verify result
        assert result == 5

    @patch("boto3.client")
    def test_update_discussion_insights(self, mock_boto3_client):
        """Test updating discussion insights."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_client.update_item.return_value = {}
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Create test insights
        insights = [
            Insight(
                category="Test Category",
                description="Test Description",
                supporting_messages=[0, 1],
                confidence_score=0.8,
            )
        ]

        # Update insights
        result = service.update_discussion_insights("test-id", insights)

        # Verify result
        assert result is True

        # Verify update_item was called
        mock_client.update_item.assert_called_once()

    @patch("boto3.client")
    def test_update_discussion_insights_not_found(self, mock_boto3_client):
        """Test updating insights for non-existent discussion."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Mock update_item to raise ConditionalCheckFailedException
        error_response = {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "The conditional request failed",
            }
        }
        mock_client.update_item.side_effect = ClientError(error_response, "UpdateItem")
        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Create test insights
        insights = [
            Insight(
                category="Test Category",
                description="Test Description",
                supporting_messages=[0, 1],
                confidence_score=0.8,
            )
        ]

        # Update insights for non-existent discussion
        result = service.update_discussion_insights("non-existent-id", insights)

        # Verify result is False
        assert result is False

    @patch("boto3.client")
    def test_get_discussions_by_topic(self, mock_boto3_client):
        """Test querying discussions by topic."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Create test discussion
        discussion = Discussion.create_new(
            topic="Marketing Strategy", participants=["persona1"]
        )

        # Mock scan to return serialized discussion
        service = DatabaseService(table_prefix="Test")
        serialized = service._serialize_discussion(discussion)
        mock_client.scan.return_value = {"Items": [serialized]}
        mock_boto3_client.return_value = mock_client

        # Re-initialize service with mocked client
        service = DatabaseService(table_prefix="Test")

        # Query by topic
        result = service.get_discussions_by_topic("Marketing")

        # Verify result
        assert len(result) == 1
        assert result[0].topic == "Marketing Strategy"

    @patch("boto3.client")
    def test_get_discussions_by_participant(self, mock_boto3_client):
        """Test querying discussions by participant."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Create test discussion
        discussion = Discussion.create_new(
            topic="Test Topic", participants=["persona1", "persona2"]
        )

        # Mock scan to return serialized discussion
        service = DatabaseService(table_prefix="Test")
        serialized = service._serialize_discussion(discussion)
        mock_client.scan.return_value = {"Items": [serialized]}
        mock_boto3_client.return_value = mock_client

        # Re-initialize service with mocked client
        service = DatabaseService(table_prefix="Test")

        # Query by participant
        result = service.get_discussions_by_participant("persona1")

        # Verify result
        assert len(result) == 1
        assert "persona1" in result[0].participants

    @patch("boto3.client")
    def test_get_discussions_by_date_range(self, mock_boto3_client):
        """Test querying discussions by date range."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Create test discussion
        discussion = Discussion.create_new(
            topic="Test Topic", participants=["persona1"]
        )

        # Mock scan to return serialized discussion
        service = DatabaseService(table_prefix="Test")
        serialized = service._serialize_discussion(discussion)
        mock_client.scan.return_value = {"Items": [serialized]}
        mock_boto3_client.return_value = mock_client

        # Re-initialize service with mocked client
        service = DatabaseService(table_prefix="Test")

        # Query by date range
        start_date = datetime.now() - timedelta(days=1)
        end_date = datetime.now() + timedelta(days=1)
        result = service.get_discussions_by_date_range(start_date, end_date)

        # Verify result
        assert len(result) == 1

    @patch("boto3.client")
    def test_get_discussions_by_mode(self, mock_boto3_client):
        """Test querying discussions by mode."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Create test discussion
        discussion = Discussion.create_new(
            topic="Test Topic", participants=["persona1"], mode="agent"
        )

        # Mock scan to return serialized discussion
        service = DatabaseService(table_prefix="Test")
        serialized = service._serialize_discussion(discussion)
        mock_client.scan.return_value = {"Items": [serialized]}
        mock_boto3_client.return_value = mock_client

        # Re-initialize service with mocked client
        service = DatabaseService(table_prefix="Test")

        # Query by mode
        result = service.get_discussions_by_mode("agent")

        # Verify result
        assert len(result) == 1
        assert result[0].mode == "agent"

    @patch("boto3.client")
    def test_get_insights_by_category(self, mock_boto3_client):
        """Test querying insights by category."""
        # Mock boto3 client
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}

        # Create test discussion with insights
        discussion = Discussion.create_new(
            topic="Test Topic", participants=["persona1"]
        )
        insight = Insight(
            category="User Needs",
            description="Test insight",
            supporting_messages=[0],
            confidence_score=0.9,
        )
        discussion = discussion.add_insight(insight)

        # Mock scan to return serialized discussion
        service = DatabaseService(table_prefix="Test")
        serialized = service._serialize_discussion(discussion)
        mock_client.scan.return_value = {"Items": [serialized]}
        mock_boto3_client.return_value = mock_client

        # Re-initialize service with mocked client
        service = DatabaseService(table_prefix="Test")

        # Query insights by category
        result = service.get_insights_by_category("User Needs")

        # Verify result
        assert len(result) == 1
        assert result[0]["category"] == "User Needs"
        assert result[0]["description"] == "Test insight"
        assert result[0]["discussion_id"] == discussion.id


class TestDiscussionCRUDOperationsProperties:
    """
    Property-based tests for Discussion CRUD operations.
    **Feature: dynamodb-migration, Property 3: CRUD operation equivalence**
    **Validates: Requirements 2.2, 2.4**
    """

    @patch("boto3.client")
    @given(discussion=_discussion_strategy())
    @settings(max_examples=100, deadline=None)
    def test_discussion_crud_cycle(self, mock_boto3_client, discussion):
        """
        Property test: For any valid Discussion, performing a create-read-update-delete cycle
        should work correctly - saved data can be retrieved, updated, and deleted.

        This tests that:
        1. Saving a discussion returns the discussion ID
        2. Reading the saved discussion returns equivalent data
        3. Updating the discussion succeeds
        4. Reading the updated discussion returns the new data
        5. Deleting the discussion succeeds
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
            if item_id in saved_items:
                del saved_items[item_id]
            return {}

        def mock_update_item(**kwargs):
            """Mock update_item to update insights in memory."""
            key = kwargs["Key"]
            item_id = mock_client._deserializer.deserialize(key["id"])
            if item_id not in saved_items:
                # Raise ConditionalCheckFailedException if item doesn't exist
                error_response = {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "The conditional request failed",
                    }
                }
                raise ClientError(error_response, "UpdateItem")

            # Update the insights in the saved item
            update_expr = kwargs.get("UpdateExpression", "")
            if "insights" in update_expr:
                expr_attr_values = kwargs.get("ExpressionAttributeValues", {})
                if ":insights" in expr_attr_values:
                    saved_items[item_id]["insights"] = expr_attr_values[":insights"]

            return {}

        # Set up deserializer for mock client
        from boto3.dynamodb.types import TypeDeserializer

        mock_client._deserializer = TypeDeserializer()

        # Configure mock methods
        mock_client.put_item = Mock(side_effect=mock_put_item)
        mock_client.get_item = Mock(side_effect=mock_get_item)
        mock_client.delete_item = Mock(side_effect=mock_delete_item)
        mock_client.update_item = Mock(side_effect=mock_update_item)

        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Step 1: CREATE - Save the discussion
        saved_id = service.save_discussion(discussion)
        assert saved_id == discussion.id, "Save should return the discussion ID"

        # Step 2: READ - Retrieve the saved discussion
        retrieved = service.get_discussion(discussion.id)
        assert retrieved is not None, "Saved discussion should be retrievable"
        assert retrieved.id == discussion.id
        assert retrieved.topic == discussion.topic
        assert retrieved.participants == discussion.participants
        assert retrieved.mode == discussion.mode
        assert retrieved.agent_config == discussion.agent_config
        assert retrieved.created_at == discussion.created_at

        # Verify messages are preserved
        assert len(retrieved.messages) == len(discussion.messages)
        for orig_msg, retr_msg in zip(discussion.messages, retrieved.messages):
            assert retr_msg.persona_id == orig_msg.persona_id
            assert retr_msg.persona_name == orig_msg.persona_name
            assert retr_msg.content == orig_msg.content
            assert retr_msg.timestamp == orig_msg.timestamp
            assert retr_msg.message_type == orig_msg.message_type
            assert retr_msg.round_number == orig_msg.round_number

        # Verify insights are preserved
        assert len(retrieved.insights) == len(discussion.insights)
        for orig_insight, retr_insight in zip(discussion.insights, retrieved.insights):
            assert retr_insight.category == orig_insight.category
            assert retr_insight.description == orig_insight.description
            assert retr_insight.supporting_messages == orig_insight.supporting_messages
            # Use approximate comparison for floats
            assert (
                abs(retr_insight.confidence_score - orig_insight.confidence_score)
                < 0.0001
            )

        # Step 3: UPDATE - Add new insights to the discussion
        new_insight = Insight(
            category="Updated Category",
            description="Updated insight",
            supporting_messages=["msg-1"],
            confidence_score=0.95,
        )
        updated_insights = list(discussion.insights) + [new_insight]
        update_result = service.update_discussion_insights(
            discussion.id, updated_insights
        )
        assert update_result is True, "Update should succeed"

        # Step 4: READ - Retrieve the updated discussion
        retrieved_updated = service.get_discussion(discussion.id)
        assert retrieved_updated is not None, "Updated discussion should be retrievable"
        assert len(retrieved_updated.insights) == len(updated_insights)

        # Verify the new insight is present
        updated_categories = [
            insight.category for insight in retrieved_updated.insights
        ]
        assert "Updated Category" in updated_categories

        # Step 5: DELETE - Remove the discussion
        delete_result = service.delete_discussion(discussion.id)
        assert delete_result is True, "Delete should succeed"

        # Step 6: READ - Verify discussion is deleted
        retrieved_after_delete = service.get_discussion(discussion.id)
        assert retrieved_after_delete is None, (
            "Deleted discussion should not be retrievable"
        )

    @patch("boto3.client")
    @given(discussion1=_discussion_strategy(), discussion2=_discussion_strategy())
    @settings(max_examples=50, deadline=None)
    def test_multiple_discussions_crud(
        self, mock_boto3_client, discussion1, discussion2
    ):
        """
        Property test: For any two valid Discussions, CRUD operations should work
        independently without interference.

        This tests that:
        1. Multiple discussions can be saved
        2. Each discussion can be retrieved independently
        3. Updating one discussion doesn't affect the other
        4. Deleting one discussion doesn't affect the other

        **Feature: dynamodb-migration, Property 3: CRUD operation equivalence**
        **Validates: Requirements 2.2, 2.4**
        """
        # Ensure discussions have different IDs
        if discussion1.id == discussion2.id:
            discussion2 = Discussion(
                id=discussion1.id + "_different",
                topic=discussion2.topic,
                participants=discussion2.participants,
                messages=discussion2.messages,
                insights=discussion2.insights,
                created_at=discussion2.created_at,
                mode=discussion2.mode,
                agent_config=discussion2.agent_config,
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
            if item_id in saved_items:
                del saved_items[item_id]
            return {}

        def mock_update_item(**kwargs):
            key = kwargs["Key"]
            from boto3.dynamodb.types import TypeDeserializer

            deserializer = TypeDeserializer()
            item_id = deserializer.deserialize(key["id"])
            if item_id not in saved_items:
                error_response = {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "The conditional request failed",
                    }
                }
                raise ClientError(error_response, "UpdateItem")

            # Update the insights in the saved item
            update_expr = kwargs.get("UpdateExpression", "")
            if "insights" in update_expr:
                expr_attr_values = kwargs.get("ExpressionAttributeValues", {})
                if ":insights" in expr_attr_values:
                    saved_items[item_id]["insights"] = expr_attr_values[":insights"]

            return {}

        mock_client.put_item = Mock(side_effect=mock_put_item)
        mock_client.get_item = Mock(side_effect=mock_get_item)
        mock_client.delete_item = Mock(side_effect=mock_delete_item)
        mock_client.update_item = Mock(side_effect=mock_update_item)

        mock_boto3_client.return_value = mock_client

        service = DatabaseService(table_prefix="Test")

        # Save both discussions
        service.save_discussion(discussion1)
        service.save_discussion(discussion2)

        # Retrieve both discussions independently
        retrieved1 = service.get_discussion(discussion1.id)
        retrieved2 = service.get_discussion(discussion2.id)

        assert retrieved1 is not None
        assert retrieved2 is not None
        assert retrieved1.id == discussion1.id
        assert retrieved2.id == discussion2.id
        assert retrieved1.topic == discussion1.topic
        assert retrieved2.topic == discussion2.topic

        # Update discussion1 with new insights
        new_insight = Insight(
            category="Modified Category",
            description="Modified insight",
            supporting_messages=["msg-1"],
            confidence_score=0.88,
        )
        updated_insights = list(discussion1.insights) + [new_insight]
        service.update_discussion_insights(discussion1.id, updated_insights)

        # Verify discussion1 is updated but discussion2 is unchanged
        retrieved1_after = service.get_discussion(discussion1.id)
        retrieved2_after = service.get_discussion(discussion2.id)

        # Check that discussion1 has the new insight
        categories1 = [insight.category for insight in retrieved1_after.insights]
        assert "Modified Category" in categories1

        # Check that discussion2 insights are unchanged
        assert len(retrieved2_after.insights) == len(discussion2.insights)

        # Delete discussion1
        service.delete_discussion(discussion1.id)

        # Verify discussion1 is deleted but discussion2 still exists
        retrieved1_deleted = service.get_discussion(discussion1.id)
        retrieved2_still_exists = service.get_discussion(discussion2.id)

        assert retrieved1_deleted is None
        assert retrieved2_still_exists is not None
        assert retrieved2_still_exists.id == discussion2.id
