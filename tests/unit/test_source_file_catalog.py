"""SourceFileCatalog: the owner of uploaded source files.

Someone else's file and a missing file get the same NOT_FOUND.
Only the owner's ready table can be used for a generation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.config import config
from src.managers.components.source_file_catalog import SourceFileCatalog
from src.models.analysis import AnalysisRecord
from src.models.errors import CodedError, ErrorCode
from src.models.source_file import (
    DocumentMetadata,
    SourceAvailabilityState,
    SourceColumn,
    SourceFile,
    SourceFileStatus,
    SourceFormat,
    SourceKind,
    SourceMetadata,
)
from src.services.s3_service import S3Service, S3ServiceError
from src.services.stores.source_file_store import (
    SourceFileStore,
    SourceFileStoreError,
    SourceFileUnavailable,
)
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture

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
    # The clock is fixed so the expiry checks do not depend on today.
    return SourceFileCatalog(store, error_type=_CallerError, clock=lambda: NOW), store


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


def _document(**changes) -> SourceFile:
    base = SourceFile.create_new(
        owner_sub=OWNER,
        filename=changes.pop("filename", "a.pdf"),
        size_bytes=100,
        part_count=1,
        now=NOW - timedelta(hours=1),
        kind=SourceKind.DOCUMENT,
    )
    status = changes.pop("status", SourceFileStatus.READY)
    metadata = (
        DocumentMetadata(
            format=SourceFormat.PDF,
            estimated_chars=1000,
            inspected_bytes=100,
            inspected_at=NOW,
        )
        if status is SourceFileStatus.READY
        else None
    )
    return base.update(status=status, metadata=metadata, **changes)


class TestReadySources:
    """Documents only (1 to 10), or one table with documents."""

    def _catalog_of(self, *files: SourceFile) -> SourceFileCatalog:
        catalog, store = _catalog()
        by_id = {f.source_file_id: f for f in files}
        store.get_for_owner.side_effect = lambda i, owner: (
            by_id.get(i) if owner == OWNER else None
        )
        return catalog

    def test_documents_only_are_used_in_order(self) -> None:
        docs = [_document() for _ in range(3)]
        catalog = self._catalog_of(*docs)

        found = catalog.ready_sources(OWNER, [d.source_file_id for d in docs])

        assert [f.source_file_id for f in found] == [d.source_file_id for d in docs]

    def test_one_table_with_documents_is_used(self) -> None:
        table, doc = _file(), _document()
        catalog = self._catalog_of(table, doc)

        found = catalog.ready_sources(OWNER, [doc.source_file_id, table.source_file_id])

        assert len(found) == 2

    def test_a_table_alone_is_used_as_before(self) -> None:
        table = _file()
        catalog = self._catalog_of(table)

        assert catalog.ready_sources(OWNER, [table.source_file_id]) == [table]

    # Up to three tables, 1GB in total (replaces the earlier "one table" limit)

    def test_three_tables_are_used_in_order(self) -> None:
        tables = [_file(), _file(), _file()]
        catalog = self._catalog_of(*tables, _document())

        found = catalog.ready_sources(OWNER, [t.source_file_id for t in tables])

        assert [f.source_file_id for f in found] == [t.source_file_id for t in tables]

    def test_four_tables_are_refused(self) -> None:
        tables = [_file() for _ in range(4)]
        catalog = self._catalog_of(*tables)

        with raises_code(
            _CallerError, ErrorCode.UPLOAD_SOURCE_INVALID, reason="tables", max=3
        ):
            catalog.ready_sources(OWNER, [t.source_file_id for t in tables])

    def test_tables_over_1gb_in_total_are_refused(self) -> None:
        tables = [_file(size_bytes=400_000_000) for _ in range(3)]
        catalog = self._catalog_of(*tables)

        with raises_code(
            _CallerError, ErrorCode.GENERATION_SOURCE_TOO_LARGE, limit="tables_total"
        ):
            catalog.ready_sources(OWNER, [t.source_file_id for t in tables])

    def test_tables_of_exactly_1gb_are_used(self) -> None:
        tables = [
            _file(size_bytes=500_000_000),
            _file(size_bytes=500_000_000),
        ]
        catalog = self._catalog_of(*tables, _document(size_bytes=90_000_000))

        assert (
            len(catalog.ready_sources(OWNER, [t.source_file_id for t in tables])) == 2
        )

    def test_nothing_chosen_is_refused(self) -> None:
        with raises_code(_CallerError, ErrorCode.UPLOAD_SOURCE_INVALID):
            self._catalog_of().ready_sources(OWNER, [])

    def test_more_than_ten_is_refused(self) -> None:
        docs = [_document() for _ in range(11)]
        catalog = self._catalog_of(*docs)

        with raises_code(_CallerError, ErrorCode.UPLOAD_SOURCE_INVALID):
            catalog.ready_sources(OWNER, [d.source_file_id for d in docs])

    @pytest.mark.parametrize(
        "status", [SourceFileStatus.UPLOADING, SourceFileStatus.INSPECTING]
    )
    def test_a_file_still_uploading_or_inspected_is_not_ready(self, status) -> None:
        # The user waits until it is ready
        good, waiting = _document(), _document(status=status)
        catalog = self._catalog_of(good, waiting)

        with raises_code(_CallerError, ErrorCode.GENERATION_SOURCE_NOT_READY):
            catalog.ready_sources(OWNER, [good.source_file_id, waiting.source_file_id])

    def test_someone_elses_waiting_file_is_still_not_found(self) -> None:
        waiting = _document(
            owner_sub="someone-else", status=SourceFileStatus.INSPECTING
        )
        catalog, store = _catalog()
        store.get_for_owner.side_effect = lambda i, owner: (
            waiting if waiting.owner_sub == owner else None
        )

        with raises_code(_CallerError, ErrorCode.GENERATION_SOURCE_NOT_FOUND):
            catalog.ready_sources(OWNER, [waiting.source_file_id])

    @pytest.mark.parametrize(
        "bad",
        [
            lambda: _document(owner_sub="someone-else"),
            lambda: _document(status=SourceFileStatus.ERROR),
        ],
    )
    def test_one_unusable_file_refuses_all(self, bad) -> None:
        good, other = _document(), bad()
        catalog, store = _catalog()
        by_id = {good.source_file_id: good, other.source_file_id: other}
        store.get_for_owner.side_effect = lambda i, owner: (
            by_id[i] if by_id[i].owner_sub == owner else None
        )

        with raises_code(_CallerError, ErrorCode.GENERATION_SOURCE_NOT_FOUND):
            catalog.ready_sources(OWNER, [good.source_file_id, other.source_file_id])


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
        # The end does not stamp the last use any more.
        store.clear_in_use.assert_called_once_with("x", "gen-1")

    def test_release_failure_is_reported_not_raised(self) -> None:
        catalog, store = _catalog()
        store.clear_in_use.side_effect = SourceFileStoreError("down")

        assert catalog.release("x", "gen-1", now=NOW) is False

    def test_cancel_deletes_only_an_uploading_file(self) -> None:
        catalog, store = _catalog()
        store.delete_uploading.return_value = False

        assert catalog.cancel_upload(OWNER, "x") is False
        store.delete_uploading.assert_called_once_with("x", OWNER)


# --- numbers, expiry, listing, deletion, repair of old records -------------------

DAYS = config.ADVANCED_SOURCE_RETENTION_DAYS


def _catalog_u2(now: datetime = NOW) -> tuple[SourceFileCatalog, Mock, Mock]:
    store = Mock(spec=SourceFileStore)
    s3 = Mock(spec=S3Service)
    catalog = SourceFileCatalog(
        store, s3=s3, error_type=_CallerError, clock=lambda: now
    )
    return catalog, store, s3


def _live(status: SourceFileStatus = SourceFileStatus.READY, **changes) -> SourceFile:
    changes.setdefault("expires_at", NOW + timedelta(days=3))
    return _file(status, **changes)


class TestNumberAndExpiryRules:
    def test_next_display_seq_is_the_highest_plus_one(self) -> None:
        catalog, store, _ = _catalog_u2()
        store.max_display_seq.return_value = 2

        assert catalog.next_display_seq(OWNER, "a.csv") == 3
        store.max_display_seq.assert_called_once_with(OWNER, "a.csv", now=NOW)

    def test_first_of_a_name_is_1(self) -> None:
        catalog, store, _ = _catalog_u2()
        store.max_display_seq.return_value = 0

        assert catalog.next_display_seq(OWNER, "a.csv") == 1

    def test_new_expiry_is_now_plus_the_retention(self) -> None:
        catalog, *_ = _catalog_u2()

        assert catalog.new_expiry() == NOW + timedelta(days=DAYS)

    def test_get_owned_hides_an_expired_file(self) -> None:
        catalog, store, _ = _catalog_u2()
        store.get_for_owner.return_value = _file(expires_at=NOW)

        with raises_code(_CallerError, ErrorCode.UPLOAD_NOT_FOUND):
            catalog.get_owned(OWNER, "x", not_found_code=ErrorCode.UPLOAD_NOT_FOUND)

    def test_get_owned_judges_an_u1_record_by_its_computed_expiry(self) -> None:
        catalog, store, _ = _catalog_u2()
        old = _file(created_at=NOW - timedelta(days=DAYS, seconds=1))
        recent = _file(created_at=NOW - timedelta(days=DAYS - 1))

        store.get_for_owner.return_value = old
        with raises_code(_CallerError, ErrorCode.UPLOAD_NOT_FOUND):
            catalog.get_owned(OWNER, "x", not_found_code=ErrorCode.UPLOAD_NOT_FOUND)
        store.get_for_owner.return_value = recent
        assert (
            catalog.get_owned(OWNER, "x", not_found_code=ErrorCode.UPLOAD_NOT_FOUND)
            is recent
        )

    def test_get_owned_still_shows_a_deleting_row(self) -> None:
        catalog, store, _ = _catalog_u2()
        deleting = _live(SourceFileStatus.DELETING)
        store.get_for_owner.return_value = deleting

        assert (
            catalog.get_owned(OWNER, "x", not_found_code=ErrorCode.UPLOAD_NOT_FOUND)
            is deleting
        )

    @pytest.mark.parametrize(
        "found",
        [
            _file(SourceFileStatus.DELETING, expires_at=NOW + timedelta(days=1)),
            _file(expires_at=NOW - timedelta(seconds=1)),
        ],
    )
    def test_ready_table_refuses_deleting_and_expired(self, found) -> None:
        catalog, store, _ = _catalog_u2()
        store.get_for_owner.return_value = found

        with raises_code(_CallerError, ErrorCode.GENERATION_SOURCE_NOT_FOUND):
            catalog.ready_table(OWNER, "x")

    def test_expired_files_are_not_counted(self) -> None:
        catalog, store, _ = _catalog_u2()
        store.list_for_owner.return_value = [
            _live(),
            _file(expires_at=NOW - timedelta(seconds=1)),
            _live(SourceFileStatus.ERROR),
        ]

        assert catalog.held_count(OWNER, now=NOW) == 1


class TestList:
    def test_lists_live_and_deleting_files_newest_first(self) -> None:
        catalog, store, _ = _catalog_u2()
        older = _live(created_at=NOW - timedelta(hours=3))
        newer = _live(SourceFileStatus.ERROR, created_at=NOW - timedelta(hours=1))
        deleting = _live(SourceFileStatus.DELETING, created_at=NOW - timedelta(hours=2))
        expired = _file(expires_at=NOW - timedelta(seconds=1))
        abandoned = _live(
            SourceFileStatus.UPLOADING,
            created_at=NOW
            - timedelta(seconds=config.ADVANCED_UPLOAD_PENDING_MAX_SECONDS + 1),
        )
        store.list_for_owner.return_value = [older, expired, abandoned, deleting, newer]

        listed = catalog.list_owned(OWNER)

        assert listed == [newer, deleting, older]
        store.list_for_owner.assert_called_once_with(OWNER)


class TestDeleteRules:
    def test_delete_goes_deleting_then_every_version_then_the_record(self) -> None:
        catalog, store, s3 = _catalog_u2()
        found = _live()
        store.get_for_owner.return_value = found
        store.begin_delete.return_value = found.update(status=SourceFileStatus.DELETING)
        s3.delete_all_versions.return_value = 2
        store.delete_record.return_value = True

        assert catalog.delete_owned(OWNER, found.source_file_id) == 2

        kwargs = store.begin_delete.call_args.kwargs
        assert kwargs["now"] == NOW
        assert kwargs["stale_before"] == NOW - timedelta(
            seconds=config.ADVANCED_UPLOAD_INSPECT_RETRY_SECONDS
        )
        s3.delete_all_versions.assert_called_once_with(found.s3_key)
        store.delete_record.assert_called_once_with(found.source_file_id, OWNER)

    def test_an_in_use_file_is_source_file_in_use(self) -> None:
        catalog, store, s3 = _catalog_u2()
        busy = _live(in_use_by="g", in_use_until=NOW + timedelta(hours=1))
        store.get_for_owner.return_value = busy
        store.begin_delete.side_effect = SourceFileUnavailable("cond")

        with raises_code(_CallerError, ErrorCode.SOURCE_FILE_IN_USE):
            catalog.delete_owned(OWNER, busy.source_file_id)
        s3.delete_all_versions.assert_not_called()

    def test_other_refusals_are_upload_not_found(self) -> None:
        catalog, store, s3 = _catalog_u2()
        uploading = _live(SourceFileStatus.UPLOADING)
        store.get_for_owner.return_value = uploading
        store.begin_delete.side_effect = SourceFileUnavailable("cond")

        with raises_code(_CallerError, ErrorCode.UPLOAD_NOT_FOUND):
            catalog.delete_owned(OWNER, uploading.source_file_id)
        s3.delete_all_versions.assert_not_called()

    def test_someone_elses_or_missing_is_upload_not_found_without_a_write(
        self,
    ) -> None:
        catalog, store, _ = _catalog_u2()
        store.get_for_owner.return_value = None

        with raises_code(_CallerError, ErrorCode.UPLOAD_NOT_FOUND):
            catalog.delete_owned(OWNER, "x")
        store.begin_delete.assert_not_called()

    def test_an_expired_u1_record_is_checked_before_the_write(self) -> None:
        catalog, store, _ = _catalog_u2()
        store.get_for_owner.return_value = _file(
            created_at=NOW - timedelta(days=DAYS + 1)
        )

        with raises_code(_CallerError, ErrorCode.UPLOAD_NOT_FOUND):
            catalog.delete_owned(OWNER, "x")
        store.begin_delete.assert_not_called()

    def test_an_s3_failure_leaves_the_record_deleting(self) -> None:
        catalog, store, s3 = _catalog_u2()
        found = _live()
        store.get_for_owner.return_value = found
        store.begin_delete.return_value = found.update(status=SourceFileStatus.DELETING)
        s3.delete_all_versions.side_effect = S3ServiceError(
            "down", code=ErrorCode.S3_OPERATION_FAILED
        )

        with raises_code(_CallerError, ErrorCode.S3_OPERATION_FAILED):
            catalog.delete_owned(OWNER, found.source_file_id)
        store.delete_record.assert_not_called()

    def test_a_record_already_gone_counts_as_deleted(self) -> None:
        catalog, store, s3 = _catalog_u2()
        found = _live(SourceFileStatus.DELETING)
        store.get_for_owner.return_value = found
        store.begin_delete.return_value = found
        s3.delete_all_versions.return_value = 0
        store.delete_record.return_value = False

        assert catalog.delete_owned(OWNER, found.source_file_id) == 0


class TestReconcileLegacy:
    def _legacy(self, age_days: float, **changes) -> SourceFile:
        return _file(created_at=NOW - timedelta(days=age_days), **changes)

    def test_fills_the_expiry_of_a_live_u1_record_without_s3(self) -> None:
        catalog, store, s3 = _catalog_u2()
        legacy = self._legacy(2)

        outcome = catalog.reconcile_legacy(OWNER, [legacy, _live()])

        store.fill_expires_at.assert_called_once_with(
            legacy.source_file_id, OWNER, legacy.created_at + timedelta(days=DAYS)
        )
        s3.delete_all_versions.assert_not_called()
        assert (outcome.filled, outcome.deleted, outcome.remaining) == (1, 0, 0)

    def test_deletes_an_expired_u1_record_that_is_not_in_use(self) -> None:
        catalog, store, s3 = _catalog_u2()
        legacy = self._legacy(DAYS + 1)
        store.begin_delete.return_value = legacy.update(
            status=SourceFileStatus.DELETING
        )
        store.delete_record.return_value = True
        s3.delete_all_versions.return_value = 1

        outcome = catalog.reconcile_legacy(OWNER, [legacy])

        s3.delete_all_versions.assert_called_once_with(legacy.s3_key)
        store.delete_record.assert_called_once_with(legacy.source_file_id, OWNER)
        assert outcome.deleted == 1

    def test_an_expired_record_in_use_only_gets_its_expiry(self) -> None:
        catalog, store, s3 = _catalog_u2()
        legacy = self._legacy(
            DAYS + 1, in_use_by="g", in_use_until=NOW + timedelta(hours=1)
        )

        outcome = catalog.reconcile_legacy(OWNER, [legacy])

        store.begin_delete.assert_not_called()
        store.fill_expires_at.assert_called_once()
        assert (outcome.filled, outcome.deleted) == (1, 0)

    def test_stops_after_the_delete_limit(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "ADVANCED_SOURCE_RECONCILE_MAX_DELETES", 2)
        catalog, store, s3 = _catalog_u2()
        legacy = [self._legacy(DAYS + 1) for _ in range(4)]
        store.begin_delete.side_effect = lambda sid, *a, **k: next(
            f.update(status=SourceFileStatus.DELETING)
            for f in legacy
            if f.source_file_id == sid
        )
        store.delete_record.return_value = True
        s3.delete_all_versions.return_value = 1

        outcome = catalog.reconcile_legacy(OWNER, legacy)

        assert (outcome.deleted, outcome.remaining) == (2, 2)
        assert s3.delete_all_versions.call_count == 2

    def test_stops_when_the_time_is_up(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "ADVANCED_SOURCE_RECONCILE_MAX_SECONDS", 0)
        catalog, store, s3 = _catalog_u2()

        outcome = catalog.reconcile_legacy(
            OWNER, [self._legacy(1), self._legacy(DAYS + 1)]
        )

        store.fill_expires_at.assert_not_called()
        assert outcome.remaining == 2

    def test_never_touches_someone_elses_record(self) -> None:
        catalog, store, s3 = _catalog_u2()
        theirs = SourceFile.create_new(
            owner_sub="owner-2",
            filename="b.csv",
            size_bytes=1,
            part_count=1,
            now=NOW - timedelta(days=DAYS + 1),
        ).update(status=SourceFileStatus.READY)

        outcome = catalog.reconcile_legacy(OWNER, [theirs])

        store.fill_expires_at.assert_not_called()
        store.begin_delete.assert_not_called()
        assert (outcome.filled, outcome.deleted, outcome.remaining) == (0, 0, 0)

    def test_failures_are_logged_not_raised(self, capsys) -> None:
        catalog, store, s3 = _catalog_u2()
        logs = LogCapture(capsys)
        store.fill_expires_at.side_effect = SourceFileStoreError("down")
        legacy = self._legacy(DAYS + 1)
        store.begin_delete.return_value = legacy.update(
            status=SourceFileStatus.DELETING
        )
        s3.delete_all_versions.side_effect = S3ServiceError(
            "down", code=ErrorCode.S3_OPERATION_FAILED
        )

        outcome = catalog.reconcile_legacy(OWNER, [self._legacy(1), legacy])

        assert outcome.status == "failed"
        (event,) = logs.events("advanced_generation.sources_reconciled")
        assert event["status"] == "failed"
        assert event["error_code"] == ErrorCode.S3_OPERATION_FAILED.value
        assert set(event) <= {
            "event",
            "ts",
            "filled",
            "deleted",
            "remaining",
            "status",
            "error_code",
        }

    def test_nothing_to_do_writes_no_log(self, capsys) -> None:
        catalog, *_ = _catalog_u2()
        logs = LogCapture(capsys)

        catalog.reconcile_legacy(OWNER, [_live()])

        assert logs.events("advanced_generation.sources_reconciled") == []


class TestAvailability:
    def _record(self, ids=(), names=(), checksums=("crc==",)) -> AnalysisRecord:
        return AnalysisRecord.create_new(
            record_id="p-1",
            owner_sub=OWNER,
            generation_id="g-1",
            plan={},
            code_blocks=[],
            seed=42,
            prepare_version="v1",
            source_checksums=list(checksums),
            now=NOW,
            source_file_ids=list(ids),
            source_display_names=list(names),
        )

    def test_a_file_still_there_with_the_same_checksum_is_available(self) -> None:
        catalog, store, _ = _catalog_u2()
        store.get_for_owner.return_value = _live(checksum="crc==")

        (found,) = catalog.availability(OWNER, self._record(["sf-1"], ["a.csv (2)"]))

        assert found.state is SourceAvailabilityState.AVAILABLE
        assert found.display_name == "a.csv (2)"
        store.get_for_owner.assert_called_once_with("sf-1", OWNER)

    @pytest.mark.parametrize(
        "found",
        [
            None,
            _file(expires_at=NOW - timedelta(seconds=1), checksum="crc=="),
            _file(expires_at=NOW + timedelta(days=1), checksum="other=="),
            _file(
                SourceFileStatus.DELETING,
                expires_at=NOW + timedelta(days=1),
                checksum="crc==",
            ),
        ],
    )
    def test_missing_expired_changed_or_deleting_is_deleted(self, found) -> None:
        catalog, store, _ = _catalog_u2()
        store.get_for_owner.return_value = found

        (judged,) = catalog.availability(OWNER, self._record(["sf-1"], ["a.csv"]))

        assert judged.state is SourceAvailabilityState.DELETED

    def test_a_record_without_ids_is_unknown(self) -> None:
        catalog, store, _ = _catalog_u2()

        (judged,) = catalog.availability(OWNER, self._record())

        assert judged.state is SourceAvailabilityState.UNKNOWN
        store.get_for_owner.assert_not_called()

    def test_a_read_failure_is_unknown_not_raised(self) -> None:
        catalog, store, _ = _catalog_u2()
        store.get_for_owner.side_effect = SourceFileStoreError("down")

        (judged,) = catalog.availability(OWNER, self._record(["sf-1"], ["a.csv"]))

        assert judged.state is SourceAvailabilityState.UNKNOWN
