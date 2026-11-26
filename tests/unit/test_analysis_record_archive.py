"""AnalysisRecordArchive: the analysis record in S3.

Deposited with ``pending=true`` (the lifecycle removes it), copied without
tags per saved persona, never readable 24 hours after the generation ended,
and never holding a presigned URL.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest

moto = pytest.importorskip("moto")

from src.managers.components.analysis_record_archive import (  # noqa: E402
    AnalysisRecordArchive,
    record_key,
)
from src.models.analysis import AnalysisRecord  # noqa: E402
from src.models.errors import CodedError, ErrorCode  # noqa: E402
from src.services.s3_service import S3Service  # noqa: E402
from tests.error_helpers import raises_code  # noqa: E402

pytestmark = pytest.mark.unit

BUCKET = "archive-test-bucket"
REGION = "us-east-1"
OWNER = "owner-1"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class _CallerError(CodedError):
    """Stands for the calling Manager's exception type."""


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield S3Service(BUCKET, REGION)


def _record(**changes) -> AnalysisRecord:
    base = AnalysisRecord.create_new(
        record_id="gen-1",
        owner_sub=OWNER,
        generation_id="gen-1",
        plan={"method": "cluster", "questions": ["どんな層があるか"]},
        code_blocks=[
            {"kind": "fixed", "code": "cluster_segments(k=3, seed=42)", "sha256": "ab"}
        ],
        seed=42,
        prepare_version="1",
        source_checksums=["crc=="],
        now=NOW,
    )
    return base.update(**changes) if changes else base


def _tags(s3: S3Service, key: str) -> dict[str, str]:
    return s3.get_object_tags(key)


class TestModel:
    def test_round_trip(self) -> None:
        record = _record()

        assert AnalysisRecord.from_dict(record.to_dict()) == record
        assert record.to_dict()["schema_version"] == 1


class TestDeposit:
    def test_writes_pending_json_under_the_owner(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)

        key = archive.deposit(_record())

        assert (
            key == record_key(OWNER, "gen-1") == "analysis-records/owner-1/gen-1.json"
        )
        assert _tags(s3, key) == {"pending": "true"}
        body = json.loads(s3.download_file(s3.path_of(key)))
        assert body["generation_id"] == "gen-1"

    def test_refuses_a_presigned_url(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)
        leaky = _record(
            plan={"note": "https://b.s3.amazonaws.com/k?X-Amz-Signature=abc"}
        )

        with raises_code(_CallerError, ErrorCode.GENERATION_OPERATION_FAILED):
            archive.deposit(leaky)
        assert not s3.exists(s3.path_of(record_key(OWNER, "gen-1")))

    def test_s3_failure_becomes_the_callers_error(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)
        archive.deposit(_record())

        # A second deposit at the same key is refused by S3 (IfNoneMatch).
        with raises_code(_CallerError, ErrorCode.GENERATION_OPERATION_FAILED):
            archive.deposit(_record())


class TestReadPending:
    def test_reads_within_24_hours(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)
        archive.deposit(_record())

        found = archive.read_pending(
            OWNER, "gen-1", finished_at=NOW, now=NOW + timedelta(hours=23)
        )

        assert found == _record()

    def test_past_24_hours_is_expired(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)
        archive.deposit(_record())

        with raises_code(_CallerError, ErrorCode.GENERATION_EXPIRED):
            archive.read_pending(
                OWNER, "gen-1", finished_at=NOW, now=NOW + timedelta(hours=25)
            )

    def test_missing_is_generation_not_found(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)

        with raises_code(_CallerError, ErrorCode.GENERATION_NOT_FOUND):
            archive.read_pending(OWNER, "gen-1", finished_at=NOW, now=NOW)


class TestCopies:
    def test_copy_per_persona_has_no_tags(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)
        archive.deposit(_record())

        archive.commit_copy(OWNER, "gen-1", "persona-1")

        key = record_key(OWNER, "persona-1")
        assert _tags(s3, key) == {}
        assert archive.read_committed(OWNER, "persona-1") == _record()

    def test_discard_copy_removes_only_that_copy(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)
        archive.deposit(_record())
        archive.commit_copy(OWNER, "gen-1", "persona-1")
        archive.commit_copy(OWNER, "gen-1", "persona-2")

        archive.discard_copy(OWNER, "persona-1")

        assert archive.read_committed(OWNER, "persona-1") is None
        assert archive.read_committed(OWNER, "persona-2") is not None

    def test_copy_of_a_missing_record_fails(self, s3: S3Service) -> None:
        archive = AnalysisRecordArchive(s3, error_type=_CallerError)

        with raises_code(_CallerError, ErrorCode.GENERATION_OPERATION_FAILED):
            archive.commit_copy(OWNER, "gen-x", "persona-1")
