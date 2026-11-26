"""Checks for the local table-creation script.

``scripts/create_dynamodb_tables.py`` must create the same tables and GSIs that the
CDK Data stack expects (``EXPECTED_GSIS`` in ``cdk/test/data-stack.test.ts``). pytest
cannot read the TypeScript, so the expectation is written out by hand; when a GSI
changes, update both lists together. DynamoDB is moto; AWS is never touched.
"""

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from scripts.create_dynamodb_tables import DynamoDBTableCreator  # noqa: E402

PREFIX = "Test"
REGION = "us-east-1"

# Expected state: no type-pinned GSIs remain.
EXPECTED_GSIS: dict[str, list[str]] = {
    "Personas": ["OwnerIndex"],
    "Discussions": ["OwnerIndex"],
    "UploadedFiles": [],
    "Datasets": [],
    "SurveyTemplates": ["OwnerIndex"],
    # Sparse index for queued and running runs (after the move to Step Functions)
    "Surveys": ["OwnerIndex", "RunQueueIndex"],
    "KnowledgeBases": [],
    "Jobs": [],
}

# Two legacy binding tables that are no longer managed by the CDK
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
