"""AppSettingsStore (``AppSettings``) with moto. Read-only here."""

from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.services.stores.app_settings_store import (  # noqa: E402
    ADVANCED_GENERATION_SETTING_KEY,
    AppSettingsStore,
    AppSettingsStoreError,
    SettingsVersionConflict,
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


def _item(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "audience_mode": "departments",
        "department_groups": ["group:営業"],
        "allow_detailed_analysis": True,
        "updated_by": "admin-1",
        "updated_at": "2026-11-22T10:00:00+00:00",
    }
    values.update(overrides)
    return values


class TestPutIfVersion:
    """The version read is the condition of the write."""

    def test_version_0_writes_only_when_there_is_no_item(self, store):
        assert store.put_if_version(ADVANCED_GENERATION_SETTING_KEY, _item(), 0) == 1

        value = store.get(ADVANCED_GENERATION_SETTING_KEY)
        assert value is not None
        assert value["version"] == 1
        assert value["updated_by"] == "admin-1"
        assert value["updated_at"] == "2026-11-22T10:00:00+00:00"
        assert value["department_groups"] == ["group:営業"]
        assert value["setting_key"] == ADVANCED_GENERATION_SETTING_KEY

    def test_version_0_loses_once_an_item_exists(self, store):
        store.put_if_version(ADVANCED_GENERATION_SETTING_KEY, _item(), 0)

        with pytest.raises(SettingsVersionConflict):
            store.put_if_version(ADVANCED_GENERATION_SETTING_KEY, _item(), 0)

    def test_the_matching_version_writes_the_next_one(self, store):
        store.put_if_version(ADVANCED_GENERATION_SETTING_KEY, _item(), 0)

        new_version = store.put_if_version(
            ADVANCED_GENERATION_SETTING_KEY,
            _item(audience_mode="everyone", department_groups=[]),
            1,
        )

        value = store.get(ADVANCED_GENERATION_SETTING_KEY)
        assert new_version == 2
        assert value is not None and value["version"] == 2
        assert value["audience_mode"] == "everyone"

    def test_two_writers_of_the_same_version_one_wins(self, store):
        store.put_if_version(ADVANCED_GENERATION_SETTING_KEY, _item(), 0)

        store.put_if_version(
            ADVANCED_GENERATION_SETTING_KEY, _item(updated_by="admin-1"), 1
        )
        with pytest.raises(SettingsVersionConflict):
            store.put_if_version(
                ADVANCED_GENERATION_SETTING_KEY, _item(updated_by="admin-2"), 1
            )

        value = store.get(ADVANCED_GENERATION_SETTING_KEY)
        assert value is not None
        assert value["updated_by"] == "admin-1" and value["version"] == 2

    def test_a_conflict_is_a_coded_store_error(self, store):
        store.put_if_version(ADVANCED_GENERATION_SETTING_KEY, _item(), 0)

        with pytest.raises(SettingsVersionConflict) as caught:
            store.put_if_version(ADVANCED_GENERATION_SETTING_KEY, _item(), 5)
        assert caught.value.code.value == "settings_version_conflict"

    def test_a_failed_write_raises_the_store_error(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        with moto.mock_aws():
            c = boto3.client("dynamodb", region_name=REGION)
            missing = AppSettingsStore(region=REGION, table_name="Missing", client=c)

            with pytest.raises(AppSettingsStoreError):
                missing.put_if_version(ADVANCED_GENERATION_SETTING_KEY, _item(), 0)


def test_a_failed_read_raises_the_store_error(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        store = AppSettingsStore(region=REGION, table_name="Missing", client=c)

        with pytest.raises(AppSettingsStoreError):
            store.get(ADVANCED_GENERATION_SETTING_KEY)
