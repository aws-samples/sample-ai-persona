"""DynamoDB reads of application settings (``AppSettings``, contract C6).

PK ``setting_key``. U1 only reads the advanced generation setting: when it is
missing, ``AdvancedGenerationPolicy`` opens the feature to administrators only
(BR1.2). Writes (with the optimistic ``version``) arrive with U6.
"""

import logging
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)

#: The item that holds the advanced generation (beta) setting.
ADVANCED_GENERATION_SETTING_KEY = "advanced_generation"


class AppSettingsStoreError(CodedError):
    """A read of ``AppSettings`` failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


def _from_dynamo(value: Any) -> Any:
    """``Decimal`` back to ``int`` / ``float``."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


class AppSettingsStore:
    """Reads of ``AppSettings``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``AppSettings``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._deserializer = TypeDeserializer()

    def get(self, setting_key: str) -> dict[str, Any] | None:
        """The setting item as plain values, or ``None`` when it is missing.

        Raises:
            AppSettingsStoreError: DynamoDB failed.
        """
        try:
            response = self.client.get_item(
                TableName=self.table_name,
                Key={"setting_key": {"S": setting_key}},
                ConsistentRead=True,
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.warning("get_item on app settings failed: %s", code)
            raise AppSettingsStoreError(f"get_item failed: {code}") from e
        except BotoCoreError as e:
            logger.warning("get_item on app settings unreachable: %s", type(e).__name__)
            raise AppSettingsStoreError(
                f"get_item unreachable: {type(e).__name__}"
            ) from e
        item = response.get("Item")
        if not item:
            return None
        return {
            k: _from_dynamo(self._deserializer.deserialize(v)) for k, v in item.items()
        }
