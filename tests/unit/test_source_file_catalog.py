"""SourceFileCatalog: the owner of uploaded source files (BR2.1, BR3.3, BR6.2, BR6.4).

Someone else's file and a missing file get the same NOT_FOUND (NFR3.9).
Only the owner's ready table can be used for a generation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.config import config
from src.managers.components.source_file_catalog import SourceFileCatalog
from src.models.errors import CodedError, ErrorCode
from src.models.source_file import (
    SourceColumn,
    SourceFile,
    SourceFileStatus,
    SourceFormat,
    SourceMetadata,
)
from src.services.stores.source_file_store import (
    SourceFileStore,
    SourceFileStoreError,
    SourceFileUnavailable,
)
from tests.error_helpers import raises_code

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
OWNER = "owner-1"


class _CallerError(CodedError):
    """Stands for the calling Manager's exception type."""


def _file(status: SourceFileStatus = SourceFileStatus.READY, **changes) -> SourceFile:
    base = SourceFile.create_new(
        owner_sub=OWNER,
        filename="a.csv",
        size_bytes=100,
        part_count=1,
        now=changes.pop("created_at", NOW - timedelta(hours=1)),
    )
    metadata = None
    if status is SourceFileStatus.READY:
        metadata = SourceMetadata.create(
            format=SourceFormat.CSV,
            encoding="utf-8",
            delimiter=",",
            columns=[SourceColumn("id", "integer")],
            sample_rows=[["1"]],
            estimated_rows=1,
            inspected_bytes=100,
            inspected_at=NOW,
        )
    return base.update(status=status, metadata=metadata, **changes)


def _catalog() -> tuple[SourceFileCatalog, Mock]:
    store = Mock(spec=SourceFileStore)
    return SourceFileCatalog(store, error_type=_CallerError), store


class TestOwnership:
    def test_someone_elses_and_missing_files_are_the_same_not_found(self) -> None:
        catalog, store = _catalog()
        store.get_for_owner.return_value = None

        with raises_code(_CallerError, ErrorCode.UPLOAD_NOT_FOUND):
            catalog.get_owned(OWNER, "x", not_found_code=ErrorCode.UPLOAD_NOT_FOUND)
        store.get_for_owner.assert_called_once_with("x", OWNER)

    def test_get_owned_returns_the_owners_file(self) -> None:
        catalog, store = _catalog()
        found = _file()
        store.get_for_owner.return_value = found

        assert (
            catalog.get_owned(
                OWNER, found.source_file_id, not_found_code=ErrorCode.UPLOAD_NOT_FOUND
            )
            is found
        )


class TestReadyTable:
    def test_ready_table_is_returned(self) -> None:
        catalog, store = _catalog()
        found = _file()
        store.get_for_owner.return_value = found

        assert catalog.ready_table(OWNER, found.source_file_id) is found

    @pytest.mark.parametrize(
        "status", [SourceFileStatus.UPLOADING, SourceFileStatus.ERROR]
    )
    def test_not_ready_is_generation_source_not_found(
        self, status: SourceFileStatus
    ) -> None:
        catalog, store = _catalog()
        store.get_for_owner.return_value = _file(status)

        with raises_code(_CallerError, ErrorCode.GENERATION_SOURCE_NOT_FOUND):
            catalog.ready_table(OWNER, "x")

    def test_missing_is_generation_source_not_found(self) -> None:
        catalog, store = _catalog()
        store.get_for_owner.return_value = None

        with raises_code(_CallerError, ErrorCode.GENERATION_SOURCE_NOT_FOUND):
            catalog.ready_table(OWNER, "x")


class TestInspectionWrites:
    def test_begin_inspection_passes_the_windows(self) -> None:
        catalog, store = _catalog()
        store.begin_inspection.return_value = _file(SourceFileStatus.INSPECTING)

        catalog.begin_inspection(OWNER, "x", now=NOW)

        kwargs = store.begin_inspection.call_args.kwargs
        assert kwargs["stale_before"] == NOW - timedelta(
            seconds=config.ADVANCED_UPLOAD_INSPECT_RETRY_SECONDS
        )
        assert kwargs["created_after"] == NOW - timedelta(
            seconds=config.ADVANCED_UPLOAD_PENDING_MAX_SECONDS
        )

    def test_begin_inspection_condition_failure_is_upload_not_found(self) -> None:
        catalog, store = _catalog()
        store.begin_inspection.side_effect = SourceFileUnavailable("cond")

        with raises_code(_CallerError, ErrorCode.UPLOAD_NOT_FOUND):
            catalog.begin_inspection(OWNER, "x", now=NOW)

    def test_mark_ready_writes_metadata_and_checksum(self) -> None:
        catalog, store = _catalog()
        ready = _file()
        store.mark_ready.return_value = ready

        result = catalog.mark_ready("x", ready.metadata, "crc==", now=NOW)

        assert result is ready
        store.mark_ready.assert_called_once_with("x", ready.metadata, "crc==", now=NOW)

    def test_store_failure_becomes_the_callers_error(self) -> None:
        catalog, store = _catalog()
        store.put_new.side_effect = SourceFileStoreError("down")

        with raises_code(_CallerError, ErrorCode.DATABASE_OPERATION_FAILED):
            catalog.create(_file(SourceFileStatus.UPLOADING))


class TestFileLimit:
    def test_counts_live_files_only(self) -> None:
        catalog, store = _catalog()
        stale = NOW - timedelta(seconds=config.ADVANCED_UPLOAD_PENDING_MAX_SECONDS + 1)
        store.list_for_owner.return_value = [
            _file(SourceFileStatus.READY),
            _file(SourceFileStatus.INSPECTING),
            _file(SourceFileStatus.UPLOADING),
            _file(SourceFileStatus.UPLOADING, created_at=stale),  # abandoned
            _file(SourceFileStatus.ERROR),
        ]

        assert catalog.held_count(OWNER, now=NOW) == 3

    def test_the_eleventh_file_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_UPLOAD_MAX_FILES", 10)
        catalog, store = _catalog()
        store.list_for_owner.return_value = [_file() for _ in range(10)]

        with raises_code(_CallerError, ErrorCode.UPLOAD_TOO_MANY_FILES, max_files=10):
            catalog.ensure_room(OWNER, now=NOW)

    def test_room_left_passes(self) -> None:
        catalog, store = _catalog()
        store.list_for_owner.return_value = [_file() for _ in range(9)]

        catalog.ensure_room(OWNER, now=NOW)


class TestMarksAndCancel:
    def test_refresh_failure_is_upload_not_found(self) -> None:
        catalog, store = _catalog()
        store.record_url_refresh.side_effect = SourceFileUnavailable("cond")

        with raises_code(_CallerError, ErrorCode.UPLOAD_NOT_FOUND):
            catalog.record_url_refresh(OWNER, "x", now=NOW)
        assert (
            store.record_url_refresh.call_args.kwargs["max_refreshes"]
            == config.ADVANCED_UPLOAD_URL_REFRESH_MAX
        )

    def test_release_clears_the_mark_of_the_generation(self) -> None:
        catalog, store = _catalog()
        store.clear_in_use.return_value = True

        assert catalog.release("x", "gen-1", now=NOW) is True
        store.clear_in_use.assert_called_once_with("x", "gen-1", now=NOW)

    def test_release_failure_is_reported_not_raised(self) -> None:
        catalog, store = _catalog()
        store.clear_in_use.side_effect = SourceFileStoreError("down")

        assert catalog.release("x", "gen-1", now=NOW) is False

    def test_cancel_deletes_only_an_uploading_file(self) -> None:
        catalog, store = _catalog()
        store.delete_uploading.return_value = False

        assert catalog.cancel_upload(OWNER, "x") is False
        store.delete_uploading.assert_called_once_with("x", OWNER)
