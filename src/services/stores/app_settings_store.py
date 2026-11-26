"""DynamoDB reads of application settings (``AppSettings``).

PK ``setting_key``. It holds the advanced generation setting: when it is
missing, ``AdvancedGenerationPolicy`` opens the feature to administrators only.
It is written with the optimistic ``version``: the
version read is the condition of the conditional ``PutItem``.
"""

import logging
from decimal import Decimal
from typing import Any, Mapping

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)

#: The item that holds the advanced generation (beta) setting.
ADVANCED_GENERATION_SETTING_KEY = "advanced_generation"


class AppSettingsStoreError(CodedError):
    """A read of ``AppSettings`` failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class SettingsVersionConflict(AppSettingsStoreError):
    """Someone wrote the setting after it was read (the condition lost)."""

    code = ErrorCode.SETTINGS_VERSION_CONFLICT


def _to_dynamo(value: Any) -> Any:
    """``float`` to ``Decimal`` (the serializer refuses floats)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {str(k): _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


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
        self._serializer = TypeSerializer()

    def put_if_version(
        self, setting_key: str, item: Mapping[str, Any], expected_version: int
    ) -> int:
        """Write the setting only when its version is still ``expected_version``.

        ``0`` means no item yet: the write needs the item to be missing.
        Otherwise ``version`` must equal ``expected_version``. The item is
        written with ``version = expected_version + 1``.

        Args:
            setting_key: The setting (``advanced_generation``).
            item: The values to write (``updated_by``, ``updated_at`` included).
            expected_version: The version read when the section was opened.

        Returns:
            The new version.

        Raises:
            SettingsVersionConflict: The condition lost.
            AppSettingsStoreError: DynamoDB failed.
        """
        new_version = int(expected_version) + 1
        values = {
            **{k: v for k, v in item.items() if v is not None},
            "setting_key": setting_key,
            "version": new_version,
        }
        condition: dict[str, Any]
        if int(expected_version) == 0:
            condition = {"ConditionExpression": "attribute_not_exists(setting_key)"}
        else:
            condition = {
                "ConditionExpression": "#version = :expected",
                "ExpressionAttributeNames": {"#version": "version"},
                "ExpressionAttributeValues": {
                    ":expected": {"N": str(int(expected_version))}
                },
            }
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    k: self._serializer.serialize(_to_dynamo(v))
                    for k, v in values.items()
                },
                **condition,
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                logger.info("app settings write lost its version condition")
                raise SettingsVersionConflict(
                    "the setting changed after it was read"
                ) from e
            logger.warning("put_item on app settings failed: %s", code)
            raise AppSettingsStoreError(f"put_item failed: {code}") from e
        except BotoCoreError as e:
            logger.warning("put_item on app settings unreachable: %s", type(e).__name__)
            raise AppSettingsStoreError(
                f"put_item unreachable: {type(e).__name__}"
            ) from e
        return new_version

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
