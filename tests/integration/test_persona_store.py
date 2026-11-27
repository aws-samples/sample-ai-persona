"""Integration tests for PersonaStore (reads and writes of the persona body), using moto.

A new persona is written under a "no such ID yet" condition: the owner is
written but shares are not. An edit changes only the columns editable on screen
and keeps owner, shares, icon, generation record and created_at. Only the admin
"all" tab scans. Batch reads are strongly consistent and re-read unprocessed
keys. Icon writes compare the image path.
"""

from datetime import datetime
from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from src.models.errors import ErrorCode  # noqa: E402
from src.models.persona import Persona  # noqa: E402
from src.services.stores.persona_store import PersonaStore, PersonaStoreError  # noqa: E402

REGION = "ap-northeast-1"
TABLE = "PersonasTest"
OLD_PATH = "s3://bucket/persona_avatars/p/old.webp"
NEW_PATH = "s3://bucket/persona_avatars/p/new.webp"
LOG = [{"role": "user", "content": "生成の依頼"}]


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
def store(client) -> PersonaStore:
    return PersonaStore(region=REGION, table_name=TABLE, client=client)


def _persona(**kwargs: Any) -> Persona:
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


def _raw(client: Any, persona_id: str) -> dict[str, Any] | None:
    return client.get_item(TableName=TABLE, Key={"id": {"S": persona_id}}).get("Item")


def _share(client: Any, persona_id: str, *groups: str) -> None:
    client.update_item(
        TableName=TABLE,
        Key={"id": {"S": persona_id}},
        UpdateExpression="SET shared_group_ids = :s",
        ExpressionAttributeValues={":s": {"SS": list(groups)}},
    )


class TestPutAndGet:
    def test_every_field_reads_back(self, store):
        persona = (
            _persona(gender="female", country="JP", city="大阪", tags=["t1", "t2"])
            .owned_by("sub-a")
            .with_avatar(OLD_PATH)
        )
        persona = Persona.from_dict(
            {
                **persona.to_dict(),
                "generation_log": LOG,
                "generation_context": {"source": "manual"},
            }
        )
        store.put_new(persona)

        read = store.get(persona.id, consistent=True)
        assert read == persona
        assert isinstance(read.age, int)

    def test_advanced_generation_context_with_floats_reads_back(self, store):
        """generation_context of advanced generation holds fractions such as composition ratios."""
        context = {
            "mode": "advanced",
            "segments": [{"id": "s1", "share": 0.375, "size": 3, "overlaps": False}],
            "analysis_run": {"seed": 42, "params": {"sample_ratio": 0.5, "k": 2}},
        }
        persona = Persona.from_dict(
            {**_persona().to_dict(), "generation_context": context}
        )

        store.put_new(persona)

        read = store.get(persona.id, consistent=True)
        assert read.generation_context == context
        share = read.generation_context["segments"][0]["share"]
        assert isinstance(share, float)
        assert isinstance(read.generation_context["analysis_run"]["seed"], int)

    def test_float_is_stored_as_a_dynamodb_number(self, store, client):
        persona = Persona.from_dict(
            {**_persona().to_dict(), "generation_context": {"share": 0.25}}
        )

        store.put_new(persona)

        item = _raw(client, persona.id)
        assert item["generation_context"] == {"M": {"share": {"N": "0.25"}}}

    def test_optional_fields_left_out_read_back_empty(self, store, client):
        persona = _persona()
        store.put_new(persona)

        read = store.get(persona.id)
        assert (read.gender, read.country, read.city, read.avatar_path) == (
            None,
            None,
            None,
            None,
        )
        assert read.tags == []
        assert read.owner_sub is None
        assert read.shared_group_ids == frozenset()
        item = _raw(client, persona.id)
        assert "gender" not in item and "tags" not in item
        assert item["type"] == {"S": "persona"}

    def test_owner_is_written_but_never_the_shares(self, store, client):
        """Shares are written only by ResourceSharing."""
        persona = Persona.from_dict(
            {**_persona().owned_by("sub-a").to_dict(), "shared_group_ids": ["sales"]}
        )
        store.put_new(persona)

        item = _raw(client, persona.id)
        assert item["owner_sub"] == {"S": "sub-a"}
        assert "shared_group_ids" not in item

    def test_shares_read_back(self, store, client):
        persona = _persona().owned_by("sub-a")
        store.put_new(persona)
        _share(client, persona.id, "sales", "plan")

        stored = store.get(persona.id, consistent=True)
        assert stored.shared_group_ids == frozenset({"sales", "plan"})

    def test_existing_id_is_refused_and_the_body_stays(self, store, client):
        """A new save does not overwrite the body (owner and shares remain)."""
        persona = _persona().owned_by("sub-a")
        store.put_new(persona)
        _share(client, persona.id, "sales")
        before = _raw(client, persona.id)

        with pytest.raises(PersonaStoreError) as exc_info:
            store.put_new(persona.update(name="上書き"))

        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED
        assert _raw(client, persona.id) == before

    def test_missing_persona_is_none(self, store):
        assert store.get("missing") is None

    @given(
        name=st.text(min_size=1, max_size=50, alphabet=st.characters(codec="utf-8")),
        age=st.integers(min_value=18, max_value=100),
        values=st.lists(
            st.text(min_size=1, max_size=20, alphabet=st.characters(codec="utf-8")),
            min_size=1,
            max_size=5,
        ),
        created_at=st.datetimes(
            min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31)
        ),
    )
    # Reuse the same moto table (delete the item with the same ID before each input)
    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_round_trip_property(self, client, name, age, values, created_at):
        store = PersonaStore(region=REGION, table_name=TABLE, client=client)
        persona = Persona(
            id=f"p-{abs(hash((name, age, created_at)))}",
            name=name,
            age=age,
            occupation="o",
            background="b",
            values=values,
            pain_points=["p"],
            goals=["g"],
            created_at=created_at,
            updated_at=created_at,
        )
        client.delete_item(TableName=TABLE, Key={"id": {"S": persona.id}})
        store.put_new(persona)

        assert store.get(persona.id, consistent=True) == persona


class TestUpdateEditable:
    def test_is_partial_and_keeps_owner_shares_avatar_and_log(self, store, client):
        persona = Persona.from_dict(
            {
                **_persona().owned_by("sub-a").with_avatar(OLD_PATH).to_dict(),
                "generation_log": LOG,
            }
        )
        store.put_new(persona)
        _share(client, persona.id, "sales")
        # Values arriving from the edit screen (no owner, icon or generation record)
        edited = Persona.from_dict(
            {
                **{
                    k: v
                    for k, v in persona.to_dict().items()
                    if k not in ("owner_sub", "avatar_path", "generation_log")
                },
                "name": "編集後",
                "created_at": "2030-01-01T00:00:00",
            }
        )
        put = Mock(wraps=client.put_item)
        client.put_item = put

        assert store.update_editable(edited) is True

        assert put.call_count == 0
        stored = store.get(persona.id, consistent=True)
        assert stored.name == "編集後"
        assert stored.owner_sub == "sub-a"
        assert stored.shared_group_ids == frozenset({"sales"})
        assert stored.avatar_path == OLD_PATH
        assert stored.generation_log == LOG
        assert stored.created_at == persona.created_at

    def test_clears_emptied_optional_fields(self, store, client):
        persona = _persona(gender="female", city="大阪", tags=["t"])
        store.put_new(persona)

        store.update_editable(persona.update(gender="", city="", tags=[]))

        item = _raw(client, persona.id)
        assert "gender" not in item and "city" not in item and "tags" not in item

    def test_missing_persona_is_false_and_creates_nothing(self, store, client):
        persona = _persona()

        assert store.update_editable(persona) is False
        assert _raw(client, persona.id) is None


class TestUpdateAvatar:
    def _saved(self, store: PersonaStore, avatar_path: str | None = None) -> Persona:
        persona = _persona().with_avatar(avatar_path)
        store.put_new(persona)
        return persona

    def test_sets_path_when_unchanged(self, store):
        persona = self._saved(store)

        assert store.update_avatar(
            persona.id,
            new_path=NEW_PATH,
            expected_path=None,
            updated_at=datetime(2030, 1, 1),
        )

        stored = store.get(persona.id)
        assert stored.avatar_path == NEW_PATH
        assert stored.updated_at == datetime(2030, 1, 1)

    def test_replaces_the_expected_path(self, store):
        persona = self._saved(store, OLD_PATH)

        assert store.update_avatar(
            persona.id,
            new_path=NEW_PATH,
            expected_path=OLD_PATH,
            updated_at=datetime.now(),
        )
        assert store.get(persona.id).avatar_path == NEW_PATH

    def test_removes_the_path(self, store):
        persona = self._saved(store, OLD_PATH)

        assert store.update_avatar(
            persona.id, new_path=None, expected_path=OLD_PATH, updated_at=datetime.now()
        )
        assert store.get(persona.id).avatar_path is None

    def test_does_not_recreate_a_deleted_persona(self, store, client):
        persona = self._saved(store)
        client.delete_item(TableName=TABLE, Key={"id": {"S": persona.id}})

        assert not store.update_avatar(
            persona.id, new_path=NEW_PATH, expected_path=None, updated_at=datetime.now()
        )
        assert store.get(persona.id) is None

    def test_refuses_when_the_path_changed_concurrently(self, store):
        """Concurrent upload: the image written first is not overwritten."""
        persona = self._saved(store)
        assert store.update_avatar(
            persona.id, new_path=OLD_PATH, expected_path=None, updated_at=datetime.now()
        )

        assert not store.update_avatar(
            persona.id, new_path=NEW_PATH, expected_path=None, updated_at=datetime.now()
        )
        assert store.get(persona.id).avatar_path == OLD_PATH

    def test_keeps_fields_edited_concurrently(self, store):
        persona = self._saved(store)
        store.update_editable(persona.update(name="編集後の名前"))

        assert store.update_avatar(
            persona.id, new_path=NEW_PATH, expected_path=None, updated_at=datetime.now()
        )

        stored = store.get(persona.id)
        assert (stored.name, stored.avatar_path) == ("編集後の名前", NEW_PATH)


class TestGetMany:
    def test_no_ids_reads_nothing(self, store, client):
        batch = Mock(wraps=client.batch_get_item)
        client.batch_get_item = batch

        assert store.get_many([]) == {}
        assert batch.call_count == 0


class TestScanAll:
    def test_reads_every_scan_page(self, store, client):
        """Used only by the search behind the admin "all" tab."""
        made = [_persona() for _ in range(5)]
        for persona in made:
            store.put_new(persona)
        original = client.scan
        calls: list[dict[str, Any]] = []

        def paged(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return original(**{**kwargs, "Limit": 2})

        client.scan = paged

        assert {p.id for p in store.scan_all()} == {p.id for p in made}
        assert len(calls) == 3


class TestFailures:
    def test_unreachable_table_raises_the_store_error(self, client):
        store = PersonaStore(region=REGION, table_name="NoSuchTable", client=client)

        with pytest.raises(PersonaStoreError) as exc_info:
            store.get("x")
        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED


def test_a_save_of_an_existing_id_keeps_the_body_and_the_index(store, client):
    """A new save overwrites neither the body nor the share index."""
    from src.managers.persona_manager import PersonaManager, PersonaManagerError
    from src.models.authz import Principal, ResourceType, Role
    from src.services.stores.resource_share_store import ResourceShareStore
    from tests.error_helpers import raises_code

    client.create_table(
        TableName="SharesTest",
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
        region=REGION, shares_table="SharesTest", client=client, sleep=lambda _s: None
    )
    original = _persona().owned_by("sub-a")
    store.put_new(original)
    shares.write_shares(
        TABLE,
        ResourceType.PERSONA,
        original.id,
        expected=frozenset(),
        new=frozenset({"sales"}),
        created_at=_raw(client, original.id)["created_at"]["S"],
        owner_sub="sub-a",
    )
    body_before = _raw(client, original.id)
    index_before = client.scan(TableName="SharesTest")["Items"]

    manager = PersonaManager(
        persona_store=store, s3_service=Mock(), resource_sharing=Mock(), owners=Mock()
    )
    saver = Principal("sub-b", frozenset({Role.MEMBER}), frozenset({"sales"}))
    with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
        manager.save_persona(saver, original.update(name="上書き"))

    assert _raw(client, original.id) == body_before
    assert body_before["owner_sub"] == {"S": "sub-a"}
    assert body_before["shared_group_ids"] == {"SS": ["sales"]}
    assert client.scan(TableName="SharesTest")["Items"] == index_before
    assert len(index_before) == 1
