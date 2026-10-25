"""SurveyTemplateStore（アンケートテンプレートの本体の読み書き）の結合テスト。moto を使う。

U7 BR1.1（新規は「まだない ID」の条件付きで、共有は書かない。OwnerIndex に載る）、
BR1.5（編集は名前・設問・画像・updated_at だけ）、NFR2.21（まとめ読みは強い整合性で、
未処理のキーは読み直す）、NFR2.25（本体の大きさの見積もり）。
"""

from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.errors import ErrorCode  # noqa: E402
from src.models.survey_template import (  # noqa: E402
    Question,
    SurveyTemplate,
    TemplateImage,
)
from src.services.stores.survey_template_store import (  # noqa: E402
    SurveyTemplateStore,
    SurveyTemplateStoreError,
)

REGION = "ap-northeast-1"
TABLE = "SurveyTemplatesTest"


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
            AttributeDefinitions=[
                {"AttributeName": "id", "AttributeType": "S"},
                {"AttributeName": "owner_sub", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "OwnerIndex",
                    "KeySchema": [
                        {"AttributeName": "owner_sub", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                }
            ],
        )
        yield c


@pytest.fixture
def store(client) -> SurveyTemplateStore:
    return SurveyTemplateStore(region=REGION, table_name=TABLE, client=client)


def _template(owner: str = "user-a", **kwargs: Any) -> SurveyTemplate:
    return SurveyTemplate.create_new(
        name="顧客満足度調査",
        questions=[
            Question.create_scale_rating("満足度は？"),
            Question.create_multiple_choice("好きな色は？", ["赤", "青", "緑"]),
            Question.create_free_text("ご意見をどうぞ"),
        ],
        **kwargs,
    ).owned_by(owner)


def _raw(client: Any, template_id: str) -> dict[str, Any] | None:
    return client.get_item(TableName=TABLE, Key={"id": {"S": template_id}}).get("Item")


def _share(client: Any, template_id: str) -> None:
    client.update_item(
        TableName=TABLE,
        Key={"id": {"S": template_id}},
        UpdateExpression="SET shared_group_ids = :s",
        ExpressionAttributeValues={":s": {"SS": ["sales"]}},
    )


class TestPutAndGet:
    def test_registered_template_reads_back(self, store):
        template = _template()
        store.put_new(template)

        read = store.get(template.id, consistent=True)
        assert read is not None
        assert (read.name, read.owner_sub) == ("顧客満足度調査", "user-a")
        assert [q.question_type for q in read.questions] == [
            "scale_rating",
            "multiple_choice",
            "free_text",
        ]
        assert read.questions[1].options == ["赤", "青", "緑"]
        scale = read.questions[0]
        assert (scale.scale_min, scale.scale_max) == (
            template.questions[0].scale_min,
            template.questions[0].scale_max,
        )
        assert isinstance(scale.scale_min, int)
        assert read.created_at == template.created_at

    def test_new_template_has_no_shares_and_lands_in_owner_index(self, store, client):
        """BR1.1: 共有は ResourceSharing だけが書く（作成は共有なしで始まる）。"""
        template = SurveyTemplate.from_dict(
            {**_template().to_dict(), "shared_group_ids": ["sales"]}
        )
        store.put_new(template)

        assert "shared_group_ids" not in _raw(client, template.id)
        owned = client.query(
            TableName=TABLE,
            IndexName="OwnerIndex",
            KeyConditionExpression="owner_sub = :o",
            ExpressionAttributeValues={":o": {"S": "user-a"}},
        )["Items"]
        assert [i["id"]["S"] for i in owned] == [template.id]

    def test_existing_id_is_refused_and_the_owner_stays(self, store, client):
        template = _template()
        store.put_new(template)

        with pytest.raises(SurveyTemplateStoreError) as exc_info:
            store.put_new(template.owned_by("other"))

        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED
        assert _raw(client, template.id)["owner_sub"] == {"S": "user-a"}

    def test_missing_template_is_none(self, store):
        assert store.get("missing") is None


class TestUpdateContent:
    def test_keeps_owner_shares_and_created_at(self, store, client):
        """BR1.5: 編集は所有者・共有・created_at を書かない（put もしない）。"""
        template = _template()
        store.put_new(template)
        _share(client, template.id)
        edited = SurveyTemplate.from_dict(
            {
                **{
                    k: v
                    for k, v in template.to_dict().items()
                    if k not in ("owner_sub", "created_at")
                },
                "created_at": "2030-01-01T00:00:00",
            }
        ).edited(name="編集後", questions=[Question.create_free_text("Q2")], images=[])
        put = Mock(wraps=client.put_item)
        client.put_item = put

        assert store.update_content(edited) is True

        assert put.call_count == 0
        stored = store.get(template.id, consistent=True)
        assert stored.name == "編集後"
        assert [q.text for q in stored.questions] == ["Q2"]
        assert stored.owner_sub == "user-a"
        assert stored.shared_group_ids == frozenset({"sales"})
        assert stored.created_at == template.created_at

    def test_writes_and_removes_images(self, store, client):
        image = TemplateImage.create_new("img", "s3://b/k.png", "image/png", "k.png")
        template = _template()
        store.put_new(template)

        store.update_content(
            template.edited(name="t", questions=template.questions, images=[image])
        )
        assert store.get(template.id, consistent=True).images[0].file_path == (
            "s3://b/k.png"
        )

        store.update_content(
            template.edited(name="t", questions=template.questions, images=[])
        )
        assert "images" not in _raw(client, template.id)

    def test_deleted_template_is_false_and_creates_nothing(self, store, client):
        template = _template()

        assert store.update_content(template) is False
        assert _raw(client, template.id) is None


class TestGetMany:
    def test_reads_strongly_and_rereads_unprocessed_keys(self, store, client):
        """NFR2.21: まとめ読みは強い整合性で、未処理のキーは読み直す。"""
        first, second = _template(), _template()
        for template in (first, second):
            store.put_new(template)
        real = client.batch_get_item
        calls: list[dict[str, Any]] = []

        def flaky(**kwargs: Any) -> Any:
            calls.append(kwargs)
            if len(calls) == 1:
                return {"Responses": {}, "UnprocessedKeys": kwargs["RequestItems"]}
            return real(**kwargs)

        client.batch_get_item = flaky

        found = store.get_many([first.id, second.id, "missing", first.id])

        assert set(found) == {first.id, second.id}
        assert found[first.id].name == "顧客満足度調査"
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

        with pytest.raises(SurveyTemplateStoreError):
            store.get_many(["a"])

    def test_no_ids_reads_nothing(self, store, client):
        batch = Mock(wraps=client.batch_get_item)
        client.batch_get_item = batch

        assert store.get_many([]) == {}
        assert batch.call_count == 0


class TestItemSize:
    def test_grows_with_the_body(self):
        """NFR2.25: 本体の大きさは属性名と値の合計で見積もる。"""
        small = SurveyTemplateStore.item_size({"id": "a"})
        large = SurveyTemplateStore.item_size({"id": "a", "text": "あ" * 1000})

        assert small < large
        assert large >= 3000

    def test_none_values_are_not_counted(self):
        assert SurveyTemplateStore.item_size(
            {"id": "a", "x": None}
        ) == SurveyTemplateStore.item_size({"id": "a"})

    def test_floats_are_counted(self):
        assert SurveyTemplateStore.item_size({"v": 1.5}) > 0


class TestFailures:
    def test_unreachable_table_raises_the_store_error(self, client):
        store = SurveyTemplateStore(
            region=REGION, table_name="NoSuchTable", client=client
        )

        with pytest.raises(SurveyTemplateStoreError) as exc_info:
            store.get("x")
        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED
