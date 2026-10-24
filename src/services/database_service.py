"""
Database service for AI Persona System.
Handles DynamoDB database operations with retry logic and error handling.
"""

import logging
import random
import time
from decimal import Decimal
from typing import (
    List,
    Optional,
    Dict,
    Any,
    Callable,
    Mapping,
    Sequence,
    TypeVar,
    TYPE_CHECKING,
)
from datetime import datetime
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

# Import models
from ..models.errors import CodedError, ErrorCode
from ..models.persona import Persona

if TYPE_CHECKING:
    from ..models.survey_template import SurveyTemplate
    from ..models.survey import Survey


class DatabaseError(CodedError):
    """Custom exception for database-related errors."""

    pass


class DatabaseConditionFailed(DatabaseError):
    """A conditional write found the item missing or already there (U7)."""


_T = TypeVar("_T")

#: Columns ``update_survey_fields`` never writes: the owner and the shares
#: belong to ``ResourceSharing`` (contract C7), the keys never change (BR1.5).
_PROTECTED_COLUMNS = frozenset(
    {"id", "owner_sub", "shared_group_ids", "created_at", "template_snapshot"}
)


def _to_dynamo(value: Any) -> Any:
    """Floats become ``Decimal`` (``TypeSerializer`` refuses floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _attribute_size(value: Mapping[str, Any]) -> int:
    """Approximate DynamoDB size of one typed attribute value."""
    ((kind, inner),) = value.items()
    if kind == "S":
        return len(str(inner).encode("utf-8"))
    if kind == "N":
        return len(str(inner)) // 2 + 2
    if kind == "B":
        return len(inner)
    if kind in ("BOOL", "NULL"):
        return 1
    if kind == "M":
        return 3 + sum(
            len(k.encode("utf-8")) + _attribute_size(v) + 1 for k, v in inner.items()
        )
    if kind == "L":
        return 3 + sum(_attribute_size(v) + 1 for v in inner)
    return sum(len(str(v).encode("utf-8")) for v in inner)  # SS / NS / BS


def _only_conditions_failed(error: ClientError) -> bool:
    """Whether a write failed only because a condition did not hold."""
    code = error.response.get("Error", {}).get("Code")
    if code == "ConditionalCheckFailedException":
        return True
    reasons = [r.get("Code") for r in error.response.get("CancellationReasons") or []]
    return "ConditionalCheckFailed" in reasons and all(
        r in ("ConditionalCheckFailed", "None", None) for r in reasons
    )


class DatabaseService:
    """Service for managing DynamoDB database operations."""

    def __init__(
        self,
        table_prefix: str = "AIPersona",
        region: str = "us-east-1",
        table_names: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize DynamoDB service with AWS configuration.

        Args:
            table_prefix: Prefix for DynamoDB table names
            region: AWS region for DynamoDB
            table_names: Physical names by table kind (``"Personas"`` etc.),
                as created by CDK. Kinds missing here fall back to
                ``{table_prefix}_{kind}``.

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
            raise DatabaseError(
                error_msg, code=ErrorCode.DATABASE_CREDENTIALS_INVALID
            ) from e

        except Exception as e:
            error_msg = f"Failed to initialize DynamoDB client: {e}"
            self.logger.error(error_msg)
            raise DatabaseError(
                error_msg, code=ErrorCode.DATABASE_OPERATION_FAILED
            ) from e

        # Initialize type serializer/deserializer for DynamoDB
        self.serializer = TypeSerializer()
        self.deserializer = TypeDeserializer()

        # Table names: CDK-generated physical names when given, else prefix-based
        names = table_names or {}

        def name_of(kind: str) -> str:
            return names.get(kind) or f"{table_prefix}_{kind}"

        self.personas_table = name_of("Personas")
        self.discussions_table = name_of("Discussions")
        self.files_table = name_of("UploadedFiles")
        self.survey_templates_table = name_of("SurveyTemplates")
        self.surveys_table = name_of("Surveys")

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
                        raise DatabaseError(
                            error_msg, code=ErrorCode.DATABASE_OPERATION_FAILED
                        ) from e

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
                        raise DatabaseError(
                            error_msg, code=ErrorCode.DATABASE_OPERATION_FAILED
                        ) from e

                # Handle conditional check failures (not retryable)
                elif error_code == "ConditionalCheckFailedException":
                    self.logger.info(f"{operation_name} conditional check failed: {e}")
                    raise DatabaseError(
                        f"conditional check failed for {operation_name}",
                        code=ErrorCode.DATABASE_OPERATION_FAILED,
                    ) from e

                # Handle resource not found (not retryable)
                elif error_code == "ResourceNotFoundException":
                    error_msg = (
                        f"{operation_name} failed: Table not found. "
                        "Please ensure DynamoDB tables are created. "
                        f"Details: {e}"
                    )
                    self.logger.error(error_msg)
                    raise DatabaseError(
                        error_msg, code=ErrorCode.DATABASE_TABLES_NOT_FOUND
                    ) from e

                # Handle validation errors (not retryable)
                elif error_code == "ValidationException":
                    error_msg = f"{operation_name} validation error: {e}"
                    self.logger.error(error_msg)
                    raise DatabaseError(
                        error_msg, code=ErrorCode.DATABASE_OPERATION_FAILED
                    ) from e

                # Handle other client errors (not retryable)
                else:
                    error_msg = f"{operation_name} failed with {error_code}: {e}"
                    self.logger.error(error_msg)
                    raise DatabaseError(
                        error_msg, code=ErrorCode.DATABASE_OPERATION_FAILED
                    ) from e

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
                    raise DatabaseError(
                        error_msg, code=ErrorCode.DATABASE_OPERATION_FAILED
                    ) from e

            except Exception as e:
                # Unexpected errors are not retried
                error_msg = f"{operation_name} failed with unexpected error: {e}"
                self.logger.error(error_msg)
                raise DatabaseError(
                    error_msg, code=ErrorCode.DATABASE_OPERATION_FAILED
                ) from e

        # Should not reach here, but handle it just in case
        error_msg = (
            f"{operation_name} failed after {max_retries} attempts. "
            f"Last error: {last_exception}"
        )
        self.logger.error(error_msg)
        raise DatabaseError(error_msg, code=ErrorCode.DATABASE_OPERATION_FAILED)

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
                    f"required DynamoDB tables not found: {self.personas_table}, "
                    f"{self.discussions_table}, {self.files_table}",
                    code=ErrorCode.DATABASE_TABLES_NOT_FOUND,
                )
            self.logger.info("Database initialized successfully")
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(
                f"database initialization failed ({type(e).__name__})",
                code=ErrorCode.DATABASE_OPERATION_FAILED,
            ) from e

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

        # Optional demographic fields (omit None/empty for backward compatibility)
        if persona.gender is not None:
            persona_dict["gender"] = persona.gender
        if persona.country is not None:
            persona_dict["country"] = persona.country
        if persona.city is not None:
            persona_dict["city"] = persona.city
        if persona.tags:
            persona_dict["tags"] = persona.tags

        # Optional fields
        if persona.generation_log is not None:
            persona_dict["generation_log"] = persona.generation_log
        if persona.generation_context is not None:
            persona_dict["generation_context"] = persona.generation_context
        if persona.avatar_path is not None:
            persona_dict["avatar_path"] = persona.avatar_path
        # Owner of a new persona (BR6.1). shared_group_ids is written only by
        # ResourceSharing together with ResourceShares (BR1.6), never here.
        if persona.owner_sub is not None:
            persona_dict["owner_sub"] = persona.owner_sub

        # Use boto3 TypeSerializer to convert to DynamoDB format
        serialized = {}
        for key, value in persona_dict.items():
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
            gender=deserialized.get("gender"),
            country=deserialized.get("country"),
            city=deserialized.get("city"),
            tags=deserialized.get("tags", []),
            generation_log=deserialized.get("generation_log"),
            generation_context=deserialized.get("generation_context"),
            avatar_path=deserialized.get("avatar_path"),
            owner_sub=deserialized.get("owner_sub") or None,
            shared_group_ids=frozenset(deserialized.get("shared_group_ids") or ()),
        )

    # Persona CRUD operations

    def save_persona(self, persona: Persona) -> str:
        """
        Save a new persona to DynamoDB.

        The write is conditioned on the id not existing yet, so a save can
        never overwrite a stored persona: its ``owner_sub`` (BR6.1) and its
        ``shared_group_ids`` (kept in step with ``ResourceShares``) stay as
        they are. Edits go through :meth:`update_persona`.

        Args:
            persona: Persona object to save

        Returns:
            Persona ID

        Raises:
            DatabaseError: If save operation fails, including when a persona
                with the same id already exists (conditional check failed)
        """

        def _save() -> str:
            serialized_persona = self._serialize_persona(persona)

            self.dynamodb_client.put_item(
                TableName=self.personas_table,
                Item=serialized_persona,
                ConditionExpression="attribute_not_exists(id)",
            )

            return persona.id

        return self._execute_with_retry(
            _save, operation_name=f"save_persona({persona.id})"
        )

    def update_persona(self, persona: Persona) -> bool:
        """
        Write the columns the edit screen can change (BR6.7).

        A partial ``UpdateItem`` conditioned on the persona still existing.
        ``owner_sub``, ``shared_group_ids``, ``avatar_path``, the generation
        log and ``created_at`` are never part of the write, so editing a shared
        persona leaves its owner and shares (and ResourceShares) untouched.
        Optional columns that became empty are removed.

        Args:
            persona: Persona object with updated data

        Returns:
            True if written, False if the persona does not exist

        Raises:
            DatabaseError: If update operation fails
        """
        required: Dict[str, Any] = {
            "name": persona.name,
            "age": persona.age,
            "occupation": persona.occupation,
            "background": persona.background,
            "values": persona.values,
            "pain_points": persona.pain_points,
            "goals": persona.goals,
            "updated_at": persona.updated_at.isoformat(),
        }
        optional: Dict[str, Any] = {
            "gender": persona.gender,
            "country": persona.country,
            "city": persona.city,
            "tags": persona.tags or None,
        }
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        sets: list[str] = []
        removes: list[str] = []
        for index, (column, value) in enumerate({**required, **optional}.items()):
            names[f"#f{index}"] = column
            if value is None:
                removes.append(f"#f{index}")
            else:
                values[f":f{index}"] = self.serializer.serialize(value)
                sets.append(f"#f{index} = :f{index}")
        expression = "SET " + ", ".join(sets)
        if removes:
            expression += " REMOVE " + ", ".join(removes)

        def _update() -> bool:
            try:
                self.dynamodb_client.update_item(
                    TableName=self.personas_table,
                    Key={"id": self.serializer.serialize(persona.id)},
                    UpdateExpression=expression,
                    ConditionExpression="attribute_exists(id)",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )
                return True
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    return False
                raise

        return self._execute_with_retry(
            _update, operation_name=f"update_persona({persona.id})"
        )

    def update_persona_avatar(
        self,
        persona_id: str,
        new_path: Optional[str],
        expected_path: Optional[str],
        updated_at: datetime,
    ) -> bool:
        """
        Set or remove a persona's avatar path with a compare-and-set condition.

        Only ``avatar_path`` and ``updated_at`` are written, so fields changed by
        other operations are not overwritten. The write succeeds only if the
        persona still exists and its ``avatar_path`` still equals
        ``expected_path`` (absent when None).

        Args:
            persona_id: ID of the persona
            new_path: Avatar path to set, or None to remove it
            expected_path: Avatar path read before the change (None if absent)
            updated_at: New ``updated_at`` value

        Returns:
            True if written, False if the persona is gone or the path changed

        Raises:
            DatabaseError: If the operation fails for another reason
        """

        def _update() -> bool:
            values: Dict[str, Any] = {
                ":updated_at": self.serializer.serialize(updated_at.isoformat())
            }
            if new_path is None:
                update_expression = "SET updated_at = :updated_at REMOVE avatar_path"
            else:
                update_expression = (
                    "SET avatar_path = :new_path, updated_at = :updated_at"
                )
                values[":new_path"] = self.serializer.serialize(new_path)
            if expected_path is None:
                condition = "attribute_exists(id) AND attribute_not_exists(avatar_path)"
            else:
                condition = "attribute_exists(id) AND avatar_path = :expected_path"
                values[":expected_path"] = self.serializer.serialize(expected_path)
            try:
                self.dynamodb_client.update_item(
                    TableName=self.personas_table,
                    Key={"id": self.serializer.serialize(persona_id)},
                    UpdateExpression=update_expression,
                    ConditionExpression=condition,
                    ExpressionAttributeValues=values,
                )
                return True
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    return False
                raise

        return self._execute_with_retry(
            _update, operation_name=f"update_persona_avatar({persona_id})"
        )

    def delete_persona_returning_old(self, persona_id: str) -> Optional[Persona]:
        """
        Delete a persona and return the item as it was at deletion time.

        The returned item comes from the same atomic write, so its
        ``avatar_path`` is the one actually removed (no read-then-delete gap).

        Args:
            persona_id: ID of persona to delete

        Returns:
            The deleted Persona, or None if it did not exist

        Raises:
            DatabaseError: If delete operation fails
        """

        def _delete() -> Optional[Persona]:
            response = self.dynamodb_client.delete_item(
                TableName=self.personas_table,
                Key={"id": self.serializer.serialize(persona_id)},
                ReturnValues="ALL_OLD",
            )
            attributes = response.get("Attributes")
            return self._deserialize_persona(attributes) if attributes else None

        return self._execute_with_retry(
            _delete, operation_name=f"delete_persona_returning_old({persona_id})"
        )

    def get_persona(
        self, persona_id: str, *, consistent: bool = False
    ) -> Optional[Persona]:
        """
        Retrieve a persona by ID from DynamoDB.

        Args:
            persona_id: ID of persona to retrieve
            consistent: Strongly consistent read. Authorization reads use it,
                so a share removed a moment ago is never seen (NFR1.12).

        Returns:
            Persona object if found, None otherwise

        Raises:
            DatabaseError: If retrieval operation fails
        """

        def _get() -> Optional[Persona]:
            response = self.dynamodb_client.get_item(
                TableName=self.personas_table,
                Key={"id": self.serializer.serialize(persona_id)},
                ConsistentRead=consistent,
            )

            # Check if item was found
            if "Item" not in response:
                return None

            # Deserialize and return persona
            return self._deserialize_persona(response["Item"])

        return self._execute_with_retry(
            _get, operation_name=f"get_persona({persona_id})"
        )

    def get_personas_by_ids(self, persona_ids: List[str]) -> Dict[str, Persona]:
        """
        Read personas with strongly consistent ``BatchGetItem`` calls (NFR2.5).

        100 IDs per call. Keys left in ``UnprocessedKeys`` are read again up to
        three times (50, 100, 200 ms apart, jittered); keys still left raise,
        so a list never silently drops rows (reliability-design.md §4).

        Args:
            persona_ids: IDs to read (duplicates are read once)

        Returns:
            id -> Persona for the IDs that exist

        Raises:
            DatabaseError: If the read fails or keys stay unprocessed
        """
        ids = list(dict.fromkeys(persona_ids))
        found: Dict[str, Persona] = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]

            def _read(chunk: List[str] = chunk) -> Dict[str, Persona]:
                return self._batch_get_personas(chunk)

            found.update(
                self._execute_with_retry(_read, operation_name="get_personas_by_ids")
            )
        return found

    def _batch_get_personas(self, ids: List[str]) -> Dict[str, Persona]:
        keys: List[Dict[str, Any]] = [{"id": {"S": i}} for i in ids]
        found: Dict[str, Persona] = {}
        for attempt, backoff in enumerate((0.0, 0.05, 0.1, 0.2)):
            if attempt:
                time.sleep(backoff * (0.5 + random.random()))  # noqa: S311
            response = self.dynamodb_client.batch_get_item(
                RequestItems={
                    self.personas_table: {"Keys": keys, "ConsistentRead": True}
                }
            )
            for item in response.get("Responses", {}).get(self.personas_table, []):
                persona = self._deserialize_persona(item)
                found[persona.id] = persona
            keys = (
                response.get("UnprocessedKeys", {})
                .get(self.personas_table, {})
                .get("Keys", [])
            )
            if not keys:
                return found
        raise DatabaseError(
            f"BatchGetItem left {len(keys)} keys unprocessed after three re-reads",
            code=ErrorCode.DATABASE_OPERATION_FAILED,
        )

    def scan_all_personas(self) -> List[Persona]:
        """
        Read every persona with a full table scan.

        Only the admin's ``all`` tab search uses this (U4 BR3.5). The
        type-fixed persona GSIs are no longer read (U5 BR8.2).

        Returns:
            Every persona, in scan order.
        """

        def _scan() -> List[Persona]:
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
            return personas

        return self._execute_with_retry(_scan, operation_name="scan_all_personas")

    # Discussions, their messages and reports are read and written by
    # DiscussionStore (U6, NFR Design Q1 A).

    # Upload metadata (UploadedFiles) is read and written by UploadedFileStore
    # with strong reads and transactional attach / detach (U5, BR5).

    # Datasets are DatasetStore (U5, U7).

    # ========== SurveyTemplate CRUD ==========

    def _serialize_survey_template(self, template: "SurveyTemplate") -> Dict[str, Any]:
        """Serialize SurveyTemplate to DynamoDB item format.

        ``shared_group_ids`` is left out: only ``ResourceSharing`` writes it,
        together with ``ResourceShares`` (contract C7).
        """
        data = template.to_dict()
        data.pop("shared_group_ids", None)
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

    def put_new_survey_template(
        self,
        template: "SurveyTemplate",
        extra_items: Sequence[Dict[str, Any]] = (),
    ) -> None:
        """Write a new template with the caller's items in one transaction (U7 BR1.1).

        Conditioned on the ID not existing yet. ``shared_group_ids`` is never
        written here (only ``ResourceSharing`` writes it, contract C7).

        Raises:
            DatabaseConditionFailed: The ID exists (or an extra item's
                condition failed); nothing was written.
            DatabaseError: Anything else.
        """
        self._transact_new(
            self.survey_templates_table,
            self._serialize_survey_template(template),
            extra_items,
            operation=f"put_new_survey_template({template.id})",
        )

    def update_survey_template_content(self, template: "SurveyTemplate") -> bool:
        """Write only the name, questions, images and ``updated_at`` (U7 BR1.5).

        ``owner_sub``, ``shared_group_ids`` and ``created_at`` are never part of
        the write. Conditioned on the template still existing.

        Returns:
            ``False`` when the template does not exist (nothing is created).
        """
        data = template.to_dict()
        fields: Dict[str, Any] = {
            "name": data["name"],
            "questions": data["questions"],
            "updated_at": data["updated_at"],
        }
        remove: tuple[str, ...] = ()
        if data.get("images"):
            fields["images"] = data["images"]
        else:
            remove = ("images",)
        return self._update_columns(
            self.survey_templates_table,
            template.id,
            fields,
            remove=remove,
            operation="update_survey_template_content",
        )

    def get_survey_template(
        self, template_id: str, *, consistent: bool = False
    ) -> Optional["SurveyTemplate"]:
        """Get a SurveyTemplate by ID (strongly consistent for decisions, U7)."""

        def _get() -> Optional["SurveyTemplate"]:
            response = self.dynamodb_client.get_item(
                TableName=self.survey_templates_table,
                Key={"id": self.serializer.serialize(template_id)},
                ConsistentRead=consistent,
            )
            if "Item" not in response:
                return None
            return self._deserialize_survey_template(response["Item"])

        return self._execute_with_retry(
            _get, operation_name=f"get_survey_template({template_id})"
        )

    def get_survey_templates_by_ids(
        self, template_ids: Sequence[str]
    ) -> Dict[str, "SurveyTemplate"]:
        """Read templates with strongly consistent ``BatchGetItem`` calls (NFR2.21).

        Returns:
            id -> SurveyTemplate for the IDs that exist.

        Raises:
            DatabaseError: The read failed or keys stayed unprocessed.
        """
        items = self._batch_get_items(self.survey_templates_table, template_ids)
        return {
            item_id: self._deserialize_survey_template(item)
            for item_id, item in items.items()
        }

    # ========== Survey CRUD ==========

    def _serialize_survey(self, survey: "Survey") -> Dict[str, Any]:
        """Serialize Survey to DynamoDB item format (shares left out, contract C7)."""
        data = survey.to_dict()
        data.pop("shared_group_ids", None)
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

    def put_new_survey(
        self, survey: "Survey", extra_items: Sequence[Dict[str, Any]] = ()
    ) -> None:
        """Write a new survey with the caller's items in one transaction (U7 BR1.1).

        Raises:
            DatabaseConditionFailed: The ID exists; nothing was written.
            DatabaseError: Anything else.
        """
        self._transact_new(
            self.surveys_table,
            self._serialize_survey(survey),
            extra_items,
            operation=f"put_new_survey({survey.id})",
        )

    def update_survey_fields(
        self,
        survey_id: str,
        fields: Mapping[str, Any],
        *,
        remove: Sequence[str] = (),
    ) -> bool:
        """Write only the given columns of a survey (U7 BR1.5, NFR1.87).

        Args:
            survey_id: The survey.
            fields: Column -> new value (status, results, report, ...).
            remove: Columns to remove.

        Returns:
            ``False`` when the survey does not exist (nothing is created).

        Raises:
            ValueError: A column the survey's owner, shares or keys live in.
        """
        protected = set(fields) | set(remove)
        refused = protected & _PROTECTED_COLUMNS
        if refused:
            raise ValueError(f"columns not updatable here: {sorted(refused)}")
        return self._update_columns(
            self.surveys_table,
            survey_id,
            dict(fields),
            remove=tuple(remove),
            operation="update_survey_fields",
        )

    def get_survey(
        self, survey_id: str, *, consistent: bool = False
    ) -> Optional["Survey"]:
        """Get a Survey by ID (strongly consistent for decisions, U7)."""

        def _get() -> Optional["Survey"]:
            response = self.dynamodb_client.get_item(
                TableName=self.surveys_table,
                Key={"id": self.serializer.serialize(survey_id)},
                ConsistentRead=consistent,
            )
            if "Item" not in response:
                return None
            return self._deserialize_survey(response["Item"])

        return self._execute_with_retry(_get, operation_name=f"get_survey({survey_id})")

    def get_surveys_by_ids(self, survey_ids: Sequence[str]) -> Dict[str, "Survey"]:
        """Read surveys with strongly consistent ``BatchGetItem`` calls (NFR2.21)."""
        items = self._batch_get_items(self.surveys_table, survey_ids)
        return {
            item_id: self._deserialize_survey(item) for item_id, item in items.items()
        }

    # Knowledge bases are KnowledgeBaseStore (U7).

    # ==================== Shared helpers (U7) ====================

    @staticmethod
    def item_size(body: Mapping[str, Any]) -> int:
        """Approximate DynamoDB size of ``body`` as an item (U7 NFR2.25)."""
        serializer = TypeSerializer()
        return sum(
            len(str(name).encode("utf-8"))
            + _attribute_size(serializer.serialize(_to_dynamo(value)))
            for name, value in body.items()
            if value is not None
        )

    def _conditional_write(self, write: Callable[[], Any], *, operation: str) -> bool:
        """Run ``write``; ``False`` when its condition failed (other errors raise)."""

        def _run() -> bool:
            try:
                write()
                return True
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")
                if code in (
                    "ConditionalCheckFailedException",
                    "TransactionCanceledException",
                ) and _only_conditions_failed(e):
                    return False
                raise

        return self._execute_with_retry(_run, operation_name=operation)

    def _update_columns(
        self,
        table: str,
        item_id: str,
        fields: Mapping[str, Any],
        *,
        remove: Sequence[str] = (),
        operation: str,
    ) -> bool:
        """``SET`` / ``REMOVE`` columns of an existing item; ``False`` when missing."""
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        sets: List[str] = []
        for index, (column, value) in enumerate(fields.items()):
            names[f"#f{index}"] = column
            values[f":f{index}"] = self.serializer.serialize(_to_dynamo(value))
            sets.append(f"#f{index} = :f{index}")
        removes: List[str] = []
        for index, column in enumerate(remove):
            names[f"#r{index}"] = column
            removes.append(f"#r{index}")
        expression = ""
        if sets:
            expression = "SET " + ", ".join(sets)
        if removes:
            expression += (" " if expression else "") + "REMOVE " + ", ".join(removes)
        if not expression:
            return True
        params: Dict[str, Any] = {
            "TableName": table,
            "Key": {"id": {"S": item_id}},
            "UpdateExpression": expression,
            "ConditionExpression": "attribute_exists(id)",
            "ExpressionAttributeNames": names,
        }
        if values:
            params["ExpressionAttributeValues"] = values
        return self._conditional_write(
            lambda: self.dynamodb_client.update_item(**params),
            operation=f"{operation}({item_id})",
        )

    def _transact_new(
        self,
        table: str,
        item: Dict[str, Any],
        extra_items: Sequence[Dict[str, Any]],
        *,
        operation: str,
    ) -> None:
        put = {
            "Put": {
                "TableName": table,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(id)",
            }
        }
        written = self._conditional_write(
            lambda: self.dynamodb_client.transact_write_items(
                TransactItems=[put, *extra_items]
            ),
            operation=operation,
        )
        if not written:
            raise DatabaseConditionFailed(
                f"{operation}: the item exists already",
                code=ErrorCode.DATABASE_OPERATION_FAILED,
            )

    def _batch_get_items(
        self, table: str, item_ids: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Strongly consistent ``BatchGetItem``s, 100 IDs each, unprocessed re-read."""
        ids = [i for i in dict.fromkeys(item_ids) if i]
        found: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]

            def _read(chunk: List[str] = chunk) -> Dict[str, Dict[str, Any]]:
                return self._batch_get_chunk(table, chunk)

            found.update(
                self._execute_with_retry(_read, operation_name=f"batch_get({table})")
            )
        return found

    def _batch_get_chunk(self, table: str, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        keys: List[Dict[str, Any]] = [{"id": {"S": i}} for i in ids]
        found: Dict[str, Dict[str, Any]] = {}
        for attempt, backoff in enumerate((0.0, 0.05, 0.1, 0.2)):
            if attempt:
                time.sleep(backoff * (0.5 + random.random()))  # noqa: S311
            response = self.dynamodb_client.batch_get_item(
                RequestItems={table: {"Keys": keys, "ConsistentRead": True}}
            )
            for item in response.get("Responses", {}).get(table, []):
                found[item["id"]["S"]] = item
            keys = response.get("UnprocessedKeys", {}).get(table, {}).get("Keys", [])
            if not keys:
                return found
        raise DatabaseError(
            f"BatchGetItem left {len(keys)} keys unprocessed after three re-reads",
            code=ErrorCode.DATABASE_OPERATION_FAILED,
        )

    # Persona bindings (KB / datasets) live in PersonaBindings and are read and
    # written by PersonaBindingStore (U5, ADR-007).

    # Job records are written by JobStore (contract C14).
