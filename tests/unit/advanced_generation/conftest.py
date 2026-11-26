"""Fixtures of the AdvancedPersonaGenerationManager tests (moto DynamoDB and S3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3
import pytest

moto = pytest.importorskip("moto")

from src.config import config  # noqa: E402
from tests.advanced_helpers import (  # noqa: E402
    BUCKET,
    REGION,
    create_candidates_table,
    create_generations_table,
    create_source_files_table,
)
from tests.unit.advanced_generation.helpers import _Env  # noqa: E402


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", True)
    monkeypatch.setattr(config, "CODE_INTERPRETER_ID", "ci-1")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        dynamo = boto3.client("dynamodb", region_name=REGION)
        create_source_files_table(dynamo)
        create_generations_table(dynamo)
        create_candidates_table(dynamo)
        s3_client = boto3.client("s3", region_name=REGION)
        s3_client.create_bucket(Bucket=BUCKET)
        yield _Env(dynamo, s3_client, tmp_path)


@pytest.fixture
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunks of 300 characters with 50 of overlap, handed over 120 at a time."""
    monkeypatch.setattr(config, "ADVANCED_DOCUMENT_CHUNK_CHARS", 300)
    monkeypatch.setattr(config, "ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS", 50)
    monkeypatch.setattr(config, "ADVANCED_DOCUMENT_TRANSFER_CHARS", 120)


@pytest.fixture
def extraction(monkeypatch: pytest.MonkeyPatch, small_chunks: None) -> None:
    """Every document goes to the evidence extraction."""
    monkeypatch.setattr(config, "PERSONA_SOURCE_MAX_CHARS", 10)
