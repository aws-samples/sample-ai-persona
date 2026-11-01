"""ローカルのテーブル作成スクリプトの検査（U9、NFR2.27、NFR2.28、cicd-pipeline.md）。

``scripts/create_dynamodb_tables.py`` が作るテーブルと GSI を、CDK の Data スタックの
期待（``cdk/test/data-stack.test.ts`` の ``EXPECTED_GSIS``）と同じ内容に保つ。CDK は
TypeScript のため pytest からは読まず、期待を手で書く。GSI を変えるときは
両方の一覧を一緒に直す。DynamoDB は moto で、AWS には触れない。
"""

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from scripts.create_dynamodb_tables import DynamoDBTableCreator  # noqa: E402

PREFIX = "Test"
REGION = "us-east-1"

# U9 の期待（型固定の GSI 7 つをすべて外した）。
EXPECTED_GSIS: dict[str, list[str]] = {
    "Personas": ["OwnerIndex"],
    "Discussions": ["OwnerIndex"],
    "UploadedFiles": [],
    "Datasets": [],
    "SurveyTemplates": ["OwnerIndex"],
    # 待ちと実行中の疎な索引（Step Functions への移行、NFR3.10）
    "Surveys": ["OwnerIndex", "RunQueueIndex"],
    "KnowledgeBases": [],
    "Jobs": [],
}

# CDK の管理から外した古い紐付けの 2 テーブル（NFR2.28）
REMOVED_TABLES = ("PersonaDatasetBindings", "PersonaKBBindings")


@pytest.fixture
def created_tables():
    """Run the script's table creation against moto and return the DynamoDB client."""
    with moto.mock_aws():
        creator = DynamoDBTableCreator(table_prefix=PREFIX, region=REGION)
        assert creator.create_all_tables(wait_for_active=False) is True
        yield boto3.client("dynamodb", region_name=REGION)


def _gsi_names(client, name: str) -> list[str]:
    table = client.describe_table(TableName=f"{PREFIX}_{name}")["Table"]
    return sorted(g["IndexName"] for g in table.get("GlobalSecondaryIndexes", []))


def test_creates_exactly_the_expected_tables(created_tables):
    names = sorted(created_tables.list_tables()["TableNames"])
    assert names == sorted(f"{PREFIX}_{name}" for name in EXPECTED_GSIS)


@pytest.mark.parametrize("removed", REMOVED_TABLES)
def test_does_not_create_the_removed_binding_tables(created_tables, removed):
    assert f"{PREFIX}_{removed}" not in created_tables.list_tables()["TableNames"]


@pytest.mark.parametrize("name", sorted(EXPECTED_GSIS))
def test_gsi_names_match_the_cdk_expectation(created_tables, name):
    assert _gsi_names(created_tables, name) == sorted(EXPECTED_GSIS[name])


def test_owner_index_is_owner_sub_and_created_at_keys_only(created_tables):
    for name in ("Personas", "Discussions", "SurveyTemplates", "Surveys"):
        table = created_tables.describe_table(TableName=f"{PREFIX}_{name}")["Table"]
        owner = next(
            g for g in table["GlobalSecondaryIndexes"] if g["IndexName"] == "OwnerIndex"
        )
        assert owner["KeySchema"] == [
            {"AttributeName": "owner_sub", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"},
        ]
        assert owner["Projection"] == {"ProjectionType": "KEYS_ONLY"}
