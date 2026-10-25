"""PersonaStore（ペルソナの本体の読み書き）の結合テスト。moto を使う。

U4 BR6.1（新規は「まだない ID」の条件付きで、所有者は書くが共有は書かない）、BR6.7
（編集は画面で変えられる列だけ。所有者・共有・アイコン・生成の記録・created_at は残す）、
BR3.5（admin の「すべて」タブだけが Scan する）、NFR2.5（まとめ読みは強い整合性で、
未処理のキーは読み直す）、アイコンの画像パスの比較つき書き込み。
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
        """BR6.1: 共有は ResourceSharing だけが書く。"""
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
        """R-02: 新規の保存は本体を上書きしない（所有者・共有が残る）。"""
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
    # 同じ moto のテーブルを使い回す（各入力の前に同じ ID の項目を消す）
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
        # 編集画面から届いた値（所有者・アイコン・生成の記録を持たない形）
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
        """同時アップロード: 先に書いた側の画像が上書きされない。"""
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
    def test_reads_strongly_and_rereads_unprocessed_keys(self, store, client):
        """NFR2.5: まとめ読みは強い整合性で、未処理のキーは読み直す。"""
        personas = [_persona().owned_by("sub-a") for _ in range(3)]
        for persona in personas:
            store.put_new(persona)
        real = client.batch_get_item
        calls: list[dict[str, Any]] = []

        def flaky(**kwargs: Any) -> Any:
            calls.append(kwargs)
            if len(calls) == 1:
                return {"Responses": {}, "UnprocessedKeys": kwargs["RequestItems"]}
            return real(**kwargs)

        client.batch_get_item = flaky

        found = store.get_many([p.id for p in personas] + ["nope", personas[0].id])

        assert set(found) == {p.id for p in personas}
        assert found[personas[0].id].owner_sub == "sub-a"
        assert len(calls) == 2
        assert calls[0]["RequestItems"][TABLE]["ConsistentRead"] is True

    def test_reads_in_chunks_of_100(self, store, client):
        batch = Mock(wraps=client.batch_get_item)
        client.batch_get_item = batch

        assert store.get_many([f"id-{i}" for i in range(150)]) == {}

        sizes = [len(c.kwargs["RequestItems"][TABLE]["Keys"]) for c in batch.mock_calls]
        assert sizes == [100, 50]

    def test_keys_left_unprocessed_raise(self, store, client):
        client.batch_get_item = lambda **kwargs: {
            "Responses": {},
            "UnprocessedKeys": kwargs["RequestItems"],
        }

        with pytest.raises(PersonaStoreError):
            store.get_many(["a"])

    def test_no_ids_reads_nothing(self, store, client):
        batch = Mock(wraps=client.batch_get_item)
        client.batch_get_item = batch

        assert store.get_many([]) == {}
        assert batch.call_count == 0


class TestScanAll:
    def test_reads_every_scan_page(self, store, client):
        """BR3.5: admin の「すべて」タブの検索だけが使う。"""
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
    """R-02: 新規の保存は本体も共有の索引も上書きしない（BR6.1 所有者、BR1 共有）。"""
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
