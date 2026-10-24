"""
Database health checks for AI Persona System.

The table reads and writes live in one store per table (``PersonaStore``,
``SurveyStore``, ``DiscussionStore`` and the others); this service only checks
that the tables exist.
"""

import logging
from typing import (
    Optional,
    Dict,
    Any,
)
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

# Import models
from ..models.errors import CodedError, ErrorCode


class DatabaseError(CodedError):
    """Custom exception for database-related errors."""

    pass


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

        # Table names: CDK-generated physical names when given, else prefix-based
        names = table_names or {}

        def name_of(kind: str) -> str:
            return names.get(kind) or f"{table_prefix}_{kind}"

        self.personas_table = name_of("Personas")
        self.discussions_table = name_of("Discussions")
        self.files_table = name_of("UploadedFiles")

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

    # Deserialization methods for DynamoDB to Python type conversion

    # Persona CRUD operations
