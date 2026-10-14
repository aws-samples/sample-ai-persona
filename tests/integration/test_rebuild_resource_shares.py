"""ResourceShares を本体から作り直すスクリプトの結合テスト（moto、NFR1.20）。

本体の shared_group_ids が正。既定は差分を出すだけで書かない。--apply で直す。
owner_sub のない本体は数えて飛ばす。種類は ResourceSharing の登録から読む。
"""

from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from scripts.rebuild_resource_shares import main, plan_rebuild, apply_plan  # noqa: E402
from src.models.authz import ResourceType  # noqa: E402
from src.models.errors import ErrorCode  # noqa: E402
from src.models.sharing import ResourceShareEntry, SharingTable  # noqa: E402
from src.services.resource_share_store import ResourceShareStore  # noqa: E402
from tests.integration.test_resource_share_store import (  # noqa: E402
    PERSONAS,
    REGION,
    SHARES,
    _create_tables,
)

PERSONA = ResourceType.PERSONA
TABLES = [
    SharingTable(
        resource_type=PERSONA,
        table=PERSONAS,
        name_attribute="name",
        not_found=ErrorCode.PERSONA_NOT_FOUND,
        delete_failed=ErrorCode.PERSONA_OPERATION_FAILED,
        cleanup_attributes=("avatar_path",),
    )
]
CREATED = "2026-10-01T09:00:00"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        _create_tables(client)
        yield client


@pytest.fixture
def store(aws) -> ResourceShareStore:
    return ResourceShareStore(
        region=REGION, shares_table=SHARES, client=aws, sleep=lambda _s: None
    )


def _body(client: Any, pid: str, owner: str | None, shared: list[str]) -> None:
    item: dict[str, Any] = {
        "id": {"S": pid},
        "name": {"S": f"name-{pid}"},
        "created_at": {"S": CREATED},
    }
    if owner:
        item["owner_sub"] = {"S": owner}
    if shared:
        item["shared_group_ids"] = {"SS": shared}
    client.put_item(TableName=PERSONAS, Item=item)


def _entry(
    group: str, pid: str, owner: str, created: str = CREATED
) -> ResourceShareEntry:
    return ResourceShareEntry(group, PERSONA, pid, created, owner)


@pytest.fixture
def drifted(aws, store) -> ResourceShareStore:
    _body(aws, "p1", "sub-a", ["sales", "plan"])  # plan の項目が足りない
    _body(aws, "p2", "sub-a", [])  # 共有なしなのに項目がある（余分）
    _body(aws, "p3", "sub-a", ["sales"])  # created_at が食い違う
    _body(aws, "p4", "sub-b", ["hr"])  # owner_sub が食い違う
    _body(aws, "p5", None, [])  # 所有者のない旧データ（数えて飛ばす）
    _body(aws, "p6", "sub-b", ["plan"])  # 正しい
    store.put_entries(
        [
            _entry("sales", "p1", "sub-a"),
            _entry("sales", "p2", "sub-a"),
            _entry("sales", "p3", "sub-a", "2026-09-01T00:00:00"),
            _entry("hr", "p4", "sub-x"),
            _entry("plan", "p6", "sub-b"),
        ]
    )
    return store


def _index(store: ResourceShareStore) -> set[tuple[str, str, str]]:
    return {(e.group_id, e.share_key, e.owner_sub) for e in store.scan_shares()}


def test_plan_reports_every_kind_of_drift(drifted):
    plan = plan_rebuild(drifted, TABLES)

    assert [(e.group_id, e.resource_id) for e in plan.missing] == [("plan", "p1")]
    assert [(e.group_id, e.resource_id) for e in plan.extra] == [("sales", "p2")]
    assert [(e.group_id, e.resource_id) for e in plan.created_at_mismatch] == [
        ("sales", "p3")
    ]
    assert [(e.group_id, e.resource_id) for e in plan.owner_mismatch] == [("hr", "p4")]
    assert plan.skipped_without_owner == 1
    assert not plan.is_clean


def test_default_run_writes_nothing(drifted, capsys):
    before = _index(drifted)
    exit_code = main(
        ["--region", REGION, "--shares-table", SHARES], store=drifted, tables=TABLES
    )

    assert exit_code == 0
    assert _index(drifted) == before
    out = capsys.readouterr().out
    assert "足りない項目: 1" in out and "余分な項目: 1" in out
    assert "created_at の食い違い: 1" in out and "owner_sub の食い違い: 1" in out
    assert "owner_sub のない本体（飛ばした）: 1" in out
    assert "--apply" in out


def test_apply_makes_the_index_match_the_bodies(drifted, capsys):
    main(["--apply"], store=drifted, tables=TABLES)

    assert _index(drifted) == {
        ("sales", f"persona#{CREATED}#p1", "sub-a"),
        ("plan", f"persona#{CREATED}#p1", "sub-a"),
        ("sales", f"persona#{CREATED}#p3", "sub-a"),
        ("hr", f"persona#{CREATED}#p4", "sub-b"),
        ("plan", f"persona#{CREATED}#p6", "sub-b"),
    }
    assert plan_rebuild(drifted, TABLES).is_clean
    assert "直しました" in capsys.readouterr().out


def test_clean_index_reports_nothing_to_fix(aws, store, capsys):
    _body(aws, "p1", "sub-a", ["sales"])
    store.put_entries([_entry("sales", "p1", "sub-a")])
    plan = plan_rebuild(store, TABLES)
    assert plan.is_clean
    apply_plan(store, plan)  # 何もしない
    main([], store=store, tables=TABLES)
    assert "ずれはありません" in capsys.readouterr().out


def test_entries_of_unregistered_types_are_left_alone(aws, store):
    store.put_entries(
        [ResourceShareEntry("sales", ResourceType.DISCUSSION, "d1", CREATED, "s")]
    )
    plan = plan_rebuild(store, TABLES)
    assert plan.extra == []


def test_store_is_built_from_the_arguments(monkeypatch, aws):
    built: dict[str, Any] = {}

    class FakeStore:
        def __init__(self, *, region: str, shares_table: str) -> None:
            built.update(region=region, shares_table=shares_table)

        def scan_items(self, table, attributes):
            return []

        def scan_shares(self):
            return []

    monkeypatch.setattr("scripts.rebuild_resource_shares.ResourceShareStore", FakeStore)
    assert (
        main(["--region", "us-west-2", "--shares-table", "Shares-x"], tables=TABLES)
        == 0
    )
    assert built == {"region": "us-west-2", "shares_table": "Shares-x"}
