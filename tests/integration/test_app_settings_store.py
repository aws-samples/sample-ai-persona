"""AppSettingsStore (``AppSettings``, contract C6) with moto. U1 only reads (BR1.2)."""

from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.services.stores.app_settings_store import (  # noqa: E402
    ADVANCED_GENERATION_SETTING_KEY,
    AppSettingsStore,
    AppSettingsStoreError,
)

REGION = "ap-northeast-1"
TABLE = "AppSettingsTest"


@pytest.fixture
def client(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        c.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "setting_key", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield c


@pytest.fixture
def store(client) -> AppSettingsStore:
    return AppSettingsStore(region=REGION, table_name=TABLE, client=client)


def test_missing_setting_is_none(store):
    assert store.get(ADVANCED_GENERATION_SETTING_KEY) is None


def test_reads_the_stored_values(store, client):
    client.put_item(
        TableName=TABLE,
        Item={
            "setting_key": {"S": ADVANCED_GENERATION_SETTING_KEY},
            "schema_version": {"N": "1"},
            "version": {"N": "3"},
            "audience_mode": {"S": "everyone"},
            "department_groups": {"L": [{"S": "group:営業"}]},
            "allow_detailed_analysis": {"BOOL": False},
        },
    )

    value = store.get(ADVANCED_GENERATION_SETTING_KEY)

    assert value == {
        "setting_key": "advanced_generation",
        "schema_version": 1,
        "version": 3,
        "audience_mode": "everyone",
        "department_groups": ["group:営業"],
        "allow_detailed_analysis": False,
    }


def test_the_setting_key_is_advanced_generation():
    assert ADVANCED_GENERATION_SETTING_KEY == "advanced_generation"


def test_a_failed_read_raises_the_store_error(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        store = AppSettingsStore(region=REGION, table_name="Missing", client=c)

        with pytest.raises(AppSettingsStoreError):
            store.get(ADVANCED_GENERATION_SETTING_KEY)
