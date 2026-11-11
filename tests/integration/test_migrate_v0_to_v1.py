"""v0 → v1 の移行スクリプトの結合テスト（moto）。

v0（us-east-1）と v1（ap-northeast-1）のテーブル・バケット・ユーザープールを moto で作り、
dry-run は何も書かないこと、--apply で各リソースが v1 の形で入ること、再実行しても
すべて「移行済み」になること（冪等）、v0 は変わらないことを確かめる。
"""

import io
from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for AWS tests")

import boto3  # noqa: E402
import polars as pl  # noqa: E402
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer  # noqa: E402

from scripts import migrate_v0_to_v1  # noqa: E402
from scripts.v0_migration.discover import Environment  # noqa: E402
from scripts.v0_migration.owners import MappingError, load_mapping  # noqa: E402
from scripts.v0_migration.runner import MEMBER_GROUP, Clients, Migration  # noqa: E402
from scripts.v0_migration.selection import select_kinds  # noqa: E402
from src.models.custom_persona_data import custom_file_key  # noqa: E402
from scripts.v0_migration.convert import stable_id  # noqa: E402

V0_REGION = "us-east-1"
V1_REGION = "ap-northeast-1"
V0_BUCKET = "aipersonadev-uploads-123456789012-us-east-1"
V1_BUCKET = "aipersonadevdata-uploadbucket-xyz"
PREFIX = "AIPersonaDev"
FILE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
IMAGE_ID = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
ADMIN = "admin@example.com"

V0_TABLES = {
    "personas": "Personas",
    "discussions": "Discussions",
    "uploaded_files": "UploadedFiles",
    "datasets": "Datasets",
    "persona_dataset_bindings": "PersonaDatasetBindings",
    "survey_templates": "SurveyTemplates",
    "surveys": "Surveys",
    "knowledge_bases": "KnowledgeBases",
    "persona_kb_bindings": "PersonaKBBindings",
}
V1_KEYS: dict[str, list[tuple[str, str]]] = {
    "personas": [("id", "S")],
    "discussions": [("id", "S")],
    "discussion_messages": [("discussion_id", "S"), ("seq", "N")],
    "discussion_reports": [("discussion_id", "S"), ("report_id", "S")],
    "uploaded_files": [("id", "S")],
    "datasets": [("id", "S")],
    "survey_templates": [("id", "S")],
    "surveys": [("id", "S")],
    "knowledge_bases": [("id", "S")],
    "custom_persona_data": [("id", "S")],
    "persona_bindings": [("persona_id", "S"), ("target_key", "S")],
    "resource_shares": [("group_id", "S"), ("share_key", "S")],
}

_ser = TypeSerializer()
_de = TypeDeserializer()


def _create(client: Any, name: str, keys: list[tuple[str, str]]) -> None:
    client.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": n, "KeyType": "HASH" if i == 0 else "RANGE"}
            for i, (n, _t) in enumerate(keys)
        ],
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": t} for n, t in keys
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _put(client: Any, table: str, item: dict[str, Any]) -> None:
    client.put_item(
        TableName=table, Item={k: _ser.serialize(v) for k, v in item.items()}
    )


def _scan(client: Any, table: str) -> list[dict[str, Any]]:
    items = client.scan(TableName=table).get("Items", [])
    return [{k: _de.deserialize(v) for k, v in i.items()} for i in items]


def _parquet() -> bytes:
    buf = io.BytesIO()
    pl.DataFrame({"age": [30, 40], "sex": ["男", "女"]}).write_parquet(buf)
    return buf.getvalue()


def _v0_data(ddb: Any, s3: Any) -> None:
    t = {k: f"{PREFIX}_{v}" for k, v in V0_TABLES.items()}
    s3.put_object(Bucket=V0_BUCKET, Key="persona_avatars/p1/a.webp", Body=b"avatar")
    s3.put_object(
        Bucket=V0_BUCKET, Key=f"discussion_documents/{FILE_ID}_資料.pdf", Body=b"pdf"
    )
    s3.put_object(
        Bucket=V0_BUCKET, Key=f"survey_images/{IMAGE_ID}_画像.png", Body=b"png"
    )
    s3.put_object(Bucket=V0_BUCKET, Key="survey-results/s1/results.csv", Body=b"a,b\n")
    s3.put_object(Bucket=V0_BUCKET, Key="datasets/ds1.csv", Body=b"c\n1\n")
    s3.put_object(
        Bucket=V0_BUCKET, Key="persona-dataset/custom/営業.parquet", Body=_parquet()
    )
    s3.put_object(
        Bucket=V0_BUCKET,
        Key="persona-dataset/custom/営業.meta.json",
        Body='{"standard_mapping": {"age": "age"}, "extra_columns": []}'.encode(),
    )
    s3.put_object(
        Bucket=V0_BUCKET,
        Key="persona-dataset/nemotron-personas-japan.parquet",
        Body=b"big",
    )
    base = {"created_at": "2026-01-01T09:00:00", "updated_at": "2026-01-01T09:00:00"}
    for pid, extra in (
        ("p1", {"avatar_path": f"s3://{V0_BUCKET}/persona_avatars/p1/a.webp"}),
        ("p2", {}),
    ):
        _put(
            ddb,
            t["personas"],
            {
                "id": pid,
                "name": f"名前{pid}",
                "age": 30,
                "occupation": "会社員",
                "background": "背景",
                "values": ["v"],
                "pain_points": ["p"],
                "goals": ["g"],
                "type": "persona",
                **base,
                **extra,
            },
        )
    _put(
        ddb,
        t["uploaded_files"],
        {
            "id": FILE_ID,
            "filename": f"{FILE_ID}_資料.pdf",
            "original_filename": "資料.pdf",
            "file_path": f"s3://{V0_BUCKET}/discussion_documents/{FILE_ID}_資料.pdf",
            "uploaded_at": "2026-01-01T08:00:00",
            "file_type": "discussion_document",
            "type": "file",
            "file_size": 3,
            "mime_type": "application/pdf",
        },
    )
    _put(
        ddb,
        t["uploaded_files"],
        {
            "id": IMAGE_ID,
            "filename": f"{IMAGE_ID}_画像.png",
            "original_filename": "画像.png",
            "file_path": f"s3://{V0_BUCKET}/survey_images/{IMAGE_ID}_画像.png",
            "uploaded_at": "2026-01-01T08:00:00",
            "file_type": "survey_image",
            "type": "file",
            "file_size": 3,
        },
    )
    _put(
        ddb,
        t["discussions"],
        {
            "id": "d1",
            "topic": "議題",
            "participants": ["p1", "p2"],
            "messages": [
                {
                    "persona_id": "p1",
                    "persona_name": "名前p1",
                    "content": "発言",
                    "timestamp": "2026-01-01T09:01:00",
                    "message_type": "statement",
                }
            ],
            "insights": [],
            "created_at": "2026-01-01T09:00:00",
            "mode": "classic",
            "type": "discussion",
            "documents": [
                {
                    "id": FILE_ID,
                    "filename": "資料.pdf",
                    "file_path": "x",
                    "file_size": 3,
                    "mime_type": "application/pdf",
                    "uploaded_at": "2026-01-01T08:00:00",
                }
            ],
            "reports": [
                {
                    "id": "r1",
                    "template_type": "summary",
                    "content": "要約",
                    "created_at": "2026-01-01T10:00:00",
                }
            ],
        },
    )
    _put(
        ddb,
        t["survey_templates"],
        {
            "id": "t1",
            "name": "テンプレート",
            "questions": [
                {
                    "id": "q1",
                    "text": "質問",
                    "question_type": "free_text",
                    "options": [],
                    "scale_min": 1,
                    "scale_max": 5,
                }
            ],
            "images": [
                {
                    "id": "i1",
                    "name": "画像",
                    "file_path": f"s3://{V0_BUCKET}/survey_images/{IMAGE_ID}_画像.png",
                    "mime_type": "image/png",
                    "original_filename": "画像.png",
                }
            ],
            **base,
        },
    )
    _put(
        ddb,
        t["surveys"],
        {
            "id": "s1",
            "name": "調査",
            "description": "",
            "template_id": "t1",
            "persona_count": 2,
            "status": "completed",
            "s3_result_path": f"s3://{V0_BUCKET}/survey-results/s1/results.csv",
            "datasource": "custom:営業",
            **base,
        },
    )
    _put(
        ddb,
        t["datasets"],
        {
            "id": "ds1",
            "name": "購買",
            "description": "",
            "s3_path": f"s3://{V0_BUCKET}/datasets/ds1.csv",
            "columns": [{"name": "c", "data_type": "int", "description": ""}],
            "row_count": 1,
            "notes": "",
            **base,
        },
    )
    _put(
        ddb,
        t["knowledge_bases"],
        {
            "id": "k1",
            "knowledge_base_id": "KB1",
            "name": "社内",
            "description": "",
            **base,
        },
    )
    _put(
        ddb,
        t["persona_dataset_bindings"],
        {
            "id": "b1",
            "persona_id": "p1",
            "dataset_id": "ds1",
            "binding_keys": {"customer_id": "C1"},
            "created_at": "2026-01-01T09:00:00",
        },
    )
    _put(
        ddb,
        t["persona_kb_bindings"],
        {
            "id": "b2",
            "persona_id": "p2",
            "kb_id": "k1",
            "metadata_filters": {},
            "created_at": "2026-01-01T09:00:00",
        },
    )


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = Clients(
            v0_dynamodb=boto3.client("dynamodb", region_name=V0_REGION),
            v0_s3=boto3.client("s3", region_name=V0_REGION),
            v0_cognito=boto3.client("cognito-idp", region_name=V0_REGION),
            v1_dynamodb=boto3.client("dynamodb", region_name=V1_REGION),
            v1_s3=boto3.client("s3", region_name=V1_REGION),
            v1_cognito=boto3.client("cognito-idp", region_name=V1_REGION),
        )
        for key, suffix in V0_TABLES.items():
            _create(c.v0_dynamodb, f"{PREFIX}_{suffix}", [("id", "S")])
        for key, keys in V1_KEYS.items():
            _create(c.v1_dynamodb, f"v1-{key}", keys)
        c.v0_s3.create_bucket(Bucket=V0_BUCKET)
        c.v1_s3.create_bucket(
            Bucket=V1_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": V1_REGION},
        )
        v0_pool = c.v0_cognito.create_user_pool(PoolName="v0")["UserPool"]["Id"]
        v1_pool = c.v1_cognito.create_user_pool(
            PoolName="v1", UsernameAttributes=["email"]
        )["UserPool"]["Id"]
        for group in ("role:admin", MEMBER_GROUP, "group:sales"):
            c.v1_cognito.create_group(UserPoolId=v1_pool, GroupName=group)
        for email in ("Admin@example.com", "member@example.com"):
            c.v0_cognito.admin_create_user(
                UserPoolId=v0_pool,
                Username=email.split("@")[0],
                UserAttributes=[{"Name": "email", "Value": email}],
            )
        c.v1_cognito.admin_create_user(
            UserPoolId=v1_pool,
            Username=ADMIN,
            UserAttributes=[{"Name": "email", "Value": ADMIN}],
        )
        c.v1_cognito.admin_add_user_to_group(
            UserPoolId=v1_pool, Username=ADMIN, GroupName="role:admin"
        )
        _v0_data(c.v0_dynamodb, c.v0_s3)
        v0 = Environment(
            region=V0_REGION,
            tables={k: f"{PREFIX}_{v}" for k, v in V0_TABLES.items()},
            bucket=V0_BUCKET,
            user_pool_id=v0_pool,
        )
        v1 = Environment(
            region=V1_REGION,
            tables={k: f"v1-{k}" for k in V1_KEYS},
            bucket=V1_BUCKET,
            user_pool_id=v1_pool,
        )
        yield c, v0, v1


def _mapping(tmp_path, text: str | None):
    if text is None:
        return load_mapping(None)
    path = tmp_path / "mapping.csv"
    path.write_text("resource_type,resource_id,owner_email,group_ids,action\n" + text)
    return load_mapping(path)


def _run(world, *, apply, mapping=None, include=None, exclude=None):
    c, v0, v1 = world
    return Migration(
        v0=v0,
        v1=v1,
        clients=c,
        kinds=select_kinds(include, exclude),
        mapping=mapping or load_mapping(None),
        default_owner=ADMIN,
        apply=apply,
        clock=lambda: 1_800_000_000,
    ).run()


def _sub(c: Any, pool: str, email: str) -> str:
    user = c.v1_cognito.admin_get_user(UserPoolId=pool, Username=email)
    return {a["Name"]: a["Value"] for a in user["UserAttributes"]}["sub"]


def _groups(c: Any, pool: str, email: str) -> set[str]:
    groups = c.v1_cognito.admin_list_groups_for_user(UserPoolId=pool, Username=email)
    return {g["GroupName"] for g in groups["Groups"]}


def _v1_items(c: Any, v1: Environment) -> int:
    return sum(len(_scan(c.v1_dynamodb, name)) for name in v1.tables.values())


def test_dry_run_writes_nothing(world, tmp_path):
    c, v0, v1 = world
    report = _run(
        world, apply=False, mapping=_mapping(tmp_path, "persona,p1,,sales,\n")
    )
    assert report.failed == 0
    assert _v1_items(c, v1) == 0
    assert c.v1_s3.list_objects_v2(Bucket=V1_BUCKET).get("KeyCount", 0) == 0
    users = c.v1_cognito.list_users(UserPoolId=v1.user_pool_id)["Users"]
    assert len(users) == 1
    assert report["users"].created == 1
    assert report["users"].existing == 1
    assert report["personas"].created == 2
    assert report["personas"].shared == 1
    assert report["bindings"].created == 2
    assert report["discussions"].created == 1


def test_apply_migrates_everything(world, tmp_path):
    c, v0, v1 = world
    before = {name: _scan(c.v0_dynamodb, name) for name in v0.tables.values()}
    mapping = _mapping(
        tmp_path,
        "persona,p1,member@example.com,sales,\n"
        "discussion,d1,,sales,\n"
        "knowledge_base,k1,,sales,\n",
    )
    report = _run(world, apply=True, mapping=mapping)
    assert report.failed == 0, report.render(apply=True)
    pool = v1.user_pool_id
    admin_sub = _sub(c, pool, ADMIN)
    member_sub = _sub(c, pool, "member@example.com")

    # users: the first admin is left alone; the member is new with role:member
    assert _groups(c, pool, ADMIN) == {"role:admin"}
    assert _groups(c, pool, "member@example.com") == {MEMBER_GROUP}
    assert report["users"].existing == 1

    personas = {p["id"]: p for p in _scan(c.v1_dynamodb, v1.table("personas"))}
    assert personas["p1"]["owner_sub"] == member_sub
    assert personas["p2"]["owner_sub"] == admin_sub
    assert personas["p1"]["shared_group_ids"] == {"sales"}
    assert (
        personas["p1"]["avatar_path"] == f"s3://{V1_BUCKET}/persona_avatars/p1/a.webp"
    )
    avatar = c.v1_s3.get_object(Bucket=V1_BUCKET, Key="persona_avatars/p1/a.webp")
    assert avatar["Body"].read() == b"avatar"

    shares = _scan(c.v1_dynamodb, v1.table("resource_shares"))
    assert {(s["group_id"], s["resource_type"], s["resource_id"]) for s in shares} == {
        ("sales", "persona", "p1"),
        ("sales", "discussion", "d1"),
    }

    [discussion] = _scan(c.v1_dynamodb, v1.table("discussions"))
    assert discussion["owner_sub"] == admin_sub
    assert discussion["message_count"] == 1
    assert discussion["report_count"] == 1
    assert "messages" not in discussion and "reports" not in discussion
    [message] = _scan(c.v1_dynamodb, v1.table("discussion_messages"))
    assert message["seq"] == 1 and message["persona_name_at_time"] == "名前p1"
    [stored_report] = _scan(c.v1_dynamodb, v1.table("discussion_reports"))
    assert stored_report["report_id"] == "r1"

    files = {f["id"]: f for f in _scan(c.v1_dynamodb, v1.table("uploaded_files"))}
    doc_key = f"uploads/{admin_sub}/discussion_document/{FILE_ID}_資料.pdf"
    assert files[FILE_ID]["s3_key"] == doc_key
    assert files[FILE_ID]["parent_id"] == "d1"
    assert "expires_at" not in files[FILE_ID]
    assert c.v1_s3.get_object(Bucket=V1_BUCKET, Key=doc_key)["Body"].read() == b"pdf"

    [template] = _scan(c.v1_dynamodb, v1.table("survey_templates"))
    image_key = f"uploads/{admin_sub}/survey_image/{IMAGE_ID}_画像.png"
    assert template["images"][0]["file_path"] == f"s3://{V1_BUCKET}/{image_key}"
    assert files[IMAGE_ID]["parent_id"] == "t1"

    custom_id = stable_id("custom_persona_data", "営業")
    [custom] = [
        i
        for i in _scan(c.v1_dynamodb, v1.table("custom_persona_data"))
        if i["id"] == custom_id
    ]
    assert custom["row_count"] == 2
    assert custom["columns"] == ["age", "sex"]
    assert c.v1_s3.head_object(Bucket=V1_BUCKET, Key=custom_file_key(custom_id))

    [survey] = _scan(c.v1_dynamodb, v1.table("surveys"))
    assert survey["datasource"] == f"custom:{custom_id}"
    assert survey["datasource_name"] == "営業"
    assert survey["s3_result_path"] == f"s3://{V1_BUCKET}/survey-results/s1/results.csv"

    [kb] = _scan(c.v1_dynamodb, v1.table("knowledge_bases"))
    assert kb["shared_group_ids"] == {"sales"}
    assert report["knowledge_bases"].messages  # the region warning
    [dataset] = _scan(c.v1_dynamodb, v1.table("datasets"))
    assert dataset["s3_path"] == f"s3://{V1_BUCKET}/datasets/ds1.csv"

    bindings = {
        (b["persona_id"], b["target_key"])
        for b in _scan(c.v1_dynamodb, v1.table("persona_bindings"))
    }
    assert bindings == {("p1", "DATASET#ds1"), ("p2", "KB")}

    # the Nemotron data is not copied
    keys = {o["Key"] for o in c.v1_s3.list_objects_v2(Bucket=V1_BUCKET)["Contents"]}
    assert not any(k.startswith("persona-dataset/nemotron") for k in keys)

    # v0 is untouched
    assert {name: _scan(c.v0_dynamodb, name) for name in v0.tables.values()} == before


def test_second_run_finds_everything_migrated(world, tmp_path):
    c, _v0, v1 = world
    mapping = _mapping(tmp_path, "persona,p1,,sales,\n")
    _run(world, apply=True, mapping=mapping)
    count = _v1_items(c, v1)
    again = _run(world, apply=True, mapping=mapping)
    assert again.failed == 0
    for kind, rep in again.kinds.items():
        assert rep.created == 0, kind
    assert again["personas"].existing == 2
    assert again["users"].existing == 2
    assert _v1_items(c, v1) == count


def test_excluded_kinds_and_dependencies(world):
    c, _v0, v1 = world
    report = _run(world, apply=True, exclude=["personas", "users"])
    assert report.failed == 0
    assert report["personas"].excluded
    assert report["bindings"].skipped["persona_missing"] == 2
    assert _scan(c.v1_dynamodb, v1.table("persona_bindings")) == []
    assert _scan(c.v1_dynamodb, v1.table("personas")) == []
    assert len(_scan(c.v1_dynamodb, v1.table("discussions"))) == 1
    assert len(c.v1_cognito.list_users(UserPoolId=v1.user_pool_id)["Users"]) == 1


def test_custom_data_excluded_keeps_the_name(world):
    c, _v0, v1 = world
    report = _run(world, apply=True, include=["survey_templates", "surveys"])
    [survey] = _scan(c.v1_dynamodb, v1.table("surveys"))
    assert survey["datasource"] == "custom:営業"
    assert survey["datasource_name"] == "営業"
    assert report["surveys"].messages


def test_mapping_skip(world, tmp_path):
    c, _v0, v1 = world
    report = _run(
        world, apply=True, mapping=_mapping(tmp_path, "discussion,d1,,,skip\n")
    )
    assert report["discussions"].skipped["mapping_skip"] == 1
    assert _scan(c.v1_dynamodb, v1.table("discussions")) == []


def test_unknown_department_stops_before_writing(world, tmp_path):
    c, _v0, v1 = world
    with pytest.raises(MappingError, match="planning"):
        _run(world, apply=True, mapping=_mapping(tmp_path, "persona,p1,,planning,\n"))
    assert _v1_items(c, v1) == 0


def test_unknown_default_owner(world):
    c, v0, v1 = world
    with pytest.raises(MappingError, match="default owner"):
        Migration(
            v0=v0,
            v1=v1,
            clients=c,
            kinds=select_kinds(["personas"], None),
            mapping=load_mapping(None),
            default_owner="nobody@example.com",
            apply=False,
        ).run()


def test_missing_source_object_fails_the_item(world):
    c, _v0, v1 = world
    c.v0_s3.delete_object(Bucket=V0_BUCKET, Key="persona_avatars/p1/a.webp")
    report = _run(world, apply=True, include=["personas"])
    assert report["personas"].failed == 1
    assert report["personas"].created == 1
    assert {p["id"] for p in _scan(c.v1_dynamodb, v1.table("personas"))} == {"p2"}


def test_cli_exit_status(world, monkeypatch, capsys):
    c, v0, v1 = world
    monkeypatch.setattr(migrate_v0_to_v1, "discover_v0", lambda *_a, **_k: v0)
    monkeypatch.setattr(migrate_v0_to_v1, "discover_v1", lambda *_a, **_k: v1)

    def factory(service: str, region: str) -> Any:
        return boto3.client(service, region_name=region)

    assert (
        migrate_v0_to_v1.main(["--default-owner", ADMIN], client_factory=factory) == 0
    )
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert _v1_items(c, v1) == 0
    assert (
        migrate_v0_to_v1.main(
            ["--default-owner", ADMIN, "--include", "personas,unknown"],
            client_factory=factory,
        )
        == 2
    )
    c.v0_s3.delete_object(Bucket=V0_BUCKET, Key="persona_avatars/p1/a.webp")
    assert (
        migrate_v0_to_v1.main(
            ["--default-owner", ADMIN, "--include", "personas", "--apply"],
            client_factory=factory,
        )
        == 1
    )


def test_v1_stores_read_the_migrated_items(world, tmp_path):
    """The app's own stores and models read what the migration wrote."""
    from src.models.discussion import Discussion
    from src.services.stores.custom_persona_data_store import CustomPersonaDataStore
    from src.services.stores.discussion_store import DiscussionStore
    from src.services.stores.persona_binding_store import PersonaBindingStore
    from src.services.stores.persona_store import PersonaStore
    from src.services.stores.survey_store import SurveyStore
    from src.services.stores.survey_template_store import SurveyTemplateStore
    from src.services.stores.uploaded_file_store import UploadedFileStore

    c, _v0, v1 = world
    _run(world, apply=True, mapping=_mapping(tmp_path, "persona,p1,,sales,\n"))
    ddb, region, t = c.v1_dynamodb, V1_REGION, v1.table
    persona = PersonaStore(region=region, table_name=t("personas"), client=ddb).get(
        "p1"
    )
    assert persona is not None and persona.shared_group_ids == frozenset({"sales"})
    store = DiscussionStore(
        region=region,
        discussions_table=t("discussions"),
        messages_table=t("discussion_messages"),
        reports_table=t("discussion_reports"),
        client=ddb,
    )
    body = store.get("d1")
    assert body is not None
    discussion = Discussion.from_dict(body)
    assert not discussion.is_legacy
    assert [m.content for m in store.query_messages("d1", up_to=1)] == ["発言"]
    assert [r.report_id for r in store.query_reports("d1")] == ["r1"]
    uploaded = UploadedFileStore(
        region=region, table_name=t("uploaded_files"), client=ddb
    )
    doc = uploaded.get(FILE_ID)
    assert doc is not None and doc.is_attached
    template = SurveyTemplateStore(
        region=region, table_name=t("survey_templates"), client=ddb
    ).get("t1")
    assert template is not None and len(template.images) == 1
    survey = SurveyStore(region=region, table_name=t("surveys"), client=ddb).get("s1")
    assert survey is not None and survey.datasource_label == "営業"
    custom = CustomPersonaDataStore(
        region=region, table_name=t("custom_persona_data"), client=ddb
    ).get(stable_id("custom_persona_data", "営業"))
    assert custom is not None and custom.name == "営業"
    bindings = PersonaBindingStore(
        region=region, table_name=t("persona_bindings"), client=ddb
    ).list_for_persona("p1")
    assert [b.target_id for b in bindings] == ["ds1"]


def test_closed_deployment_creates_users_without_an_invitation(world, monkeypatch):
    c, v0, v1 = world
    calls: list[dict[str, Any]] = []
    original = c.v1_cognito.admin_create_user

    def spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(c.v1_cognito, "admin_create_user", spy)
    report = Migration(
        v0=v0,
        v1=v1,
        clients=c,
        kinds=select_kinds(["users"], None),
        mapping=load_mapping(None),
        default_owner=ADMIN,
        apply=True,
        invite=False,
    ).run()
    assert report["users"].created == 1
    [call] = calls
    assert call["MessageAction"] == "SUPPRESS"
    assert "DesiredDeliveryMediums" not in call
