"""
Database service for AI Persona System.
Handles DynamoDB database operations with retry logic and error handling.
"""

import logging
import time
from typing import List, Optional, Dict, Any, Callable, Tuple, TypeVar, TYPE_CHECKING
from datetime import datetime
from decimal import Decimal
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

# Import models
from ..models.persona import Persona
from ..models.discussion import Discussion
from ..models.message import Message
from ..models.insight import Insight

if TYPE_CHECKING:
    from ..models.dataset import Dataset, PersonaDatasetBinding
    from ..models.survey_template import SurveyTemplate
    from ..models.survey import Survey
    from ..models.knowledge_base import KnowledgeBase, PersonaKBBinding


class DatabaseError(Exception):
    """Custom exception for database-related errors."""

    pass


_T = TypeVar("_T")


class DatabaseService:
    """Service for managing DynamoDB database operations."""

    def __init__(
        self,
        table_prefix: str = "AIPersona",
        region: str = "us-east-1",
    ):
        """
        Initialize DynamoDB service with AWS configuration.

        Args:
            table_prefix: Prefix for DynamoDB table names
            region: AWS region for DynamoDB

        Raises:
            DatabaseError: If AWS credentials are invalid or missing
        """
        self.table_prefix = table_prefix
        self.region = region
        self.logger = logging.getLogger(__name__)

        # Initialize AWS client
        try:
            import boto3

            # Create DynamoDB client with configuration
            client_config = {"region_name": region}

            self.dynamodb_client = boto3.client("dynamodb", **client_config)

            # Test connection by listing tables
            self.dynamodb_client.list_tables(Limit=1)

            self.logger.info(
                f"DynamoDB service initialized successfully "
                f"(region: {region}, prefix: {table_prefix})"
            )

        except (NoCredentialsError, PartialCredentialsError) as e:
            error_msg = (
                "AWS credentials are invalid or missing. "
                "Please configure AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                "environment variables or AWS credentials file."
            )
            self.logger.error(f"Credential error: {e}")
            raise DatabaseError(error_msg)

        except Exception as e:
            error_msg = f"Failed to initialize DynamoDB client: {e}"
            self.logger.error(error_msg)
            raise DatabaseError(error_msg)

        # Initialize type serializer/deserializer for DynamoDB
        self.serializer = TypeSerializer()
        self.deserializer = TypeDeserializer()

        # Table names
        self.personas_table = f"{table_prefix}_Personas"
        self.discussions_table = f"{table_prefix}_Discussions"
        self.files_table = f"{table_prefix}_UploadedFiles"
        self.datasets_table = f"{table_prefix}_Datasets"
        self.bindings_table = f"{table_prefix}_PersonaDatasetBindings"
        self.survey_templates_table = f"{table_prefix}_SurveyTemplates"
        self.surveys_table = f"{table_prefix}_Surveys"
        self.kb_table = f"{table_prefix}_KnowledgeBases"
        self.persona_kb_bindings_table = f"{table_prefix}_PersonaKBBindings"

    def _get_table_name(self, entity_type: str) -> str:
        """
        Get full table name for entity type.

        Args:
            entity_type: Type of entity ('personas', 'discussions', 'files', 'datasets', 'bindings')

        Returns:
            Full table name with prefix
        """
        table_map = {
            "personas": self.personas_table,
            "discussions": self.discussions_table,
            "files": self.files_table,
            "datasets": self.datasets_table,
            "bindings": self.bindings_table,
        }
        return table_map.get(entity_type, f"{self.table_prefix}_{entity_type}")

    def _execute_with_retry(
        self,
        operation: Callable[[], _T],
        max_retries: int = 5,
        operation_name: str = "DynamoDB operation",
    ) -> _T:
        """
        Execute DynamoDB operation with retry logic for transient errors.

        Implements exponential backoff for throttling errors and fixed retry
        for network errors.

        Args:
            operation: Callable that performs the DynamoDB operation
            max_retries: Maximum number of retry attempts
            operation_name: Name of operation for logging

        Returns:
            Result of the operation

        Raises:
            DatabaseError: If operation fails after all retries
        """
        last_exception = None

        for attempt in range(max_retries):
            try:
                # Execute the operation
                result = operation()

                # Log successful retry if this wasn't the first attempt
                if attempt > 0:
                    self.logger.info(
                        f"{operation_name} succeeded on attempt {attempt + 1}"
                    )

                return result

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "Unknown")
                last_exception = e

                # Handle throttling errors with exponential backoff
                if error_code == "ProvisionedThroughputExceededException":
                    if attempt < max_retries - 1:
                        delay = 2**attempt  # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                        self.logger.warning(
                            f"{operation_name} throttled (attempt {attempt + 1}/{max_retries}), "
                            f"retrying in {delay}s"
                        )
                        time.sleep(delay)
                        continue
                    else:
                        error_msg = (
                            f"{operation_name} failed after {max_retries} attempts "
                            f"due to throttling: {e}"
                        )
                        self.logger.error(error_msg)
                        raise DatabaseError(error_msg)

                # Handle transient service errors with fixed retry
                elif error_code in [
                    "RequestTimeout",
                    "ServiceUnavailable",
                    "InternalServerError",
                ]:
                    if attempt < max_retries - 1:
                        delay = 1  # Intentional: retry backoff for transient DynamoDB errors
                        self.logger.warning(
                            f"{operation_name} encountered {error_code} "
                            f"(attempt {attempt + 1}/{max_retries}), retrying in {delay}s"
                        )
                        time.sleep(delay)  # noqa: S311
                        continue
                    else:
                        error_msg = (
                            f"{operation_name} failed after {max_retries} attempts "
                            f"due to {error_code}: {e}"
                        )
                        self.logger.error(error_msg)
                        raise DatabaseError(error_msg)

                # Handle conditional check failures (not retryable)
                elif error_code == "ConditionalCheckFailedException":
                    self.logger.info(f"{operation_name} conditional check failed: {e}")
                    raise DatabaseError(f"Conditional check failed: {e}")

                # Handle resource not found (not retryable)
                elif error_code == "ResourceNotFoundException":
                    error_msg = (
                        f"{operation_name} failed: Table not found. "
                        "Please ensure DynamoDB tables are created. "
                        f"Details: {e}"
                    )
                    self.logger.error(error_msg)
                    raise DatabaseError(error_msg)

                # Handle validation errors (not retryable)
                elif error_code == "ValidationException":
                    error_msg = f"{operation_name} validation error: {e}"
                    self.logger.error(error_msg)
                    raise DatabaseError(error_msg)

                # Handle other client errors (not retryable)
                else:
                    error_msg = f"{operation_name} failed with {error_code}: {e}"
                    self.logger.error(error_msg)
                    raise DatabaseError(error_msg)

            except (ConnectionError, TimeoutError) as e:
                last_exception = e

                # Handle network errors with fixed retry
                if attempt < max_retries - 1:
                    delay = 1  # Intentional: retry backoff for transient network errors
                    self.logger.warning(
                        f"{operation_name} network error "
                        f"(attempt {attempt + 1}/{max_retries}), retrying in {delay}s: {e}"
                    )
                    time.sleep(delay)  # noqa: S311
                    continue
                else:
                    error_msg = (
                        f"{operation_name} failed after {max_retries} attempts "
                        f"due to network error: {e}"
                    )
                    self.logger.error(error_msg)
                    raise DatabaseError(error_msg)

            except Exception as e:
                # Unexpected errors are not retried
                error_msg = f"{operation_name} failed with unexpected error: {e}"
                self.logger.error(error_msg)
                raise DatabaseError(error_msg)

        # Should not reach here, but handle it just in case
        error_msg = (
            f"{operation_name} failed after {max_retries} attempts. "
            f"Last error: {last_exception}"
        )
        self.logger.error(error_msg)
        raise DatabaseError(error_msg)

    def check_database_health(self) -> bool:
        """
        Check if DynamoDB is accessible and required tables exist.

        Returns:
            True if database is healthy, False otherwise
        """
        try:
            # Check if all required tables exist
            required_tables = {
                self.personas_table,
                self.discussions_table,
                self.files_table,
            }

            # List all tables and check if required ones exist
            response = self.dynamodb_client.list_tables()
            existing_tables = set(response.get("TableNames", []))

            # Check if all required tables are present
            tables_exist = required_tables.issubset(existing_tables)

            if not tables_exist:
                missing = required_tables - existing_tables
                self.logger.warning(
                    f"Database health check failed: Missing tables {missing}"
                )
                return False

            # Try to describe one table to verify access
            self.dynamodb_client.describe_table(TableName=self.personas_table)

            self.logger.info("Database health check passed")
            return True

        except Exception as e:
            self.logger.error(f"Database health check failed: {e}")
            return False

    def initialize_database(self) -> None:
        """
        Initialize database by verifying DynamoDB tables exist.

        For DynamoDB, tables should be created via CDK or create_dynamodb_tables.py.
        This method only verifies that required tables exist.

        Raises:
            DatabaseError: If required tables don't exist
        """
        try:
            if not self.check_database_health():
                raise DatabaseError(
                    f"Required DynamoDB tables not found. "
                    f"Please create tables: {self.personas_table}, "
                    f"{self.discussions_table}, {self.files_table}"
                )
            self.logger.info("Database initialized successfully")
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to initialize database: {e}")

    def get_database_info(self) -> Dict[str, Any]:
        """
        Get database information for debugging.

        Returns:
            Dictionary with database information
        """
        try:
            info: Dict[str, Any] = {
                "backend": "dynamodb",
                "region": self.region,
                "table_prefix": self.table_prefix,
                "tables": {},
            }

            # Get information about each table
            for table_name in [
                self.personas_table,
                self.discussions_table,
                self.files_table,
            ]:
                try:
                    response = self.dynamodb_client.describe_table(TableName=table_name)
                    table_info = response.get("Table", {})

                    info["tables"][table_name] = {
                        "status": table_info.get("TableStatus"),
                        "item_count": table_info.get("ItemCount", 0),
                        "size_bytes": table_info.get("TableSizeBytes", 0),
                        "creation_date": str(table_info.get("CreationDateTime", "")),
                    }

                except ClientError as e:
                    if (
                        e.response.get("Error", {}).get("Code")
                        == "ResourceNotFoundException"
                    ):
                        info["tables"][table_name] = {"status": "NOT_FOUND"}
                    else:
                        info["tables"][table_name] = {
                            "status": "ERROR",
                            "error": str(e),
                        }

            return info

        except Exception as e:
            self.logger.error(f"Failed to get database info: {e}")
            return {"backend": "dynamodb", "error": str(e)}

    # Serialization methods for Python to DynamoDB type conversion

    def _serialize_persona(self, persona: Persona) -> Dict[str, Any]:
        """
        Serialize Persona object to DynamoDB format.

        Args:
            persona: Persona object to serialize

        Returns:
            Dictionary in DynamoDB format with type descriptors
        """
        # Convert persona to dictionary with ISO datetime strings
        persona_dict = {
            "id": persona.id,
            "name": persona.name,
            "age": persona.age,
            "occupation": persona.occupation,
            "background": persona.background,
            "values": persona.values,
            "pain_points": persona.pain_points,
            "goals": persona.goals,
            "created_at": persona.created_at.isoformat(),
            "updated_at": persona.updated_at.isoformat(),
            "type": "persona",  # For GSI queries
        }

        # Optional fields
        if persona.generation_log is not None:
            persona_dict["generation_log"] = persona.generation_log
        if persona.generation_context is not None:
            persona_dict["generation_context"] = persona.generation_context

        # Use boto3 TypeSerializer to convert to DynamoDB format
        serialized = {}
        for key, value in persona_dict.items():
            serialized[key] = self.serializer.serialize(value)

        return serialized

    def _serialize_discussion(self, discussion: Discussion) -> Dict[str, Any]:
        """
        Serialize Discussion object to DynamoDB format.

        Args:
            discussion: Discussion object to serialize

        Returns:
            Dictionary in DynamoDB format with type descriptors
        """
        # Convert messages to dictionaries
        messages_list = []
        for msg in discussion.messages:
            msg_dict: Dict[str, Any] = {
                "persona_id": msg.persona_id,
                "persona_name": msg.persona_name,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat(),
                "message_type": msg.message_type,
            }
            # Add round_number if present
            if msg.round_number is not None:
                msg_dict["round_number"] = msg.round_number
            messages_list.append(msg_dict)

        # Convert insights to dictionaries
        insights_list = []
        for insight in discussion.insights:
            insight_dict = {
                "category": insight.category,
                "description": insight.description,
                "supporting_messages": insight.supporting_messages,
                "confidence_score": Decimal(
                    str(insight.confidence_score)
                ),  # Convert float to Decimal
            }
            insights_list.append(insight_dict)

        # Build discussion dictionary
        discussion_dict: Dict[str, Any] = {
            "id": discussion.id,
            "topic": discussion.topic,
            "participants": discussion.participants,
            "messages": messages_list,
            "insights": insights_list,
            "created_at": discussion.created_at.isoformat(),
            "mode": discussion.mode,
            "type": "discussion",  # For GSI queries
        }

        # Add agent_config if present
        if discussion.agent_config is not None:
            discussion_dict["agent_config"] = discussion.agent_config

        # Add documents if present
        if discussion.documents is not None:
            discussion_dict["documents"] = discussion.documents

        # Add reports if present
        if discussion.reports:
            discussion_dict["reports"] = [r.to_dict() for r in discussion.reports]

        # Use boto3 TypeSerializer to convert to DynamoDB format
        serialized = {}
        for key, value in discussion_dict.items():
            serialized[key] = self.serializer.serialize(value)

        return serialized

    def _serialize_file_info(
        self,
        file_id: str,
        filename: str,
        file_path: str,
        file_size: Optional[int] = None,
        file_hash: Optional[str] = None,
        mime_type: Optional[str] = None,
        uploaded_at: Optional[datetime] = None,
        original_filename: Optional[str] = None,
        file_type: str = "persona_interview",
    ) -> Dict[str, Any]:
        """
        Serialize file metadata to DynamoDB format.

        Args:
            file_id: Unique file identifier
            filename: Saved filename (with UUID prefix)
            file_path: Path to stored file
            file_size: File size in bytes (optional)
            file_hash: File hash for integrity checking (optional)
            mime_type: MIME type of file (optional)
            uploaded_at: Upload timestamp (optional, defaults to now)
            original_filename: Original filename before UUID prefix (optional)
            file_type: File type ('persona_interview' or 'discussion_document')

        Returns:
            Dictionary in DynamoDB format with type descriptors
        """
        # Build file info dictionary
        file_dict: Dict[str, Any] = {
            "id": file_id,
            "filename": filename,
            "original_filename": original_filename if original_filename else filename,
            "file_path": file_path,
            "uploaded_at": (uploaded_at or datetime.now()).isoformat(),
            "file_type": file_type,
            "type": "file",  # For GSI queries
        }

        # Add optional fields if provided
        if file_size is not None:
            file_dict["file_size"] = file_size
        if file_hash is not None:
            file_dict["file_hash"] = file_hash
        if mime_type is not None:
            file_dict["mime_type"] = mime_type

        # Use boto3 TypeSerializer to convert to DynamoDB format
        serialized = {}
        for key, value in file_dict.items():
            serialized[key] = self.serializer.serialize(value)

        return serialized

    # Deserialization methods for DynamoDB to Python type conversion

    def _deserialize_persona(self, item: Dict[str, Any]) -> Persona:
        """
        Deserialize DynamoDB item to Persona object.

        Args:
            item: DynamoDB item with type descriptors

        Returns:
            Persona object
        """
        # Use boto3 TypeDeserializer to convert from DynamoDB format
        deserialized = {}
        for key, value in item.items():
            deserialized[key] = self.deserializer.deserialize(value)

        # Convert ISO datetime strings back to datetime objects
        created_at = datetime.fromisoformat(deserialized["created_at"])
        updated_at = datetime.fromisoformat(deserialized["updated_at"])

        # Create Persona object
        # Convert Decimal to int for age (DynamoDB returns numbers as Decimal)
        return Persona(
            id=deserialized["id"],
            name=deserialized["name"],
            age=int(deserialized["age"]),
            occupation=deserialized["occupation"],
            background=deserialized["background"],
            values=deserialized["values"],
            pain_points=deserialized["pain_points"],
            goals=deserialized["goals"],
            created_at=created_at,
            updated_at=updated_at,
            generation_log=deserialized.get("generation_log"),
            generation_context=deserialized.get("generation_context"),
        )

    def _deserialize_discussion(self, item: Dict[str, Any]) -> Discussion:
        """
        Deserialize DynamoDB item to Discussion object.

        Args:
            item: DynamoDB item with type descriptors

        Returns:
            Discussion object
        """
        # Use boto3 TypeDeserializer to convert from DynamoDB format
        deserialized = {}
        for key, value in item.items():
            deserialized[key] = self.deserializer.deserialize(value)

        # Import Message and Insight here to avoid circular imports
        from ..models.insight import Insight

        # Convert messages from dictionaries to Message objects
        messages = []
        for msg_dict in deserialized.get("messages", []):
            msg = Message(
                persona_id=msg_dict["persona_id"],
                persona_name=msg_dict["persona_name"],
                content=msg_dict["content"],
                timestamp=datetime.fromisoformat(msg_dict["timestamp"]),
                message_type=msg_dict.get("message_type", "statement"),
                round_number=msg_dict.get("round_number"),
            )
            messages.append(msg)

        # Convert insights from dictionaries to Insight objects
        insights = []
        for insight_dict in deserialized.get("insights", []):
            insight = Insight(
                category=insight_dict["category"],
                description=insight_dict["description"],
                supporting_messages=insight_dict["supporting_messages"],
                confidence_score=float(
                    insight_dict["confidence_score"]
                ),  # Convert Decimal to float
            )
            insights.append(insight)

        # Convert ISO datetime string back to datetime object
        created_at = datetime.fromisoformat(deserialized["created_at"])

        from ..models.discussion_report import DiscussionReport

        # Convert reports from dictionaries to DiscussionReport objects
        reports = [
            DiscussionReport.from_dict(r)
            for r in deserialized.get("reports", [])
        ]

        # Create Discussion object
        return Discussion(
            id=deserialized["id"],
            topic=deserialized["topic"],
            participants=deserialized["participants"],
            messages=messages,
            insights=insights,
            created_at=created_at,
            mode=deserialized.get("mode", "classic"),
            agent_config=deserialized.get("agent_config"),
            documents=deserialized.get("documents"),
            reports=reports,
        )

    def _deserialize_file_info(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deserialize DynamoDB item to file metadata dictionary.

        Args:
            item: DynamoDB item with type descriptors

        Returns:
            Dictionary with file metadata
        """
        # Use boto3 TypeDeserializer to convert from DynamoDB format
        deserialized = {}
        for key, value in item.items():
            deserialized[key] = self.deserializer.deserialize(value)

        # Convert ISO datetime string back to datetime object
        uploaded_at = datetime.fromisoformat(deserialized["uploaded_at"])

        # Build file info dictionary
        file_info = {
            "id": deserialized["id"],
            "filename": deserialized.get(
                "filename", deserialized.get("original_filename")
            ),
            "original_filename": deserialized.get(
                "original_filename", deserialized.get("filename")
            ),
            "file_path": deserialized["file_path"],
            "uploaded_at": uploaded_at,
        }

        # Add optional fields if present
        if "file_size" in deserialized:
            file_info["file_size"] = deserialized["file_size"]
        if "file_hash" in deserialized:
            file_info["file_hash"] = deserialized["file_hash"]
        if "mime_type" in deserialized:
            file_info["mime_type"] = deserialized["mime_type"]

        return file_info

    # Persona CRUD operations

    def save_persona(self, persona: Persona) -> str:
        """
        Save a persona to DynamoDB.

        Args:
            persona: Persona object to save

        Returns:
            Persona ID

        Raises:
            DatabaseError: If save operation fails
        """

        def _save() -> str:
            serialized_persona = self._serialize_persona(persona)

            self.dynamodb_client.put_item(
                TableName=self.personas_table, Item=serialized_persona
            )

            return persona.id

        return self._execute_with_retry(
            _save, operation_name=f"save_persona({persona.id})"
        )

    def update_persona(self, persona: Persona) -> bool:
        """
        Update an existing persona in DynamoDB.

        Args:
            persona: Persona object with updated data

        Returns:
            True if update was successful, False otherwise

        Raises:
            DatabaseError: If update operation fails
        """

        def _update() -> bool:
            serialized_persona = self._serialize_persona(persona)

            # Use put_item to replace the entire item (replaces all fields)
            self.dynamodb_client.put_item(
                TableName=self.personas_table, Item=serialized_persona
            )

            return True

        return self._execute_with_retry(
            _update, operation_name=f"update_persona({persona.id})"
        )

    def delete_persona(self, persona_id: str) -> bool:
        """
        Delete a persona from DynamoDB.

        Args:
            persona_id: ID of persona to delete

        Returns:
            True if deletion was successful, False if persona didn't exist

        Raises:
            DatabaseError: If delete operation fails
        """

        def _delete() -> bool:
            try:
                self.dynamodb_client.delete_item(
                    TableName=self.personas_table,
                    Key={"id": self.serializer.serialize(persona_id)},
                )
                # DynamoDB delete_item succeeds even if item doesn't exist
                return True

            except ClientError:
                # Re-raise to be handled by retry logic
                raise

        return self._execute_with_retry(
            _delete, operation_name=f"delete_persona({persona_id})"
        )

    def get_persona(self, persona_id: str) -> Optional[Persona]:
        """
        Retrieve a persona by ID from DynamoDB.

        Args:
            persona_id: ID of persona to retrieve

        Returns:
            Persona object if found, None otherwise

        Raises:
            DatabaseError: If retrieval operation fails
        """

        def _get() -> Optional[Persona]:
            response = self.dynamodb_client.get_item(
                TableName=self.personas_table,
                Key={"id": self.serializer.serialize(persona_id)},
            )

            # Check if item was found
            if "Item" not in response:
                return None

            # Deserialize and return persona
            return self._deserialize_persona(response["Item"])

        return self._execute_with_retry(
            _get, operation_name=f"get_persona({persona_id})"
        )

    def get_all_personas(
        self,
        limit: int = 20,
        cursor: Optional[Dict[str, Any]] = None,
        search_all: bool = False,
    ) -> Tuple[List[Persona], Optional[Dict[str, Any]]]:
        """
        Retrieve personas with cursor-based pagination via GSI 'CreatedAtIndex'.

        Args:
            limit: Page size (default 20).
            cursor: LastEvaluatedKey from a previous call for the next page.
            search_all: If True, fall back to scan and return all items
                (used when full-text search needs to span all records).

        Returns:
            Tuple of (personas, next_cursor). next_cursor is None if no more pages.
        """

        def _query() -> Tuple[List[Persona], Optional[Dict[str, Any]]]:
            if search_all:
                # Full-table scan fallback for search that GSI cannot support.
                personas: List[Persona] = []
                scan_params: Dict[str, Any] = {"TableName": self.personas_table}
                response = self.dynamodb_client.scan(**scan_params)
                for item in response.get("Items", []):
                    personas.append(self._deserialize_persona(item))
                while "LastEvaluatedKey" in response:
                    scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                    response = self.dynamodb_client.scan(**scan_params)
                    for item in response.get("Items", []):
                        personas.append(self._deserialize_persona(item))
                return personas, None

            params: Dict[str, Any] = {
                "TableName": self.personas_table,
                "IndexName": "CreatedAtIndex",
                "KeyConditionExpression": "#t = :t",
                "ExpressionAttributeNames": {"#t": "type"},
                "ExpressionAttributeValues": {":t": {"S": "persona"}},
                "ScanIndexForward": False,
                "Limit": limit,
            }
            if cursor:
                params["ExclusiveStartKey"] = cursor
            response = self.dynamodb_client.query(**params)
            personas = [
                self._deserialize_persona(item) for item in response.get("Items", [])
            ]
            return personas, response.get("LastEvaluatedKey")

        return self._execute_with_retry(_query, operation_name="get_all_personas")

    def persona_exists(self, persona_id: str) -> bool:
        """
        Check if a persona exists in DynamoDB.

        Args:
            persona_id: ID of persona to check

        Returns:
            True if persona exists, False otherwise

        Raises:
            DatabaseError: If check operation fails
        """

        def _exists() -> bool:
            response = self.dynamodb_client.get_item(
                TableName=self.personas_table,
                Key={"id": self.serializer.serialize(persona_id)},
                # Only retrieve the key to minimize data transfer
                ProjectionExpression="id",
            )

            return "Item" in response

        return self._execute_with_retry(
            _exists, operation_name=f"persona_exists({persona_id})"
        )

    def get_persona_count(self) -> int:
        """
        Get the total count of personas in DynamoDB.

        Returns:
            Number of personas

        Raises:
            DatabaseError: If count operation fails
        """

        def _count() -> int:
            # Use GSI Query with Select='COUNT' (more efficient than scan)
            params: Dict[str, Any] = {
                "TableName": self.personas_table,
                "IndexName": "CreatedAtIndex",
                "KeyConditionExpression": "#t = :t",
                "ExpressionAttributeNames": {"#t": "type"},
                "ExpressionAttributeValues": {":t": {"S": "persona"}},
                "Select": "COUNT",
            }
            response = self.dynamodb_client.query(**params)
            count: int = response.get("Count", 0)

            while "LastEvaluatedKey" in response:
                params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.dynamodb_client.query(**params)
                count += int(response.get("Count", 0))

            return count

        return self._execute_with_retry(_count, operation_name="get_persona_count")

    def get_personas_by_name(
        self, name_pattern: str, limit: Optional[int] = None
    ) -> List[Persona]:
        """
        Query personas by name pattern using Global Secondary Index.

        Note: This implementation uses scan with filter since GSI requires exact matches.
        For production, consider using begins_with or contains operators with GSI.

        Args:
            name_pattern: Name pattern to search for (case-insensitive substring match)
            limit: Maximum number of personas to return (optional)

        Returns:
            List of Persona objects matching the name pattern

        Raises:
            DatabaseError: If query operation fails
        """

        def _query_by_name() -> list[Persona]:
            personas = []

            # Build scan parameters with filter expression
            scan_params: Dict[str, Any] = {
                "TableName": self.personas_table,
                "FilterExpression": "contains(#name, :name_val)",
                "ExpressionAttributeNames": {"#name": "name"},
                "ExpressionAttributeValues": {
                    ":name_val": self.serializer.serialize(name_pattern)
                },
            }

            if limit:
                scan_params["Limit"] = limit

            # Scan with filter
            response = self.dynamodb_client.scan(**scan_params)

            # Deserialize all items
            for item in response.get("Items", []):
                persona = self._deserialize_persona(item)
                personas.append(persona)

            # Handle pagination if no limit was specified
            while "LastEvaluatedKey" in response and not limit:
                scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.dynamodb_client.scan(**scan_params)

                for item in response.get("Items", []):
                    persona = self._deserialize_persona(item)
                    personas.append(persona)

            return personas

        return self._execute_with_retry(
            _query_by_name, operation_name=f"get_personas_by_name({name_pattern})"
        )

    def get_personas_by_occupation(
        self, occupation_pattern: str, limit: Optional[int] = None
    ) -> List[Persona]:
        """
        Query personas by occupation pattern using Global Secondary Index.

        Note: This implementation uses scan with filter since GSI requires exact matches.
        For production, consider using begins_with or contains operators with GSI.

        Args:
            occupation_pattern: Occupation pattern to search for (case-insensitive substring match)
            limit: Maximum number of personas to return (optional)

        Returns:
            List of Persona objects matching the occupation pattern

        Raises:
            DatabaseError: If query operation fails
        """

        def _query_by_occupation() -> list[Persona]:
            personas = []

            # Build scan parameters with filter expression
            scan_params: Dict[str, Any] = {
                "TableName": self.personas_table,
                "FilterExpression": "contains(#occupation, :occupation_val)",
                "ExpressionAttributeNames": {"#occupation": "occupation"},
                "ExpressionAttributeValues": {
                    ":occupation_val": self.serializer.serialize(occupation_pattern)
                },
            }

            if limit:
                scan_params["Limit"] = limit

            # Scan with filter
            response = self.dynamodb_client.scan(**scan_params)

            # Deserialize all items
            for item in response.get("Items", []):
                persona = self._deserialize_persona(item)
                personas.append(persona)

            # Handle pagination if no limit was specified
            while "LastEvaluatedKey" in response and not limit:
                scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.dynamodb_client.scan(**scan_params)

                for item in response.get("Items", []):
                    persona = self._deserialize_persona(item)
                    personas.append(persona)

            return personas

        return self._execute_with_retry(
            _query_by_occupation,
            operation_name=f"get_personas_by_occupation({occupation_pattern})",
        )

    def save_discussion(self, discussion: Discussion) -> str:
        """
        Save a discussion to DynamoDB.

        Args:
            discussion: Discussion object to save

        Returns:
            Discussion ID

        Raises:
            DatabaseError: If save operation fails
        """

        def _save() -> str:
            serialized_discussion = self._serialize_discussion(discussion)

            self.dynamodb_client.put_item(
                TableName=self.discussions_table, Item=serialized_discussion
            )

            return discussion.id

        return self._execute_with_retry(
            _save, operation_name=f"save_discussion({discussion.id})"
        )

    def get_discussion(self, discussion_id: str) -> Optional[Discussion]:
        """
        Retrieve a discussion by ID from DynamoDB.

        Args:
            discussion_id: ID of discussion to retrieve

        Returns:
            Discussion object if found, None otherwise

        Raises:
            DatabaseError: If retrieval operation fails
        """

        def _get() -> Optional[Discussion]:
            response = self.dynamodb_client.get_item(
                TableName=self.discussions_table,
                Key={"id": self.serializer.serialize(discussion_id)},
            )

            # Check if item was found
            if "Item" not in response:
                return None

            # Deserialize and return discussion
            return self._deserialize_discussion(response["Item"])

        return self._execute_with_retry(
            _get, operation_name=f"get_discussion({discussion_id})"
        )

    def get_discussions(
        self,
        limit: int = 21,
        cursor: Optional[Dict[str, Any]] = None,
        mode: Optional[str] = None,
        sort_ascending: bool = False,
        search_all: bool = False,
    ) -> Tuple[List[Discussion], Optional[Dict[str, Any]]]:
        """
        Retrieve discussions with cursor-based pagination via GSI.

        Uses 'CreatedAtIndex' by default, or 'ModeIndex' when mode is specified.

        Args:
            limit: Page size (default 21).
            cursor: LastEvaluatedKey from a previous call.
            mode: Filter by discussion mode ('classic', 'agent', 'interview').
                  When set, uses ModeIndex GSI.
            sort_ascending: If True, sort oldest first.
            search_all: If True, fall back to full scan (for topic search).

        Returns:
            Tuple of (discussions, next_cursor).
        """

        def _query() -> Tuple[List[Discussion], Optional[Dict[str, Any]]]:
            if search_all:
                discussions: List[Discussion] = []
                scan_params: Dict[str, Any] = {"TableName": self.discussions_table}
                response = self.dynamodb_client.scan(**scan_params)
                for item in response.get("Items", []):
                    discussions.append(self._deserialize_discussion(item))
                while "LastEvaluatedKey" in response:
                    scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                    response = self.dynamodb_client.scan(**scan_params)
                    for item in response.get("Items", []):
                        discussions.append(self._deserialize_discussion(item))
                return discussions, None

            if mode:
                # Use ModeIndex (PK=mode, SK=created_at)
                params: Dict[str, Any] = {
                    "TableName": self.discussions_table,
                    "IndexName": "ModeIndex",
                    "KeyConditionExpression": "#m = :m",
                    "ExpressionAttributeNames": {"#m": "mode"},
                    "ExpressionAttributeValues": {":m": {"S": mode}},
                    "ScanIndexForward": sort_ascending,
                    "Limit": limit,
                }
            else:
                # Use CreatedAtIndex (PK=type, SK=created_at)
                params = {
                    "TableName": self.discussions_table,
                    "IndexName": "CreatedAtIndex",
                    "KeyConditionExpression": "#t = :t",
                    "ExpressionAttributeNames": {"#t": "type"},
                    "ExpressionAttributeValues": {":t": {"S": "discussion"}},
                    "ScanIndexForward": sort_ascending,
                    "Limit": limit,
                }
            if cursor:
                params["ExclusiveStartKey"] = cursor
            response = self.dynamodb_client.query(**params)
            discussions = [
                self._deserialize_discussion(item)
                for item in response.get("Items", [])
            ]
            return discussions, response.get("LastEvaluatedKey")

        return self._execute_with_retry(_query, operation_name="get_discussions")

    def delete_discussion(self, discussion_id: str) -> bool:
        """
        Delete a discussion from DynamoDB.

        Args:
            discussion_id: ID of discussion to delete

        Returns:
            True if deletion was successful, False if discussion didn't exist

        Raises:
            DatabaseError: If delete operation fails
        """

        def _delete() -> bool:
            try:
                self.dynamodb_client.delete_item(
                    TableName=self.discussions_table,
                    Key={"id": self.serializer.serialize(discussion_id)},
                )
                # DynamoDB delete_item succeeds even if item doesn't exist
                return True

            except ClientError:
                # Re-raise to be handled by retry logic
                raise

        return self._execute_with_retry(
            _delete, operation_name=f"delete_discussion({discussion_id})"
        )

    def get_discussions_by_topic(
        self, topic_pattern: str, limit: Optional[int] = None
    ) -> List[Discussion]:
        """
        Query discussions by topic pattern using scan with filter.

        Note: This implementation uses scan with filter since GSI requires exact matches.
        For production, consider using begins_with or contains operators with GSI.

        Args:
            topic_pattern: Topic pattern to search for (substring match)
            limit: Maximum number of discussions to return (optional)

        Returns:
            List of Discussion objects matching the topic pattern

        Raises:
            DatabaseError: If query operation fails
        """

        def _query_by_topic() -> list[Discussion]:
            discussions = []

            # Build scan parameters with filter expression
            scan_params: Dict[str, Any] = {
                "TableName": self.discussions_table,
                "FilterExpression": "contains(#topic, :topic_val)",
                "ExpressionAttributeNames": {"#topic": "topic"},
                "ExpressionAttributeValues": {
                    ":topic_val": self.serializer.serialize(topic_pattern)
                },
            }

            if limit:
                scan_params["Limit"] = limit

            # Scan with filter
            response = self.dynamodb_client.scan(**scan_params)

            # Deserialize all items
            for item in response.get("Items", []):
                discussion = self._deserialize_discussion(item)
                discussions.append(discussion)

            # Handle pagination if no limit was specified
            while "LastEvaluatedKey" in response and not limit:
                scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.dynamodb_client.scan(**scan_params)

                for item in response.get("Items", []):
                    discussion = self._deserialize_discussion(item)
                    discussions.append(discussion)

            return discussions

        return self._execute_with_retry(
            _query_by_topic, operation_name=f"get_discussions_by_topic({topic_pattern})"
        )

    def get_discussions_by_participant(
        self, persona_id: str, limit: Optional[int] = None
    ) -> List[Discussion]:
        """
        Query discussions by participant persona ID.

        Args:
            persona_id: ID of persona to search for in participants
            limit: Maximum number of discussions to return (optional)

        Returns:
            List of Discussion objects where persona is a participant

        Raises:
            DatabaseError: If query operation fails
        """

        def _query_by_participant() -> list[Discussion]:
            discussions = []

            # Build scan parameters with filter expression
            # Check if persona_id is in the participants list
            scan_params: Dict[str, Any] = {
                "TableName": self.discussions_table,
                "FilterExpression": "contains(participants, :persona_id)",
                "ExpressionAttributeValues": {
                    ":persona_id": self.serializer.serialize(persona_id)
                },
            }

            if limit:
                scan_params["Limit"] = limit

            # Scan with filter
            response = self.dynamodb_client.scan(**scan_params)

            # Deserialize all items
            for item in response.get("Items", []):
                discussion = self._deserialize_discussion(item)
                discussions.append(discussion)

            # Handle pagination if no limit was specified
            while "LastEvaluatedKey" in response and not limit:
                scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.dynamodb_client.scan(**scan_params)

                for item in response.get("Items", []):
                    discussion = self._deserialize_discussion(item)
                    discussions.append(discussion)

            return discussions

        return self._execute_with_retry(
            _query_by_participant,
            operation_name=f"get_discussions_by_participant({persona_id})",
        )

    def get_discussions_by_date_range(
        self, start_date: datetime, end_date: datetime, limit: Optional[int] = None
    ) -> List[Discussion]:
        """
        Query discussions by date range using scan with filter.

        Note: For production with GSI, this could use Query operation on CreatedAtIndex.

        Args:
            start_date: Start of date range (inclusive)
            end_date: End of date range (inclusive)
            limit: Maximum number of discussions to return (optional)

        Returns:
            List of Discussion objects created within the date range

        Raises:
            DatabaseError: If query operation fails
        """

        def _query_by_date_range() -> list[Discussion]:
            discussions = []

            # Convert dates to ISO format strings
            start_iso = start_date.isoformat()
            end_iso = end_date.isoformat()

            # Build scan parameters with filter expression
            scan_params: Dict[str, Any] = {
                "TableName": self.discussions_table,
                "FilterExpression": "#created_at BETWEEN :start_date AND :end_date",
                "ExpressionAttributeNames": {"#created_at": "created_at"},
                "ExpressionAttributeValues": {
                    ":start_date": self.serializer.serialize(start_iso),
                    ":end_date": self.serializer.serialize(end_iso),
                },
            }

            if limit:
                scan_params["Limit"] = limit

            # Scan with filter
            response = self.dynamodb_client.scan(**scan_params)

            # Deserialize all items
            for item in response.get("Items", []):
                discussion = self._deserialize_discussion(item)
                discussions.append(discussion)

            # Handle pagination if no limit was specified
            while "LastEvaluatedKey" in response and not limit:
                scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.dynamodb_client.scan(**scan_params)

                for item in response.get("Items", []):
                    discussion = self._deserialize_discussion(item)
                    discussions.append(discussion)

            return discussions

        return self._execute_with_retry(
            _query_by_date_range,
            operation_name=f"get_discussions_by_date_range({start_date}, {end_date})",
        )

    def get_discussions_by_mode(
        self, mode: str, limit: Optional[int] = None
    ) -> List[Discussion]:
        """
        Query discussions by mode using scan with filter.

        Note: For production with GSI (ModeIndex), this could use Query operation.

        Args:
            mode: Discussion mode to filter by (e.g., "traditional" or "agent")
            limit: Maximum number of discussions to return (optional)

        Returns:
            List of Discussion objects with the specified mode

        Raises:
            DatabaseError: If query operation fails
        """

        def _query_by_mode() -> list[Discussion]:
            discussions = []

            # Build scan parameters with filter expression
            scan_params: Dict[str, Any] = {
                "TableName": self.discussions_table,
                "FilterExpression": "#mode = :mode_val",
                "ExpressionAttributeNames": {"#mode": "mode"},
                "ExpressionAttributeValues": {
                    ":mode_val": self.serializer.serialize(mode)
                },
            }

            if limit:
                scan_params["Limit"] = limit

            # Scan with filter
            response = self.dynamodb_client.scan(**scan_params)

            # Deserialize all items
            for item in response.get("Items", []):
                discussion = self._deserialize_discussion(item)
                discussions.append(discussion)

            # Handle pagination if no limit was specified
            while "LastEvaluatedKey" in response and not limit:
                scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.dynamodb_client.scan(**scan_params)

                for item in response.get("Items", []):
                    discussion = self._deserialize_discussion(item)
                    discussions.append(discussion)

            return discussions

        return self._execute_with_retry(
            _query_by_mode, operation_name=f"get_discussions_by_mode({mode})"
        )

    def discussion_exists(self, discussion_id: str) -> bool:
        """
        Check if a discussion exists in DynamoDB.

        Args:
            discussion_id: ID of discussion to check

        Returns:
            True if discussion exists, False otherwise

        Raises:
            DatabaseError: If check operation fails
        """

        def _exists() -> bool:
            response = self.dynamodb_client.get_item(
                TableName=self.discussions_table,
                Key={"id": self.serializer.serialize(discussion_id)},
                # Only retrieve the key to minimize data transfer
                ProjectionExpression="id",
            )

            return "Item" in response

        return self._execute_with_retry(
            _exists, operation_name=f"discussion_exists({discussion_id})"
        )

    def get_discussion_count(self) -> int:
        """
        Get the total count of discussions in DynamoDB.

        Returns:
            Number of discussions

        Raises:
            DatabaseError: If count operation fails
        """

        def _count() -> int:
            # Use scan with Select='COUNT' for efficient counting
            response = self.dynamodb_client.scan(
                TableName=self.discussions_table, Select="COUNT"
            )

            count: int = response.get("Count", 0)

            # Handle pagination to get accurate count
            while "LastEvaluatedKey" in response:
                response = self.dynamodb_client.scan(
                    TableName=self.discussions_table,
                    Select="COUNT",
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                count += int(response.get("Count", 0))

            return count

        return self._execute_with_retry(_count, operation_name="get_discussion_count")

    def update_discussion_insights(
        self, discussion_id: str, insights: List["Insight"]
    ) -> bool:
        """
        Update insights for an existing discussion in DynamoDB.

        Args:
            discussion_id: ID of discussion to update
            insights: List of Insight objects to set

        Returns:
            True if update was successful, False if discussion doesn't exist

        Raises:
            DatabaseError: If update operation fails
        """

        def _update() -> bool:
            # Convert insights to dictionaries
            insights_list = []
            for insight in insights:
                insight_dict = {
                    "category": insight.category,
                    "description": insight.description,
                    "supporting_messages": insight.supporting_messages,
                    "confidence_score": Decimal(str(insight.confidence_score)),
                }
                insights_list.append(insight_dict)

            # Serialize the insights list
            serialized_insights = self.serializer.serialize(insights_list)

            # Update the insights attribute
            try:
                self.dynamodb_client.update_item(
                    TableName=self.discussions_table,
                    Key={"id": self.serializer.serialize(discussion_id)},
                    UpdateExpression="SET insights = :insights",
                    ExpressionAttributeValues={":insights": serialized_insights},
                    ConditionExpression="attribute_exists(id)",
                )
                return True

            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    # Discussion doesn't exist
                    return False
                # Re-raise other errors to be handled by retry logic
                raise

        return self._execute_with_retry(
            _update, operation_name=f"update_discussion_insights({discussion_id})"
        )

    def get_insights_by_category(
        self, category: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Query insights by category across all discussions.

        This scans all discussions and extracts insights matching the category.

        Args:
            category: Insight category to filter by
            limit: Maximum number of insights to return (optional)

        Returns:
            List of insight dictionaries with discussion_id added

        Raises:
            DatabaseError: If query operation fails
        """

        def _query_insights_by_category() -> list[dict[str, Any]]:
            insights_result = []

            # Scan all discussions
            scan_params: Dict[str, Any] = {"TableName": self.discussions_table}

            response = self.dynamodb_client.scan(**scan_params)

            # Process all discussions
            while True:
                for item in response.get("Items", []):
                    discussion = self._deserialize_discussion(item)

                    # Extract insights matching the category
                    for insight in discussion.insights:
                        if insight.category == category:
                            insight_dict = {
                                "discussion_id": discussion.id,
                                "category": insight.category,
                                "description": insight.description,
                                "supporting_messages": insight.supporting_messages,
                                "confidence_score": insight.confidence_score,
                            }
                            insights_result.append(insight_dict)

                            # Check if we've reached the limit
                            if limit and len(insights_result) >= limit:
                                return insights_result[:limit]

                # Check for more pages
                if "LastEvaluatedKey" in response:
                    scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                    response = self.dynamodb_client.scan(**scan_params)
                else:
                    break

            return insights_result

        return self._execute_with_retry(
            _query_insights_by_category,
            operation_name=f"get_insights_by_category({category})",
        )

    def save_uploaded_file_info(
        self,
        file_id: str,
        filename: str,
        file_path: str,
        file_size: Optional[int] = None,
        file_hash: Optional[str] = None,
        mime_type: Optional[str] = None,
        uploaded_at: Optional[datetime] = None,
        original_filename: Optional[str] = None,
        file_type: str = "persona_interview",
    ) -> str:
        """
        Save uploaded file metadata to DynamoDB.

        Args:
            file_id: Unique file identifier
            filename: Saved filename (with UUID prefix)
            file_path: Path to stored file
            file_size: File size in bytes (optional)
            file_hash: File hash for integrity checking (optional)
            mime_type: MIME type of file (optional)
            uploaded_at: Upload timestamp (optional, defaults to now)
            original_filename: Original filename before UUID prefix (optional)
            file_type: File type ('persona_interview' or 'discussion_document')

        Returns:
            File ID

        Raises:
            DatabaseError: If save operation fails
        """

        def _save() -> str:
            serialized_file = self._serialize_file_info(
                file_id=file_id,
                filename=filename,
                file_path=file_path,
                file_size=file_size,
                file_hash=file_hash,
                mime_type=mime_type,
                uploaded_at=uploaded_at,
                original_filename=original_filename,
                file_type=file_type,
            )

            self.dynamodb_client.put_item(
                TableName=self.files_table, Item=serialized_file
            )

            return file_id

        return self._execute_with_retry(
            _save, operation_name=f"save_uploaded_file_info({file_id})"
        )

    def get_uploaded_file_info(self, file_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve uploaded file metadata from DynamoDB.

        Args:
            file_id: ID of file to retrieve

        Returns:
            Dictionary with file metadata if found, None otherwise

        Raises:
            DatabaseError: If retrieval operation fails
        """

        def _get() -> Optional[Dict[str, Any]]:
            response = self.dynamodb_client.get_item(
                TableName=self.files_table,
                Key={"id": self.serializer.serialize(file_id)},
            )

            # Check if item was found
            if "Item" not in response:
                return None

            # Deserialize and return file info
            return self._deserialize_file_info(response["Item"])

        return self._execute_with_retry(
            _get, operation_name=f"get_uploaded_file_info({file_id})"
        )

    def get_all_uploaded_files(
        self, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieve all uploaded file metadata from DynamoDB.

        Args:
            limit: Maximum number of files to return (optional)

        Returns:
            List of dictionaries with file metadata

        Raises:
            DatabaseError: If retrieval operation fails
        """

        def _get_all() -> list[dict[str, Any]]:
            files = []

            # Build scan parameters
            scan_params: Dict[str, Any] = {"TableName": self.files_table}

            if limit:
                scan_params["Limit"] = limit

            # Scan the table
            response = self.dynamodb_client.scan(**scan_params)

            # Deserialize all items
            for item in response.get("Items", []):
                file_info = self._deserialize_file_info(item)
                files.append(file_info)

            # Handle pagination if no limit was specified
            while "LastEvaluatedKey" in response and not limit:
                scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self.dynamodb_client.scan(**scan_params)

                for item in response.get("Items", []):
                    file_info = self._deserialize_file_info(item)
                    files.append(file_info)

            return files

        return self._execute_with_retry(
            _get_all, operation_name="get_all_uploaded_files"
        )

    def delete_uploaded_file_info(self, file_id: str) -> bool:
        """
        Delete uploaded file metadata from DynamoDB.

        Args:
            file_id: ID of file to delete

        Returns:
            True if deletion was successful

        Raises:
            DatabaseError: If delete operation fails
        """

        def _delete() -> bool:
            self.dynamodb_client.delete_item(
                TableName=self.files_table,
                Key={"id": self.serializer.serialize(file_id)},
            )
            # DynamoDB delete_item succeeds even if item doesn't exist
            return True

        return self._execute_with_retry(
            _delete, operation_name=f"delete_uploaded_file_info({file_id})"
        )

    # ==================== Dataset Operations ====================

    def save_dataset(self, dataset: "Dataset") -> "Dataset":
        """Save dataset to DynamoDB."""

        def _save() -> "Dataset":
            item = {
                "id": self.serializer.serialize(dataset.id),
                "name": self.serializer.serialize(dataset.name),
                "description": self.serializer.serialize(dataset.description),
                "s3_path": self.serializer.serialize(dataset.s3_path),
                "columns": self.serializer.serialize(
                    [
                        {
                            "name": c.name,
                            "data_type": c.data_type,
                            "description": c.description,
                        }
                        for c in dataset.columns
                    ]
                ),
                "row_count": self.serializer.serialize(dataset.row_count),
                "created_at": self.serializer.serialize(dataset.created_at.isoformat()),
                "updated_at": self.serializer.serialize(dataset.updated_at.isoformat()),
                "notes": self.serializer.serialize(dataset.notes),
            }
            self.dynamodb_client.put_item(TableName=self.datasets_table, Item=item)
            return dataset

        return self._execute_with_retry(
            _save, operation_name=f"save_dataset({dataset.id})"
        )

    def get_dataset(self, dataset_id: str) -> Optional["Dataset"]:
        """Get dataset by ID."""
        from ..models.dataset import Dataset, DatasetColumn

        def _get() -> Optional["Dataset"]:
            response = self.dynamodb_client.get_item(
                TableName=self.datasets_table,
                Key={"id": self.serializer.serialize(dataset_id)},
            )
            if "Item" not in response:
                return None
            item = {
                k: self.deserializer.deserialize(v) for k, v in response["Item"].items()
            }
            item["columns"] = [DatasetColumn(**c) for c in item.get("columns", [])]
            item["created_at"] = datetime.fromisoformat(item["created_at"])
            item["updated_at"] = datetime.fromisoformat(item["updated_at"])
            return Dataset(**item)

        return self._execute_with_retry(
            _get, operation_name=f"get_dataset({dataset_id})"
        )

    def get_all_datasets(self) -> List["Dataset"]:
        """Get all datasets."""
        from ..models.dataset import Dataset, DatasetColumn

        def _get_all() -> List["Dataset"]:
            datasets = []
            response = self.dynamodb_client.scan(TableName=self.datasets_table)
            for raw_item in response.get("Items", []):
                item = {
                    k: self.deserializer.deserialize(v) for k, v in raw_item.items()
                }
                item["columns"] = [DatasetColumn(**c) for c in item.get("columns", [])]
                item["created_at"] = datetime.fromisoformat(item["created_at"])
                item["updated_at"] = datetime.fromisoformat(item["updated_at"])
                datasets.append(Dataset(**item))
            return datasets

        return self._execute_with_retry(_get_all, operation_name="get_all_datasets")

    def delete_dataset(self, dataset_id: str) -> bool:
        """Delete dataset by ID."""

        def _delete() -> bool:
            self.dynamodb_client.delete_item(
                TableName=self.datasets_table,
                Key={"id": self.serializer.serialize(dataset_id)},
            )
            return True

        return self._execute_with_retry(
            _delete, operation_name=f"delete_dataset({dataset_id})"
        )

    # ==================== PersonaDatasetBinding Operations ====================

    def save_binding(self, binding: "PersonaDatasetBinding") -> "PersonaDatasetBinding":
        """Save persona-dataset binding."""

        def _save() -> "PersonaDatasetBinding":
            item = {
                "id": self.serializer.serialize(binding.id),
                "persona_id": self.serializer.serialize(binding.persona_id),
                "dataset_id": self.serializer.serialize(binding.dataset_id),
                "binding_keys": self.serializer.serialize(binding.binding_keys),
                "created_at": self.serializer.serialize(binding.created_at.isoformat()),
            }
            self.dynamodb_client.put_item(TableName=self.bindings_table, Item=item)
            return binding

        return self._execute_with_retry(
            _save, operation_name=f"save_binding({binding.id})"
        )

    def get_bindings_by_persona(self, persona_id: str) -> List["PersonaDatasetBinding"]:
        """Get all bindings for a persona."""
        from ..models.dataset import PersonaDatasetBinding

        def _get() -> List["PersonaDatasetBinding"]:
            bindings = []
            response = self.dynamodb_client.scan(
                TableName=self.bindings_table,
                FilterExpression="persona_id = :pid",
                ExpressionAttributeValues={
                    ":pid": self.serializer.serialize(persona_id)
                },
            )
            for raw_item in response.get("Items", []):
                item = {
                    k: self.deserializer.deserialize(v) for k, v in raw_item.items()
                }
                item["created_at"] = datetime.fromisoformat(item["created_at"])
                bindings.append(PersonaDatasetBinding(**item))
            return bindings

        return self._execute_with_retry(
            _get, operation_name=f"get_bindings_by_persona({persona_id})"
        )

    def delete_binding(self, binding_id: str) -> bool:
        """Delete binding by ID."""

        def _delete() -> bool:
            self.dynamodb_client.delete_item(
                TableName=self.bindings_table,
                Key={"id": self.serializer.serialize(binding_id)},
            )
            return True

        return self._execute_with_retry(
            _delete, operation_name=f"delete_binding({binding_id})"
        )

    def delete_bindings_by_persona(self, persona_id: str) -> int:
        """Delete all bindings for a persona. Returns count of deleted items."""
        bindings = self.get_bindings_by_persona(persona_id)
        for b in bindings:
            self.delete_binding(b.id)
        return len(bindings)

    # ========== SurveyTemplate CRUD ==========

    def _serialize_survey_template(self, template: "SurveyTemplate") -> Dict[str, Any]:
        """Serialize SurveyTemplate to DynamoDB item format."""
        data = template.to_dict()
        return {k: self.serializer.serialize(v) for k, v in data.items()}

    def _deserialize_survey_template(self, item: Dict[str, Any]) -> "SurveyTemplate":
        """Deserialize DynamoDB item to SurveyTemplate."""
        from ..models.survey_template import SurveyTemplate

        deserialized = {k: self.deserializer.deserialize(v) for k, v in item.items()}
        # Convert Decimal scale values back to int inside questions
        for q in deserialized.get("questions", []):
            if "scale_min" in q:
                q["scale_min"] = int(q["scale_min"])
            if "scale_max" in q:
                q["scale_max"] = int(q["scale_max"])
        return SurveyTemplate.from_dict(deserialized)

    def save_survey_template(self, template: "SurveyTemplate") -> str:
        """Save a SurveyTemplate to DynamoDB. Returns the template ID."""

        def _save() -> str:
            item = self._serialize_survey_template(template)
            self.dynamodb_client.put_item(
                TableName=self.survey_templates_table, Item=item
            )
            return template.id

        return self._execute_with_retry(
            _save, operation_name=f"save_survey_template({template.id})"
        )

    def get_survey_template(self, template_id: str) -> Optional["SurveyTemplate"]:
        """Get a SurveyTemplate by ID."""

        def _get() -> Optional["SurveyTemplate"]:
            response = self.dynamodb_client.get_item(
                TableName=self.survey_templates_table,
                Key={"id": self.serializer.serialize(template_id)},
            )
            if "Item" not in response:
                return None
            return self._deserialize_survey_template(response["Item"])

        return self._execute_with_retry(
            _get, operation_name=f"get_survey_template({template_id})"
        )

    def get_all_survey_templates(self) -> List["SurveyTemplate"]:
        """Get all SurveyTemplates."""

        def _get_all() -> List["SurveyTemplate"]:
            templates = []
            response = self.dynamodb_client.scan(TableName=self.survey_templates_table)
            for raw_item in response.get("Items", []):
                templates.append(self._deserialize_survey_template(raw_item))
            return templates

        return self._execute_with_retry(
            _get_all, operation_name="get_all_survey_templates"
        )

    def update_survey_template(self, template: "SurveyTemplate") -> bool:
        """Update an existing SurveyTemplate. Returns True on success."""

        def _update() -> bool:
            item = self._serialize_survey_template(template)
            self.dynamodb_client.put_item(
                TableName=self.survey_templates_table, Item=item
            )
            return True

        return self._execute_with_retry(
            _update, operation_name=f"update_survey_template({template.id})"
        )

    def delete_survey_template(self, template_id: str) -> bool:
        """Delete a SurveyTemplate by ID. Returns True on success."""

        def _delete() -> bool:
            self.dynamodb_client.delete_item(
                TableName=self.survey_templates_table,
                Key={"id": self.serializer.serialize(template_id)},
            )
            return True

        return self._execute_with_retry(
            _delete, operation_name=f"delete_survey_template({template_id})"
        )

    # ========== Survey CRUD ==========

    def _serialize_survey(self, survey: "Survey") -> Dict[str, Any]:
        """Serialize Survey to DynamoDB item format."""
        data = survey.to_dict()
        return {
            k: self.serializer.serialize(v) for k, v in data.items() if v is not None
        }

    def _deserialize_survey(self, item: Dict[str, Any]) -> "Survey":
        """Deserialize DynamoDB item to Survey."""
        from ..models.survey import Survey

        deserialized = {k: self.deserializer.deserialize(v) for k, v in item.items()}
        # Convert Decimal persona_count back to int
        deserialized["persona_count"] = int(deserialized["persona_count"])
        return Survey.from_dict(deserialized)

    def save_survey(self, survey: "Survey") -> str:
        """Save a Survey to DynamoDB. Returns the survey ID."""

        def _save() -> str:
            item = self._serialize_survey(survey)
            self.dynamodb_client.put_item(TableName=self.surveys_table, Item=item)
            return survey.id

        return self._execute_with_retry(
            _save, operation_name=f"save_survey({survey.id})"
        )

    def get_survey(self, survey_id: str) -> Optional["Survey"]:
        """Get a Survey by ID."""

        def _get() -> Optional["Survey"]:
            response = self.dynamodb_client.get_item(
                TableName=self.surveys_table,
                Key={"id": self.serializer.serialize(survey_id)},
            )
            if "Item" not in response:
                return None
            return self._deserialize_survey(response["Item"])

        return self._execute_with_retry(_get, operation_name=f"get_survey({survey_id})")

    def get_all_surveys(self) -> List["Survey"]:
        """Get all Surveys."""

        def _get_all() -> List["Survey"]:
            surveys = []
            response = self.dynamodb_client.scan(TableName=self.surveys_table)
            for raw_item in response.get("Items", []):
                surveys.append(self._deserialize_survey(raw_item))
            return surveys

        return self._execute_with_retry(_get_all, operation_name="get_all_surveys")

    def update_survey(self, survey: "Survey") -> bool:
        """Update an existing Survey. Returns True on success."""

        def _update() -> bool:
            item = self._serialize_survey(survey)
            self.dynamodb_client.put_item(TableName=self.surveys_table, Item=item)
            return True

        return self._execute_with_retry(
            _update, operation_name=f"update_survey({survey.id})"
        )

    def delete_survey(self, survey_id: str) -> bool:
        """Delete a Survey by ID. Returns True on success."""

        def _delete() -> bool:
            self.dynamodb_client.delete_item(
                TableName=self.surveys_table,
                Key={"id": self.serializer.serialize(survey_id)},
            )
            return True

        return self._execute_with_retry(
            _delete, operation_name=f"delete_survey({survey_id})"
        )

    # ==================== KnowledgeBase Operations ====================

    def save_knowledge_base(self, kb: "KnowledgeBase") -> "KnowledgeBase":
        """Save a KnowledgeBase entry."""

        def _save() -> "KnowledgeBase":
            item = {k: self.serializer.serialize(v) for k, v in kb.to_dict().items()}
            self.dynamodb_client.put_item(TableName=self.kb_table, Item=item)
            return kb

        return self._execute_with_retry(
            _save, operation_name=f"save_knowledge_base({kb.id})"
        )

    def get_knowledge_base(self, kb_id: str) -> Optional["KnowledgeBase"]:
        """Get a KnowledgeBase by internal ID."""
        from ..models.knowledge_base import KnowledgeBase

        def _get() -> Optional["KnowledgeBase"]:
            response = self.dynamodb_client.get_item(
                TableName=self.kb_table,
                Key={"id": self.serializer.serialize(kb_id)},
            )
            raw_item = response.get("Item")
            if not raw_item:
                return None
            item = {k: self.deserializer.deserialize(v) for k, v in raw_item.items()}
            return KnowledgeBase.from_dict(item)

        return self._execute_with_retry(
            _get, operation_name=f"get_knowledge_base({kb_id})"
        )

    def get_all_knowledge_bases(self) -> List["KnowledgeBase"]:
        """Get all registered KnowledgeBases."""
        from ..models.knowledge_base import KnowledgeBase

        def _get_all() -> List["KnowledgeBase"]:
            kbs = []
            response = self.dynamodb_client.scan(TableName=self.kb_table)
            for raw_item in response.get("Items", []):
                item = {
                    k: self.deserializer.deserialize(v) for k, v in raw_item.items()
                }
                kbs.append(KnowledgeBase.from_dict(item))
            return kbs

        return self._execute_with_retry(
            _get_all, operation_name="get_all_knowledge_bases"
        )

    def delete_knowledge_base(self, kb_id: str) -> bool:
        """Delete a KnowledgeBase by ID."""

        def _delete() -> bool:
            self.dynamodb_client.delete_item(
                TableName=self.kb_table,
                Key={"id": self.serializer.serialize(kb_id)},
            )
            return True

        return self._execute_with_retry(
            _delete, operation_name=f"delete_knowledge_base({kb_id})"
        )

    # ==================== PersonaKBBinding Operations ====================

    def save_kb_binding(self, binding: "PersonaKBBinding") -> "PersonaKBBinding":
        """Save persona-KB binding. Overwrites existing binding for the persona."""

        def _save() -> "PersonaKBBinding":
            # 既存の紐付けを削除（1ペルソナ:1KB制約）
            existing = self.get_kb_binding_by_persona(binding.persona_id)
            if existing:
                self.delete_kb_binding(existing.id)

            item = {
                k: self.serializer.serialize(v) for k, v in binding.to_dict().items()
            }
            self.dynamodb_client.put_item(
                TableName=self.persona_kb_bindings_table, Item=item
            )
            return binding

        return self._execute_with_retry(
            _save, operation_name=f"save_kb_binding({binding.id})"
        )

    def get_kb_binding_by_persona(
        self, persona_id: str
    ) -> Optional["PersonaKBBinding"]:
        """Get KB binding for a persona (at most one)."""
        from ..models.knowledge_base import PersonaKBBinding

        def _get() -> Optional["PersonaKBBinding"]:
            response = self.dynamodb_client.scan(
                TableName=self.persona_kb_bindings_table,
                FilterExpression="persona_id = :pid",
                ExpressionAttributeValues={
                    ":pid": self.serializer.serialize(persona_id)
                },
            )
            items = response.get("Items", [])
            if not items:
                return None
            item = {
                k: self.deserializer.deserialize(v) for k, v in items[0].items()
            }
            return PersonaKBBinding.from_dict(item)

        return self._execute_with_retry(
            _get, operation_name=f"get_kb_binding_by_persona({persona_id})"
        )

    def delete_kb_binding(self, binding_id: str) -> bool:
        """Delete KB binding by ID."""

        def _delete() -> bool:
            self.dynamodb_client.delete_item(
                TableName=self.persona_kb_bindings_table,
                Key={"id": self.serializer.serialize(binding_id)},
            )
            return True

        return self._execute_with_retry(
            _delete, operation_name=f"delete_kb_binding({binding_id})"
        )

    def delete_kb_bindings_by_persona(self, persona_id: str) -> int:
        """Delete KB binding for a persona. Returns count of deleted items."""
        binding = self.get_kb_binding_by_persona(persona_id)
        if binding:
            self.delete_kb_binding(binding.id)
            return 1
        return 0
