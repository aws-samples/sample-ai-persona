"""まとめ読み（BatchGetItem）の共通の約束を、単純なテーブルのストアごとに確かめる。moto を使う。

NFR2.5・NFR2.21: まとめ読みは強い整合性で、未処理のキーは読み直す。100 件ずつに
分けて読み、読み直しても未処理が残れば行を黙って落とさずストアの例外にする。

同じ実装が各ストアにあるため、テストはここで 1 つにまとめる。議論（射影と複数の
テーブル）とカスタムペルソナデータ（待ち時間つきの再試行）は読み方が違うので、
それぞれのテストファイルで確かめる。
"""

from dataclasses import dataclass
from typing import Any, Callable
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.services.stores.persona_store import PersonaStore, PersonaStoreError  # noqa: E402
from src.services.stores.survey_store import SurveyStore, SurveyStoreError  # noqa: E402
from src.services.stores.survey_template_store import (  # noqa: E402
    SurveyTemplateStore,
    SurveyTemplateStoreError,
)
from src.services.stores.uploaded_file_store import (  # noqa: E402
    UploadedFileStore,
    UploadedFileStoreError,
)
from tests.integration.test_persona_store import _persona  # noqa: E402
from tests.integration.test_survey_store import _survey  # noqa: E402
from tests.integration.test_survey_template_store import _template  # noqa: E402
from tests.integration.test_uploaded_file_store import _file  # noqa: E402

REGION = "ap-northeast-1"
TABLE = "BatchReadTest"


@dataclass(frozen=True)
class StoreCase:
    """A store whose ``get_many`` reads one table keyed by ``id``."""

    store_type: Callable[..., Any]
    error_type: type[Exception]
    #: Saves one new row and returns its id.
    save_one: Callable[[Any], str]


def _save(make: Callable[[], Any], method: str) -> Callable[[Any], str]:
    def save(store: Any) -> str:
        row = make()
        getattr(store, method)(row)
        return row.id

    return save


CASES = {
    "persona": StoreCase(
        PersonaStore, PersonaStoreError, _save(lambda: _persona(), "put_new")
    ),
    "survey": StoreCase(SurveyStore, SurveyStoreError, _save(_survey, "put_new")),
    "survey_template": StoreCase(
        SurveyTemplateStore, SurveyTemplateStoreError, _save(_template, "put_new")
    ),
    "uploaded_file": StoreCase(
        UploadedFileStore, UploadedFileStoreError, _save(_file, "put")
    ),
}


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


@pytest.fixture(params=sorted(CASES))
def case(request) -> StoreCase:
    return CASES[request.param]


@pytest.fixture
def store(case: StoreCase, client: Any) -> Any:
    return case.store_type(region=REGION, table_name=TABLE, client=client)


def test_reads_strongly_and_rereads_unprocessed_keys(case, store, client):
    ids = [case.save_one(store) for _ in range(3)]
    real = client.batch_get_item
    calls: list[dict[str, Any]] = []

    def partial(**kwargs: Any) -> Any:
        calls.append(kwargs)
        request = kwargs["RequestItems"][TABLE]
        if len(calls) == 1:
            # one key is answered, the rest come back unprocessed
            first = real(RequestItems={TABLE: {**request, "Keys": request["Keys"][:1]}})
            first["UnprocessedKeys"] = {TABLE: {**request, "Keys": request["Keys"][1:]}}
            return first
        return real(**kwargs)

    client.batch_get_item = partial

    found = store.get_many(ids + ["missing", ids[0]])

    assert set(found) == set(ids)
    assert all(found[i].id == i for i in ids)
    assert len(calls) == 2
    assert all(c["RequestItems"][TABLE]["ConsistentRead"] is True for c in calls)


def test_reads_in_chunks_of_100(store, client):
    batch = Mock(wraps=client.batch_get_item)
    client.batch_get_item = batch

    assert store.get_many([f"id-{i}" for i in range(150)]) == {}

    sizes = [len(c.kwargs["RequestItems"][TABLE]["Keys"]) for c in batch.mock_calls]
    assert sizes == [100, 50]


def test_keys_left_unprocessed_raise_after_three_rereads(case, store, client):
    calls: list[int] = []

    def never(**kwargs: Any) -> Any:
        calls.append(1)
        return {"Responses": {}, "UnprocessedKeys": kwargs["RequestItems"]}

    client.batch_get_item = never

    with pytest.raises(case.error_type):
        store.get_many(["a"])
    assert len(calls) == 4  # the first read and three re-reads
