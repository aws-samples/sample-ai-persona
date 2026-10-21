"""CustomPersonaDataStore（カスタムペルソナデータの登録情報）の結合テスト。moto を使う。

BR4.3〜BR4.6（ID・名前の押さえ・登録情報・削除）、BR5.2（利用範囲の ADD / DELETE）、
NFR1.74（名前の押さえの項目を出さない）、NFR1.85、NFR1.87（本体があることを条件に）。
"""

from datetime import datetime
from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.models.custom_persona_data import (  # noqa: E402
    CustomPersonaData,
    ExtraColumn,
    reservation_id,
)
from src.services.custom_persona_data_store import (  # noqa: E402
    CustomPersonaDataConditionFailed,
    CustomPersonaDataNameTaken,
    CustomPersonaDataStore,
    CustomPersonaDataStoreError,
)

pytestmark = pytest.mark.integration

REGION = "ap-northeast-1"
TABLE = "CustomPersonaDataTest"
NOW = datetime(2026, 10, 1, 9, 0, 0)


@pytest.fixture
def client(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        c.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield c


@pytest.fixture
def store(client) -> CustomPersonaDataStore:
    return CustomPersonaDataStore(region=REGION, table_name=TABLE, client=client)


def _data(name: str = "顧客データ", **kwargs: Any) -> CustomPersonaData:
    return CustomPersonaData.create_new(
        name=name,
        columns=["sex", "age", "hobby"],
        row_count=120,
        registered_by="admin-sub",
        standard_mapping={"sex": "性別"},
        extra_columns=[ExtraColumn(csv_column="hobby", description="趣味")],
        now=NOW,
        **kwargs,
    )


def _raw(client: Any, item_id: str) -> dict | None:
    return client.get_item(TableName=TABLE, Key={"id": {"S": item_id}}).get("Item")


class TestPutNew:
    def test_writes_the_record_and_the_name_reservation(self, store, client):
        data = _data()
        store.put_new(data)

        assert store.get(data.id) == data
        reservation = _raw(client, reservation_id(data.name))
        assert reservation is not None
        assert reservation["data_id"]["S"] == data.id

    def test_same_name_twice_is_name_taken_and_writes_nothing(self, store, client):
        store.put_new(_data())
        second = _data()
        with pytest.raises(CustomPersonaDataNameTaken):
            store.put_new(second)
        assert _raw(client, second.id) is None

    def test_scope_chosen_at_registration_is_a_string_set(self, store, client):
        data = _data(shared_group_ids=frozenset({"sales"}))
        store.put_new(data)
        assert _raw(client, data.id)["shared_group_ids"] == {"SS": ["sales"]}

    def test_names_are_case_sensitive(self, store):
        store.put_new(_data("Data"))
        store.put_new(_data("data"))
        assert len(store.list_all()) == 2


class TestReads:
    def test_get_missing_is_none(self, store):
        assert store.get("2b0f8d2e-0000-4000-8000-000000000000") is None

    def test_get_of_a_reservation_id_is_none(self, store):
        data = _data()
        store.put_new(data)
        assert store.get(reservation_id(data.name)) is None

    def test_get_many_skips_reservations_and_missing(self, store):
        first, second = _data("a"), _data("b")
        store.put_new(first)
        store.put_new(second)
        found = store.get_many([first.id, second.id, reservation_id("a"), "missing-id"])
        assert set(found) == {first.id, second.id}

    def test_get_many_reads_unprocessed_keys_again(self, store, client):
        data = _data()
        store.put_new(data)
        real = client.batch_get_item
        calls = {"n": 0}

        def flaky(**kwargs: Any) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "Responses": {TABLE: []},
                    "UnprocessedKeys": {TABLE: kwargs["RequestItems"][TABLE]},
                }
            return real(**kwargs)

        client.batch_get_item = flaky
        assert set(store.get_many([data.id])) == {data.id}
        assert calls["n"] == 2

    def test_list_all_reads_every_page_and_hides_reservations(self, store, client):
        made = [_data(f"n{i}") for i in range(30)]
        for data in made:
            store.put_new(data)
        real_scan = client.scan
        limits: list[Any] = []

        def paged_scan(**kwargs: Any) -> dict:
            limits.append(kwargs.get("ExclusiveStartKey"))
            return real_scan(**{**kwargs, "Limit": 7})

        client.scan = paged_scan
        listed = store.list_all()
        assert {d.id for d in listed} == {d.id for d in made}
        assert len(limits) > 1

    def test_250_records_are_all_listed_with_their_scopes(self, store, client):
        """NFR9.37: hundreds of assets are read page by page, nothing is lost."""
        from tests.unit.survey_world import bulk_put_custom_data

        scopes = [{"sales"}, {"plan"}, {"sales", "plan"}, set()]
        made = [
            _data(f"規模{i:03d}", shared_group_ids=frozenset(scopes[i % 4]))
            for i in range(250)
        ]
        bulk_put_custom_data(client, TABLE, made)
        # 名前の押さえ（予約中の項目）も同じテーブルにある
        assert all(_raw(client, reservation_id(d.name)) for d in made[:3])
        real_scan = client.scan
        pages: list[int] = []

        def paged_scan(**kwargs: Any) -> dict:
            response = real_scan(**{**kwargs, "Limit": 7})
            pages.append(len(response.get("Items", [])))
            return response

        client.scan = paged_scan
        listed = store.list_all()

        assert len(listed) == 250
        assert {d.id for d in listed} == {d.id for d in made}
        assert {d.id: d.shared_group_ids for d in listed} == {
            d.id: d.shared_group_ids for d in made
        }
        assert len(pages) > 500 // 7
        sales = {d.id for d in listed if "sales" in d.shared_group_ids}
        assert sales == {d.id for i, d in enumerate(made) if i % 4 in (0, 2)}
        assert len(sales) == 125


class TestRename:
    def test_moves_the_reservation_and_the_name_together(self, store, client):
        data = _data("旧")
        store.put_new(data)
        store.rename(data.id, old_name="旧", new_name="新")

        assert store.get(data.id).name == "新"
        assert _raw(client, reservation_id("旧")) is None
        assert _raw(client, reservation_id("新"))["data_id"]["S"] == data.id

    def test_taken_name_changes_nothing(self, store, client):
        first, second = _data("一"), _data("二")
        store.put_new(first)
        store.put_new(second)
        with pytest.raises(CustomPersonaDataNameTaken):
            store.rename(second.id, old_name="二", new_name="一")
        assert store.get(second.id).name == "二"
        assert _raw(client, reservation_id("二")) is not None

    def test_rename_of_a_deleted_record_is_condition_failed(self, store):
        data = _data()
        store.put_new(data)
        store.delete(data.id, data.name)
        with pytest.raises(CustomPersonaDataConditionFailed):
            store.rename(data.id, old_name=data.name, new_name="新")


class TestPartialUpdates:
    def test_update_scope_adds_then_removes_and_keeps_other_columns(
        self, store, client
    ):
        data = _data(shared_group_ids=frozenset({"plan"}))
        store.put_new(data)
        store.update_scope(
            data.id, add=frozenset({"sales"}), remove=frozenset({"plan"})
        )
        updated = store.get(data.id)
        assert updated.shared_group_ids == frozenset({"sales"})
        assert updated.name == data.name
        assert updated.row_count == data.row_count

    def test_removing_everything_drops_the_column(self, store, client):
        data = _data(shared_group_ids=frozenset({"plan"}))
        store.put_new(data)
        store.update_scope(data.id, add=frozenset(), remove=frozenset({"plan"}))
        assert "shared_group_ids" not in _raw(client, data.id)

    def test_empty_sides_send_no_update(self, store, client):
        data = _data()
        store.put_new(data)
        spy = Mock(wraps=client.update_item)
        client.update_item = spy
        store.update_scope(data.id, add=frozenset({"sales"}), remove=frozenset())
        assert spy.call_count == 1
        store.update_scope(data.id, add=frozenset(), remove=frozenset())
        assert spy.call_count == 1

    def test_update_scope_of_a_missing_record_creates_nothing(self, store, client):
        with pytest.raises(CustomPersonaDataConditionFailed):
            store.update_scope(
                "2b0f8d2e-0000-4000-8000-000000000000",
                add=frozenset({"sales"}),
                remove=frozenset(),
            )
        assert client.scan(TableName=TABLE)["Count"] == 0

    def test_update_fields_changes_only_the_given_columns(self, store):
        data = _data(shared_group_ids=frozenset({"sales"}))
        store.put_new(data)
        store.update_fields(data.id, {"row_count": 200, "columns": ["a"]})
        updated = store.get(data.id)
        assert updated.row_count == 200
        assert updated.columns == ("a",)
        assert updated.shared_group_ids == frozenset({"sales"})
        assert updated.name == data.name

    def test_update_fields_refuses_scope_and_name(self, store):
        with pytest.raises(ValueError):
            store.update_fields("x", {"shared_group_ids": ["a"]})
        with pytest.raises(ValueError):
            store.update_fields("x", {"name": "y"})

    def test_update_fields_of_a_missing_record_creates_nothing(self, store, client):
        with pytest.raises(CustomPersonaDataConditionFailed):
            store.update_fields("missing", {"row_count": 1})
        assert client.scan(TableName=TABLE)["Count"] == 0
        store.update_fields("missing", {})

    def test_scope_and_rename_both_survive(self, store):
        data = _data("旧")
        store.put_new(data)
        store.update_scope(data.id, add=frozenset({"sales"}), remove=frozenset())
        store.rename(data.id, old_name="旧", new_name="新")
        updated = store.get(data.id)
        assert updated.name == "新"
        assert updated.shared_group_ids == frozenset({"sales"})


class TestDelete:
    def test_deletes_the_record_and_the_reservation(self, store, client):
        data = _data()
        store.put_new(data)
        assert store.delete(data.id, data.name) is True
        assert store.get(data.id) is None
        assert _raw(client, reservation_id(data.name)) is None
        # 同じ名前で登録し直せる
        store.put_new(_data())

    def test_missing_record_is_false(self, store):
        assert store.delete("2b0f8d2e-0000-4000-8000-000000000000", "x") is False


class TestFailures:
    def _broken(self, store: CustomPersonaDataStore, operation: str) -> None:
        def fail(**_: Any) -> dict:
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "boom"}},
                operation,
            )

        setattr(store.client, operation, fail)

    @pytest.mark.parametrize(
        ("operation", "call"),
        [
            ("get_item", lambda s: s.get("x")),
            ("scan", lambda s: s.list_all()),
            ("batch_get_item", lambda s: s.get_many(["a", "b"])),
            ("transact_write_items", lambda s: s.put_new(_data())),
            (
                "update_item",
                lambda s: s.update_scope("x", add=frozenset({"a"}), remove=frozenset()),
            ),
        ],
    )
    def test_other_failures_are_store_errors_not_conditions(
        self, store, operation, call
    ):
        self._broken(store, operation)
        with pytest.raises(CustomPersonaDataStoreError) as info:
            call(store)
        assert not isinstance(
            info.value, (CustomPersonaDataConditionFailed, CustomPersonaDataNameTaken)
        )
