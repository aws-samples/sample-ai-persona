"""Fixtures of the AdvancedPersonaGenerationManager tests (moto DynamoDB and S3)."""

from __future__ import annotations

import threading
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


class _SerializedClient:
    """A boto3 client whose calls run one at a time.

    The generation runs on its own thread while the test thread writes control
    requests and reads the record. moto keeps tables in plain dicts and is not
    thread-safe, so two calls at once can fail with "dictionary changed size
    during iteration". Real DynamoDB and S3 take concurrent calls; only the
    fake needs the lock.
    """

    def __init__(self, client: Any, lock: threading.RLock) -> None:
        self._client = client
        self._lock = lock

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attribute(*args, **kwargs)

        return call


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
        lock = threading.RLock()
        yield _Env(
            _SerializedClient(dynamo, lock),
            _SerializedClient(s3_client, lock),
            tmp_path,
        )


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
