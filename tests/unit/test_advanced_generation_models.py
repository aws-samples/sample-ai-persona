"""Models of advanced generation (beta) U1 (entities.md, contracts C4, C5, C8).

Each model follows the project pattern: frozen dataclass, ``create_new()`` /
``update()`` return new instances, ``to_dict()`` leaves out ``None`` values and
``from_dict()`` reads it back.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.models.advanced_generation import (
    PERSONA_COUNT_MAX,
    PURPOSE_MAX_CHARS,
    AdvancedGeneration,
    EventType,
    GenerationEvent,
    GenerationStatus,
    OwnerLock,
    ResultPart,
    ResultPartName,
)
from src.models.analysis import (
    U1_ANALYSIS_TOOLS,
    AnalysisMethod,
    AnalysisPlan,
    AnalysisRecord,
    AnalysisRun,
    AnalysisTool,
    AnalysisToolPolicy,
    Evidence,
    EvidenceKind,
    PrepareReport,
    ReportColumn,
    Segment,
)
from src.models.errors import ErrorCode, ErrorKind
from src.models.source_file import (
    SAMPLE_CELL_MAX_CHARS,
    SAMPLE_COLUMNS_MAX,
    SAMPLE_ROWS_MAX,
    PartUrl,
    SourceAvailability,
    SourceAvailabilityState,
    SourceColumn,
    SourceEncoding,
    SourceFile,
    SourceFileListItem,
    SourceFileStatus,
    SourceFormat,
    SourceKind,
    SourceMetadata,
    UploadSession,
    part_count_for,
    source_key,
)

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def _metadata(**overrides) -> SourceMetadata:
    values = dict(
        format=SourceFormat.CSV,
        encoding=SourceEncoding.UTF8,
        delimiter=",",
        columns=[SourceColumn("age", "integer"), SourceColumn("city", "string")],
        sample_rows=[["31", "東京"], ["45", "大阪"]],
        estimated_rows=1200,
        inspected_bytes=4 * 1024 * 1024,
        inspected_at=NOW,
    )
    values.update(overrides)
    return SourceMetadata.create(**values)


def _source_file(**overrides) -> SourceFile:
    values = dict(
        owner_sub="user-1",
        filename="sales.csv",
        size_bytes=40_000_000,
        part_count=3,
        now=NOW,
    )
    values.update(overrides)
    return SourceFile.create_new(**values)


def _plan(**overrides) -> AnalysisPlan:
    values = dict(
        interpreted_purpose="首都圏の 30 代の購買層",
        questions=("どの層が多いか",),
        method=AnalysisMethod.FILTER,
        rationale="対象が指定されている",
        budget={"max_tool_calls": 30},
        split_axis="city",
    )
    values.update(overrides)
    return AnalysisPlan(**values)


def _segment(**overrides) -> Segment:
    values = dict(
        id="seg-1",
        label="東京の 30 代",
        definition={"kind": "filter", "spec": {"city": "東京"}},
        size=120,
        share=0.1,
        features=({"name": "age", "value": 33, "baseline": 41},),
        evidence_ids=("ev-1",),
    )
    values.update(overrides)
    return Segment(**values)


def _evidence(**overrides) -> Evidence:
    values = dict(
        id="ev-1",
        source_id="sf-1",
        location="tool-call-1",
        kind=EvidenceKind.ATTRIBUTE,
        claim="東京が最も多い",
        statistic={"value": 120, "population": 1200},
    )
    values.update(overrides)
    return Evidence(**values)


class TestSourceFile:
    def test_create_new_starts_uploading_under_the_owners_key(self):
        sf = _source_file()

        assert sf.status is SourceFileStatus.UPLOADING
        assert sf.kind is SourceKind.TABLE
        assert sf.s3_key == f"sources/user-1/{sf.source_file_id}"
        assert sf.s3_key == source_key("user-1", sf.source_file_id)
        assert sf.created_at == NOW

    def test_filename_is_cut_to_255_and_loses_control_characters(self):
        sf = _source_file(filename="a\x00b\n\t" + "x" * 300 + ".csv")

        assert "\x00" not in sf.filename and "\n" not in sf.filename
        assert sf.filename.startswith("ab")
        assert len(sf.filename) == 255

    def test_update_returns_a_new_instance(self):
        sf = _source_file()
        ready = sf.update(status=SourceFileStatus.READY, metadata=_metadata())

        assert ready is not sf
        assert sf.status is SourceFileStatus.UPLOADING
        assert ready.status is SourceFileStatus.READY
        with pytest.raises(FrozenInstanceError):
            sf.status = SourceFileStatus.READY  # type: ignore[misc]

    def test_to_dict_leaves_out_none(self):
        data = _source_file().to_dict()

        assert data["schema_version"] == 1
        assert data["status"] == "uploading"
        for name in ("source_metadata", "checksum", "error_code", "in_use_by"):
            assert name not in data
        assert None not in data.values()

    def test_round_trip_with_every_field(self):
        sf = _source_file(multipart_upload_id="mpu-1").update(
            status=SourceFileStatus.READY,
            metadata=_metadata(),
            checksum="crc64==",
            in_use_by="gen-1",
            in_use_until=NOW + timedelta(hours=1),
            last_used_at=NOW,
            url_refresh_count=2,
        )

        assert SourceFile.from_dict(sf.to_dict()) == sf

    def test_times_are_comparable_utc_text(self):
        jst = timezone(timedelta(hours=9))
        sf = _source_file(now=datetime(2026, 11, 19, 19, 0, tzinfo=jst))

        assert sf.to_dict()["created_at"] == "2026-11-19T10:00:00.000000+00:00"

    def test_unknown_status_is_rejected(self):
        data = _source_file().to_dict()
        data["status"] = "lost"

        with pytest.raises(ValueError):
            SourceFile.from_dict(data)

    def test_size_must_be_positive(self):
        with pytest.raises(ValueError):
            _source_file(size_bytes=0)

    def test_status_values(self):
        assert [s.value for s in SourceFileStatus] == [
            "uploading",
            "inspecting",
            "ready",
            "error",
            "deleting",
        ]


class TestSourceMetadata:
    def test_sample_rows_are_cut_to_5_rows_50_columns_200_characters(self):
        rows = [[f"{r}-" + "v" * 300 for _ in range(60)] for r in range(8)]
        cols = [SourceColumn(f"c{i}", "string") for i in range(60)]

        md = _metadata(columns=cols, sample_rows=rows)

        assert len(md.sample_rows) == SAMPLE_ROWS_MAX == 5
        assert all(len(row) == SAMPLE_COLUMNS_MAX == 50 for row in md.sample_rows)
        assert all(
            len(cell) == SAMPLE_CELL_MAX_CHARS == 200
            for row in md.sample_rows
            for cell in row
        )
        # The columns themselves are kept (only the display sample is cut).
        assert len(md.columns) == 60

    def test_encodings_include_undetermined(self):
        assert [e.value for e in SourceEncoding] == [
            "utf-8",
            "utf-8-sig",
            "cp932",
            "euc-jp",
            "undetermined",
        ]

    def test_delimiter_must_match_the_format(self):
        with pytest.raises(ValueError):
            _metadata(format=SourceFormat.TSV, delimiter=",")

    def test_round_trip(self):
        md = _metadata(format=SourceFormat.TSV, delimiter="\t")

        assert SourceMetadata.from_dict(md.to_dict()) == md
        assert md.to_dict()["columns"][0] == {"name": "age", "inferred_type": "integer"}


class TestUploadSession:
    def test_part_count_is_the_ceiling_of_size_over_part_size(self):
        part = 16 * 1024 * 1024
        assert part_count_for(1, part) == 1
        assert part_count_for(part, part) == 1
        assert part_count_for(part + 1, part) == 2
        assert part_count_for(1_000_000_000, part) == 60

    def test_part_count_rejects_bad_sizes(self):
        with pytest.raises(ValueError):
            part_count_for(0, 16)

    def test_to_dict_lists_the_part_urls(self):
        session = UploadSession(
            source_file_id="sf-1",
            part_size_bytes=16,
            part_count=2,
            part_urls=(PartUrl(1, "https://a/1"), PartUrl(2, "https://a/2")),
            urls_expire_at=NOW + timedelta(hours=1),
        )

        data = session.to_dict()

        assert data["part_urls"] == [
            {"part_number": 1, "url": "https://a/1"},
            {"part_number": 2, "url": "https://a/2"},
        ]
        assert datetime.fromisoformat(data["expires_at"]) == NOW + timedelta(hours=1)


class TestAdvancedGeneration:
    def _new(self, **overrides) -> AdvancedGeneration:
        values = dict(
            owner_sub="user-1",
            source_file_ids=["sf-1"],
            purpose="首都圏の 30 代",
            persona_count=3,
            now=NOW,
            ttl_seconds=48 * 3600,
        )
        values.update(overrides)
        return AdvancedGeneration.create_new(**values)

    def test_create_new_starts_preparing_with_a_48h_ttl(self):
        gen = self._new()

        assert gen.status is GenerationStatus.PREPARING
        assert gen.expires_at == int(NOW.timestamp()) + 48 * 3600
        assert gen.source_file_ids == ("sf-1",)

    @pytest.mark.parametrize("count", [0, PERSONA_COUNT_MAX + 1])
    def test_persona_count_is_1_to_10(self, count):
        with pytest.raises(ValueError):
            self._new(persona_count=count)

    def test_purpose_is_at_most_2000_characters_and_may_be_empty(self):
        assert self._new(purpose="").purpose == ""
        assert len(self._new(purpose="x" * PURPOSE_MAX_CHARS).purpose) == 2000
        with pytest.raises(ValueError):
            self._new(purpose="x" * (PURPOSE_MAX_CHARS + 1))

    @pytest.mark.parametrize("ids", [[], [f"sf-{i}" for i in range(11)]])
    def test_source_files_are_1_to_10(self, ids):
        with pytest.raises(ValueError):
            self._new(source_file_ids=ids)

    def test_update_returns_a_new_instance(self):
        gen = self._new()
        planned = gen.update(status=GenerationStatus.PLANNING, plan=_plan())

        assert gen.status is GenerationStatus.PREPARING
        assert planned.status is GenerationStatus.PLANNING
        assert planned.plan == _plan()

    def test_terminal_states(self):
        assert {s for s in GenerationStatus if s.is_terminal} == {
            GenerationStatus.DONE,
            GenerationStatus.FAILED,
            GenerationStatus.CANCELLED,
        }

    def test_to_dict_leaves_out_none(self):
        data = self._new().to_dict()

        assert data["schema_version"] == 1
        assert data["status"] == "preparing"
        for name in ("plan", "analysis_run", "error_code", "finished_at"):
            assert name not in data
        assert None not in data.values()

    def test_round_trip_with_nested_results(self):
        run = AnalysisRun(
            method="filter",
            plan_ref="plan-1",
            code_sha256=("abc",),
            params={"k": 3},
            source_checksums=("crc",),
            prepare_version="1",
            tool_call_ids=("tc-1",),
        )
        gen = self._new().update(
            status=GenerationStatus.DONE,
            plan=_plan(),
            analysis_run=run,
            part_counts={"segments": 1},
            analysis_record_id="rec-1",
            candidate_ids=("c-1", "c-2"),
            heartbeat_at=NOW,
            error_code=ErrorCode.GENERATION_TIMEOUT.value,
            error_context={"limit_minutes": 60},
            finished_at=NOW,
        )

        assert AdvancedGeneration.from_dict(gen.to_dict()) == gen


class TestGenerationEvent:
    def test_summary_is_cut_to_500_characters(self):
        event = GenerationEvent.create_new(
            generation_id="gen-1",
            sequence=1,
            event_type=EventType.PROGRESS,
            summary="x" * 900,
            now=NOW,
        )
        assert len(event.summary or "") == 500

    def test_sequence_starts_at_1(self):
        with pytest.raises(ValueError):
            GenerationEvent.create_new(
                generation_id="g", sequence=0, event_type=EventType.STAGE, now=NOW
            )

    def test_round_trip_without_summary(self):
        event = GenerationEvent.create_new(
            generation_id="g", sequence=3, event_type=EventType.STAGE, now=NOW
        )
        data = event.to_dict()

        assert "summary" not in data
        assert GenerationEvent.from_dict(data) == event

    def test_event_types(self):
        assert [t.value for t in EventType] == [
            "stage",
            "progress",
            "plan",
            "result",
            "error",
        ]


class TestOwnerLockAndResultPart:
    def test_lock_is_held_until_lock_until(self):
        lock = OwnerLock("user-1", "gen-1", NOW + timedelta(minutes=75))

        assert lock.is_held(NOW)
        assert not lock.is_held(NOW + timedelta(minutes=75))
        assert OwnerLock.from_dict(lock.to_dict()) == lock

    def test_result_part_round_trip(self):
        part = ResultPart(ResultPartName.SEGMENTS, 0, ({"id": "seg-1"},))

        assert ResultPart.from_dict(part.to_dict()) == part

    def test_result_part_index_is_not_negative(self):
        with pytest.raises(ValueError):
            ResultPart(ResultPartName.EVIDENCE, -1, ())


class TestAnalysisModels:
    def test_prepare_report_round_trip(self):
        report = PrepareReport(
            source_id="sf-1",
            rows_read=1000,
            rows_excluded=2,
            columns=(ReportColumn("age", "BIGINT"),),
            encoding=SourceEncoding.CP932,
            encoding_retried=True,
            prepare_version="1",
            excluded_reasons={"column_mismatch": 2, "examples": [3, 9]},
        )

        assert PrepareReport.from_dict(report.to_dict()) == report

    def test_prepare_report_never_keeps_undetermined(self):
        with pytest.raises(ValueError):
            PrepareReport(
                source_id="sf-1",
                rows_read=1,
                rows_excluded=0,
                columns=(),
                encoding=SourceEncoding.UNDETERMINED,
                prepare_version="1",
            )

    def test_plan_round_trip_and_optional_fields(self):
        plan = _plan(split_axis=None)
        data = plan.to_dict()

        assert "split_axis" not in data and "deviation_reason" not in data
        assert AnalysisPlan.from_dict(data) == plan

    def test_plan_needs_a_question_and_a_known_method(self):
        with pytest.raises(ValueError):
            _plan(questions=())
        with pytest.raises(ValueError):
            AnalysisPlan.from_dict({**_plan().to_dict(), "method": "time_series"})

    def test_segment_round_trip(self):
        seg = _segment(overlaps=True)

        assert Segment.from_dict(seg.to_dict()) == seg
        assert _segment().overlaps is False

    @pytest.mark.parametrize(
        "overrides",
        [{"size": 0}, {"share": 1.5}, {"share": -0.1}, {"evidence_ids": ()}],
    )
    def test_segment_bounds(self, overrides):
        with pytest.raises(ValueError):
            _segment(**overrides)

    def test_evidence_round_trip(self):
        ev = _evidence()

        assert Evidence.from_dict(ev.to_dict()) == ev
        assert "quote" not in ev.to_dict()

    def test_run_seed_defaults_to_42(self):
        run = AnalysisRun(
            method="cluster",
            plan_ref="p",
            code_sha256=(),
            params={},
            source_checksums=(),
            prepare_version="1",
            tool_call_ids=(),
        )

        assert run.seed == 42
        assert AnalysisRun.from_dict(run.to_dict()) == run

    def test_u1_tools_are_the_three_fixed_tools(self):
        assert U1_ANALYSIS_TOOLS == (
            AnalysisTool.PROFILE_DATASET,
            AnalysisTool.ANALYZE_DATASET,
            AnalysisTool.CLUSTER_SEGMENTS,
        )

    def test_tool_policy_round_trip(self):
        policy = AnalysisToolPolicy(
            tools=U1_ANALYSIS_TOOLS, limits={"max_calls": 30, "max_output_chars": 20000}
        )

        assert AnalysisToolPolicy.from_dict(policy.to_dict()) == policy

    def test_tool_policy_rejects_unknown_tools(self):
        with pytest.raises(ValueError):
            AnalysisToolPolicy.from_dict({"tools": ["run_code"], "limits": {}})


class TestAdvancedGenerationErrorCodes:
    """functional-spec.md §5: names and kinds of the U1 error codes."""

    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            (ErrorCode.ADVANCED_GENERATION_UNAVAILABLE, ErrorKind.CONFIG),
            (ErrorCode.ADVANCED_GENERATION_BUSY, ErrorKind.TRANSIENT),
            (ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING, ErrorKind.TRANSIENT),
            (ErrorCode.UPLOAD_SOURCE_INVALID, ErrorKind.VALIDATION),
            (ErrorCode.UPLOAD_TOO_LARGE, ErrorKind.CAPACITY),
            (ErrorCode.UPLOAD_TOO_MANY_FILES, ErrorKind.CAPACITY),
            (ErrorCode.UPLOAD_NOT_FOUND, ErrorKind.NOT_FOUND),
            (ErrorCode.GENERATION_SOURCE_NOT_FOUND, ErrorKind.NOT_FOUND),
            (ErrorCode.GENERATION_NOT_FOUND, ErrorKind.NOT_FOUND),
            (ErrorCode.GENERATION_EXPIRED, ErrorKind.NOT_FOUND),
            (ErrorCode.GENERATION_SOURCE_UNREADABLE, ErrorKind.VALIDATION),
            (ErrorCode.GENERATION_CAPACITY, ErrorKind.CAPACITY),
            (ErrorCode.GENERATION_INSUFFICIENT_DATA, ErrorKind.VALIDATION),
            (ErrorCode.GENERATION_ANALYSIS_FAILED, ErrorKind.VALIDATION),
            (ErrorCode.GENERATION_VERIFICATION_FAILED, ErrorKind.VALIDATION),
            (ErrorCode.GENERATION_RESULT_TOO_LARGE, ErrorKind.CAPACITY),
            (ErrorCode.GENERATION_TIMEOUT, ErrorKind.TRANSIENT),
            (ErrorCode.GENERATION_SANDBOX_UNAVAILABLE, ErrorKind.TRANSIENT),
        ],
    )
    def test_kind(self, code, kind):
        assert code.kind is kind


# --- U2: 同じ名前の番号・期限・元のファイルの判定（BR11.3、BR13.2、BR14.3、BR12.1） ---


class TestSourceFileU2:
    def test_display_seq_defaults_to_1(self):
        assert _source_file().display_seq == 1

    def test_display_seq_is_1_when_an_u1_record_has_none(self):
        data = _source_file().to_dict()
        data.pop("display_seq", None)

        assert SourceFile.from_dict(data).display_seq == 1

    def test_display_seq_must_be_positive(self):
        with pytest.raises(ValueError):
            _source_file(display_seq=0)

    @pytest.mark.parametrize(
        ("seq", "name"),
        [(1, "sales.csv"), (2, "sales.csv (2)"), (11, "sales.csv (11)")],
    )
    def test_display_name(self, seq, name):
        assert _source_file(display_seq=seq).display_name == name

    def test_expires_at_round_trips(self):
        sf = _source_file(display_seq=3, expires_at=NOW + timedelta(days=7))

        again = SourceFile.from_dict(sf.to_dict())

        assert again == sf
        assert again.expires_at == NOW + timedelta(days=7)

    def test_expires_at_may_be_read_from_epoch_seconds(self):
        data = _source_file().to_dict()
        data["expires_at"] = int((NOW + timedelta(days=7)).timestamp())

        assert SourceFile.from_dict(data).expires_at == NOW + timedelta(days=7)

    def test_effective_expiry_of_an_u1_record_counts_from_the_creation(self):
        sf = _source_file()

        assert sf.expires_at is None
        assert sf.effective_expires_at(7) == NOW + timedelta(days=7)

    def test_effective_expiry_of_an_u1_record_counts_from_the_last_use(self):
        sf = _source_file().update(last_used_at=NOW + timedelta(days=2))

        assert sf.effective_expires_at(7) == NOW + timedelta(days=9)

    def test_stored_expiry_wins(self):
        sf = _source_file(expires_at=NOW + timedelta(days=1)).update(
            last_used_at=NOW + timedelta(days=2)
        )

        assert sf.effective_expires_at(7) == NOW + timedelta(days=1)

    def test_is_live_until_the_expiry_and_never_while_deleting(self):
        sf = _source_file(expires_at=NOW + timedelta(days=7))

        assert sf.is_live(NOW + timedelta(days=6), 7)
        assert not sf.is_live(NOW + timedelta(days=7), 7)
        assert not sf.update(status=SourceFileStatus.DELETING).is_live(NOW, 7)

    def test_in_use_while_the_mark_is_valid(self):
        sf = _source_file().update(
            in_use_by="gen-1", in_use_until=NOW + timedelta(hours=1)
        )

        assert sf.is_in_use(NOW)
        assert not sf.is_in_use(NOW + timedelta(hours=1))
        assert not _source_file().is_in_use(NOW)


class TestSourceFileListItem:
    def _ready(self, **changes) -> SourceFile:
        return _source_file(expires_at=NOW + timedelta(days=7)).update(
            status=SourceFileStatus.READY, metadata=_metadata(), **changes
        )

    def test_ready_file_is_selectable_with_an_expiry_date(self):
        item = SourceFileListItem.of(self._ready(), now=NOW, retention_days=7)

        assert item.display_name == "sales.csv"
        assert item.status is SourceFileStatus.READY
        assert item.selectable and not item.in_use
        assert item.expires_on == (NOW + timedelta(days=7)).date()

    def test_in_use_file_is_not_selectable(self):
        busy = self._ready(in_use_by="g", in_use_until=NOW + timedelta(hours=1))

        item = SourceFileListItem.of(busy, now=NOW, retention_days=7)

        assert item.in_use and not item.selectable

    def test_uploading_file_has_no_expiry_date(self):
        item = SourceFileListItem.of(_source_file(), now=NOW, retention_days=7)

        assert item.expires_on is None and not item.selectable

    def test_error_file_keeps_its_code(self):
        failed = _source_file().update(
            status=SourceFileStatus.ERROR, error_code="source_not_a_table"
        )

        item = SourceFileListItem.of(failed, now=NOW, retention_days=7)

        assert item.error_code == "source_not_a_table"
        assert item.expires_on is not None


class TestSourceAvailability:
    def test_states(self):
        assert [s.value for s in SourceAvailabilityState] == [
            "available",
            "deleted",
            "unknown",
        ]

    def test_value(self):
        found = SourceAvailability(
            source_file_id="sf-1",
            display_name="sales.csv (2)",
            state=SourceAvailabilityState.DELETED,
        )

        assert found.state is SourceAvailabilityState.DELETED


class TestAnalysisRecordU2:
    def _record(self, **extra) -> AnalysisRecord:
        return AnalysisRecord.create_new(
            record_id="gen-1",
            owner_sub="user-1",
            generation_id="gen-1",
            plan=_plan().to_dict(),
            code_blocks=[],
            seed=42,
            prepare_version="p1",
            source_checksums=["crc=="],
            now=NOW,
            **extra,
        )

    def test_keeps_the_ids_and_display_names(self):
        record = self._record(
            source_file_ids=["sf-1"], source_display_names=["sales.csv (2)"]
        )

        again = AnalysisRecord.from_dict(record.to_dict())

        assert again.source_file_ids == ("sf-1",)
        assert again.source_display_names == ("sales.csv (2)",)

    def test_an_u1_record_has_neither(self):
        data = self._record().to_dict()

        assert "source_file_ids" not in data
        again = AnalysisRecord.from_dict(data)
        assert again.source_file_ids == () and again.source_display_names == ()


class TestSourceErrorCodesU2:
    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            (ErrorCode.SOURCE_FILE_IN_USE, ErrorKind.TRANSIENT),
            (ErrorCode.SOURCE_EMPTY_COLUMN_NAME, ErrorKind.VALIDATION),
            (ErrorCode.SOURCE_DUPLICATE_COLUMN_NAME, ErrorKind.VALIDATION),
            (ErrorCode.SOURCE_ENCODING_UNKNOWN, ErrorKind.VALIDATION),
            (ErrorCode.SOURCE_NOT_A_TABLE, ErrorKind.VALIDATION),
        ],
    )
    def test_kind(self, code, kind):
        assert code.kind is kind
