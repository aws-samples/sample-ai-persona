"""Shared moto set-up of the advanced generation (beta) Manager tests (U1).

The tables are made with the same keys and GSI as the CDK (contracts C4, C5).
"""

from __future__ import annotations

from typing import Any

OWNER_INDEX = "owner_sub-created_at"
SOURCE_FILES_TABLE = "SourceFilesTest"
GENERATIONS_TABLE = "AdvancedGenerationsTest"
CANDIDATES_TABLE = "PersonaCandidatesTest"
BUCKET = "advanced-test-bucket"
REGION = "us-east-1"

_OWNER_GSI = {
    "IndexName": OWNER_INDEX,
    "KeySchema": [
        {"AttributeName": "owner_sub", "KeyType": "HASH"},
        {"AttributeName": "created_at", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "ALL"},
}


def create_source_files_table(client: Any) -> None:
    """``SourceFiles``: PK ``source_file_id``, GSI ``owner_sub-created_at``."""
    client.create_table(
        TableName=SOURCE_FILES_TABLE,
        KeySchema=[{"AttributeName": "source_file_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "source_file_id", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[_OWNER_GSI],
        BillingMode="PAY_PER_REQUEST",
    )


def create_generations_table(client: Any) -> None:
    """``AdvancedGenerations``: ``pk`` / ``sk``, GSI ``owner_sub-created_at``."""
    client.create_table(
        TableName=GENERATIONS_TABLE,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[_OWNER_GSI],
        BillingMode="PAY_PER_REQUEST",
    )


def create_candidates_table(client: Any) -> None:
    """``PersonaCandidates``: PK ``candidate_id`` (the existing store)."""
    client.create_table(
        TableName=CANDIDATES_TABLE,
        KeySchema=[{"AttributeName": "candidate_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "candidate_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
