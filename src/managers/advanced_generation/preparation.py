"""The preparing stage: bring the files in, prepare the tables and the joins, and read the documents.

It ends with the guide table chosen, the join plans made and, for the
documents, the text or the checked evidence the next stages read.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from ...config import config
from ...models.advanced_generation import EventType, GenerationStatus
from ...models.analysis import (
    TABLE_ALIASES,
    DocumentMode,
    DocumentReport,
    Evidence,
    PrepareReport,
)
from ...models.errors import CodedError, ErrorCode, ErrorKind
from ...models.source_file import SourceFile, SourceKind
from ...services.s3_service import S3ServiceError
from ..shared.advanced_generation_log import log_advanced_event
from . import documents, prompts
from .analysis_policy import AnalysisPolicy
from .errors import AdvancedGenerationError, StopRun, fail
from .join_planner import PlannedTable, plan_joins
from .run_context import CAUSE_SANDBOX, RunContext, safe_context
from .run_types import Run, Table

logger = logging.getLogger(__name__)

#: Seconds between two "根拠の抽出 n / M" notices when 10% is not reached.
_EXTRACTION_NOTICE_SECONDS = 30
#: The preparation's time limit, told apart in ``error_context``.
_LIMIT_PREPARE = "prepare_minutes"
#: Wait before the one re-send of a failed copy of the source.
_REFRESH_RETRY_SECONDS = 1.0


class PreparationStage:
    """Bring every file in, prepare each table and each document."""

    def __init__(self, context: RunContext) -> None:
        self._context = context

    def prepare(self, state: Run) -> None:
        """Bring every file in, prepare the table, then each document.

        Every chosen file is refreshed and brought in. The 20 minutes of the
        preparation count the table and the documents together.
        """
        generation = state.generation
        sources = [
            self._context.catalog.get_owned(
                generation.owner_sub,
                source_file_id,
                not_found_code=ErrorCode.GENERATION_SOURCE_NOT_FOUND,
            )
            for source_file_id in generation.source_file_ids
        ]
        state.sources = [self._refresh_source(state, s) for s in sources]
        state.source = next(
            (s for s in state.sources if s.kind is SourceKind.TABLE), None
        )
        sandbox = self._context.sandbox
        state.session = sandbox.start_session(
            generation.generation_id, config.ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS
        )
        # A reader that ends the generation stops the session by its ID
        if not self._context.store.transition(
            generation.generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.PREPARING,
            updates={"session_id": state.session.session_id},
        ):
            raise StopRun()
        for source in state.sources:
            self._bring_in(state, source)
        prepare_started = self._context.clock()
        # Each table in the order chosen, one at a time, within
        # the same 20 minutes as the documents
        tables = [s for s in state.sources if s.kind is SourceKind.TABLE]
        for alias, table in zip(TABLE_ALIASES, tables):
            self._prepare_table(
                state, table, prepare_started, alias=alias, only=len(tables) == 1
            )
        if state.tables:
            self._settle_guide_table(state)
        position = 0
        for source_index, source in enumerate(state.sources):
            if source.kind is not SourceKind.DOCUMENT:
                continue
            position += 1
            report = self._prepare_document(
                state, position, source_index, source, prepare_started
            )
            state.documents.append((source_index, source, report))
        if state.documents:
            self._decide_document_mode(state)

    def _prepare_table(
        self,
        state: Run,
        source: SourceFile,
        prepare_started: datetime,
        *,
        alias: str = TABLE_ALIASES[0],
        only: bool = True,
    ) -> None:
        """One table's preparation and its checks."""
        assert state.session is not None
        started = time.monotonic()
        state.report = None
        try:
            state.report = self._run_prepare(state, source, prepare_started)
        except CodedError as e:
            self._log_prepared(state, started, e.code, e.context)
            raise
        self._log_prepared(state, started, None, {})
        if only:
            self._check_enough_rows(state)
        profile = self._context.sandbox.call(
            state.session, "profile_dataset", {"source_id": source.source_file_id}
        )
        state.profile = dict(self._context.library_value(profile))
        state.tables.append(Table(alias, source, state.report, state.profile))

    def _settle_guide_table(self, state: Run) -> None:
        """The table the guide and the population read, then joins.

        With several tables the row check is made on that table, and
        the join keys are found when the detailed analysis is on.
        """
        state.reading = AnalysisPolicy.read_tables(
            [(t.alias, t.source.display_name, t.profile) for t in state.tables]
        )
        self._context.use_guide_table(state, AnalysisPolicy.guide_alias(state.reading))
        if len(state.tables) > 1:
            self._check_enough_rows(state)
            if state.generation.use_detailed_analysis:
                self._plan_joins(state)

    def _plan_joins(self, state: Run) -> None:
        """Find the join keys with the fixed ``key_overlap``."""
        deadline = self._context.clock() + timedelta(
            seconds=config.ADVANCED_JOIN_CHECK_TIMEOUT_SECONDS
        )

        def overlap(
            axis: PlannedTable, other: PlannedTable, columns: list[str]
        ) -> Optional[list[Mapping[str, Any]]]:
            assert state.session is not None
            command_id = self._context.sandbox.start_command(
                state.session,
                "key_overlap",
                {"axis": axis.source_id, "other": other.source_id, "columns": columns},
            )
            while True:
                status = self._context.sandbox.poll_command(state.session, command_id)
                if status.get("done"):
                    result = status.get("result") or {}
                    value = result.get("value") if result.get("ok") else None
                    rows = value.get("columns") if isinstance(value, Mapping) else None
                    return list(rows) if isinstance(rows, list) else None
                self._context.checkpoint(state)
                if self._context.clock() > deadline:
                    self._cancel_quietly(state, command_id)
                    return None
                self._context.sleep(
                    self._context.poll_seconds
                    if self._context.poll_seconds is not None
                    else config.ADVANCED_GENERATION_PREPARE_POLL_SECONDS
                )

        state.joins = plan_joins(
            [
                PlannedTable(t.alias, t.source.source_file_id, t.profile)
                for t in state.tables
            ],
            overlap=overlap,
            min_match_rate=float(config.ADVANCED_JOIN_MIN_MATCH_RATE),
        )

    def _check_enough_rows(self, state: Run) -> None:
        """Stop when the table has fewer rows than persona count x 10.

        Runs after the bad-row check (which raised already if it applied) and
        before any model call. The count is the rows read, before any narrowing
        by the user's instruction.
        """
        assert state.report is not None
        persona_count = state.generation.persona_count
        required = persona_count * config.ADVANCED_MIN_ROWS_PER_PERSONA
        if state.report.rows_read < required:
            raise fail(
                ErrorCode.GENERATION_INSUFFICIENT_DATA,
                "too few rows for the persona count",
                rows_read=state.report.rows_read,
                required_rows=required,
                persona_count=persona_count,
            )

    def _log_prepared(
        self,
        state: Run,
        started: float,
        error_code: Optional[ErrorCode],
        context: Mapping[str, Any],
    ) -> None:
        """One line at the end of the preparation: counts and time only."""
        fields: dict[str, Any] = {
            "generation_id": state.generation.generation_id,
            "status": "failed" if error_code else "done",
            "error_code": error_code.value if error_code else None,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        report = state.report
        if report is not None:
            fields["rows_read"] = report.rows_read
            fields["rows_excluded"] = report.rows_excluded
            counts = (report.excluded_reasons or {}).get("counts")
            if isinstance(counts, Mapping):
                fields["excluded_reasons"] = {
                    str(k): v
                    for k, v in counts.items()
                    if isinstance(v, int) and not isinstance(v, bool)
                }
            fields["memory_limit"] = state.memory_limit
        else:
            safe = safe_context(context)
            for key in ("rows_read", "rows_excluded"):
                if key in safe:
                    fields[key] = safe[key]
        try:
            log_advanced_event("advanced_generation.prepared", **fields)
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("preparation not logged (%s)", type(e).__name__)

    def _refresh_source(self, state: Run, source: SourceFile) -> SourceFile:
        """Copy the object onto its key so S3 keeps it 9 more days.

        Runs before the session starts. A transient S3 failure is sent again
        once after 1 second; an object gone from S3 or a checksum
        that differs from the record is ``GENERATION_SOURCE_UNREADABLE``. A
        record without a checksum gets the copy's.

        Returns:
            The file, with the filled checksum when there was none.
        """
        started = time.monotonic()
        checksum: Optional[str] = None
        for attempt in range(2):
            try:
                checksum = self._context.s3.refresh_object(source.s3_key)
                break
            except S3ServiceError as e:
                if attempt == 0 and e.code.kind is ErrorKind.TRANSIENT:
                    self._context.sleep(_REFRESH_RETRY_SECONDS)
                    continue
                code = (
                    ErrorCode.GENERATION_SOURCE_UNREADABLE
                    if e.code is ErrorCode.S3_OBJECT_NOT_FOUND
                    else e.code
                )
                self._log_refresh(state, source, started, code)
                raise fail(code, "source object not refreshed") from e
        if checksum and source.checksum and checksum != source.checksum:
            code = ErrorCode.GENERATION_SOURCE_UNREADABLE
            self._log_refresh(state, source, started, code)
            raise fail(code, "source checksum changed")
        if checksum and not source.checksum:
            self._context.catalog.fill_checksum(source.source_file_id, checksum)
            source = source.update(checksum=checksum)
        self._log_refresh(state, source, started, None)
        return source

    @staticmethod
    def _log_refresh(
        state: Run,
        source: SourceFile,
        started: float,
        error_code: Optional[ErrorCode],
    ) -> None:
        log_advanced_event(
            "advanced_generation.source_refreshed",
            generation_id=state.generation.generation_id,
            source_file_id=source.source_file_id,
            status="failed" if error_code else "done",
            size_bytes=source.size_bytes,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_code=error_code.value if error_code else None,
        )

    def _bring_in(self, state: Run, source: SourceFile) -> None:
        """Hand the file in with a fresh presigned GET; one new URL on refusal."""
        outcome: Mapping[str, Any] = {}
        for _ in range(2):
            url = self._context.s3.generate_presigned_url(
                self._context.s3.path_of(source.s3_key),
                config.ADVANCED_GENERATION_DOWNLOAD_URL_SECONDS,
            )
            results = self._context.sandbox.put_files(
                state.session,  # type: ignore[arg-type]
                [
                    {
                        "source_file_id": source.source_file_id,
                        "s3_key": source.s3_key,
                        "download_url": url,
                    }
                ],
            )
            outcome = results[0] if results else {}
            if outcome.get("ok"):
                return
        context: dict[str, Any] = {"cause": CAUSE_SANDBOX}
        if outcome.get("http_status") is not None:
            context["http_status"] = int(outcome["http_status"])
        raise AdvancedGenerationError(
            "file not brought in",
            code=ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
            context=context,
        )

    def _run_prepare(
        self, state: Run, source: SourceFile, prepare_started: datetime
    ) -> PrepareReport:
        metadata = source.table_metadata
        assert metadata is not None and state.session is not None
        args = {
            "source_id": source.source_file_id,
            "metadata": {
                "encoding": metadata.encoding.value,
                "delimiter": metadata.delimiter,
            },
            "max_excluded_percent": config.ADVANCED_SOURCE_MAX_EXCLUDED_PERCENT,
        }
        command_id = self._context.sandbox.start_command(state.session, "prepare", args)
        last: dict[str, Optional[int]] = {"rows": None}

        def on_progress(progress: Mapping[str, Any]) -> bool:
            rows = progress.get("rows")
            if not isinstance(rows, int) or rows == last["rows"]:
                return False
            last["rows"] = rows
            self._context.event(
                state,
                EventType.PROGRESS,
                message=f"{rows} 行を読み込みました",
                detail={"rows": rows},
            )
            return True

        status = self._wait_command(state, command_id, prepare_started, on_progress)
        value = self._context.library_value(status.get("result") or {"ok": False})
        limit_text = value.get("memory_limit")
        if isinstance(limit_text, str) and re.fullmatch(r"\d{1,6}[KMGT]?B", limit_text):
            state.memory_limit = limit_text
        try:
            report = PrepareReport.from_dict(value)
        except (KeyError, TypeError, ValueError):
            raise fail(
                ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                "bad preparation report",
                cause=CAUSE_SANDBOX,
            ) from None
        self._context.event(
            state,
            EventType.PROGRESS,
            message=(
                f"{report.rows_read} 行を読み込みました（{report.rows_excluded} 行を除外）"
            ),
            detail={"rows": report.rows_read, "rows_excluded": report.rows_excluded},
        )
        return report

    def _wait_command(
        self,
        state: Run,
        command_id: str,
        prepare_started: datetime,
        on_progress: Callable[[Mapping[str, Any]], bool],
    ) -> Mapping[str, Any]:
        """Poll a started command until it ends, within the 20 and 60 minutes.

        ``on_progress`` gets the progress file and says whether it wrote an
        event; without one for 60 seconds a "still going" notice is written.
        The 20 minutes count from ``prepare_started``, the start of
        the whole preparation (table and documents).
        """
        assert state.session is not None
        sandbox = self._context.sandbox
        limit = timedelta(seconds=config.ADVANCED_GENERATION_PREPARE_TIMEOUT_SECONDS)
        notice = timedelta(seconds=config.ADVANCED_GENERATION_PREPARE_NOTICE_SECONDS)
        started = prepare_started
        last_event_at = self._context.clock()
        while True:
            status = sandbox.poll_command(state.session, command_id)
            if on_progress(status.get("progress") or {}):
                last_event_at = self._context.clock()
            if status.get("done"):
                return status
            if state.cancel_requested.is_set() or state.ended_elsewhere.is_set():
                # Stop the command, then end the run
                self._cancel_quietly(state, command_id)
                self._context.checkpoint(state)
            now = self._context.clock()
            if now - last_event_at >= notice:
                # The rows stand still while the table is written: say
                # that it goes on, so the screen hears from us within 5 minutes.
                last_event_at = now
                minutes = max(int((now - started).total_seconds() // 60), 1)
                self._context.event(
                    state,
                    EventType.PROGRESS,
                    message=f"前準備を続けています（{minutes} 分経過）",
                    detail={"minutes": minutes},
                )
            if now > state.deadline:
                self._cancel_quietly(state, command_id)
                raise self._context.generation_timeout("generation over 60 minutes")
            if now - started > limit:
                self._cancel_quietly(state, command_id)
                raise self._context.timeout(
                    "preparation over 20 minutes",
                    _LIMIT_PREPARE,
                    config.ADVANCED_GENERATION_PREPARE_TIMEOUT_SECONDS,
                )
            self._context.sleep(
                self._context.poll_seconds
                if self._context.poll_seconds is not None
                else config.ADVANCED_GENERATION_PREPARE_POLL_SECONDS
            )

    # --- Documents --------------------------------

    def _prepare_document(
        self,
        state: Run,
        position: int,
        source_index: int,
        source: SourceFile,
        prepare_started: datetime,
    ) -> DocumentReport:
        """Prepare one document in the sandbox and say what it holds."""
        assert state.session is not None
        metadata = source.document_metadata
        fmt = source.source_format
        if metadata is None or fmt is None:
            raise fail(ErrorCode.GENERATION_SOURCE_NOT_FOUND, "not a ready document")
        args: dict[str, Any] = {
            "source_id": source.source_file_id,
            "source_index": source_index,
            "format": fmt.value,
            "chunk_chars": int(config.ADVANCED_DOCUMENT_CHUNK_CHARS),
            "overlap_chars": int(config.ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS),
        }
        if metadata.encoding is not None:
            args["encoding"] = metadata.encoding.value
        started = time.monotonic()
        last: dict[str, Optional[int]] = {"pages": None}

        def on_progress(progress: Mapping[str, Any]) -> bool:
            pages = progress.get("pages")
            if not isinstance(pages, int) or pages == last["pages"]:
                return False
            last["pages"] = pages
            self._context.event(
                state,
                EventType.PROGRESS,
                message=f"{position} 件目の文書: {pages} ページを読み込みました",
                detail={"pages": pages},
            )
            return True

        try:
            command_id = self._context.sandbox.start_command(
                state.session, "prepare_document", args
            )
            status = self._wait_command(state, command_id, prepare_started, on_progress)
            value = self._context.library_value(status.get("result") or {"ok": False})
            try:
                report = DocumentReport.from_dict(value)
            except (KeyError, TypeError, ValueError):
                raise fail(
                    ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                    "bad document report",
                    cause=CAUSE_SANDBOX,
                ) from None
        except CodedError as e:
            self._log_document(state, source_index, fmt.value, started, None, e.code)
            raise
        self._log_document(state, source_index, fmt.value, started, report, None)
        self._context.event(
            state,
            EventType.PROGRESS,
            message=documents.prepared_message(position, report),
            detail={
                "document_index": source_index,
                "pages": report.pages,
                "chars": report.chars,
                "chunks": report.chunks,
                "no_text_pages": len(report.no_text_pages),
            },
        )
        return report

    @staticmethod
    def _log_document(
        state: Run,
        source_index: int,
        fmt: str,
        started: float,
        report: Optional[DocumentReport],
        error_code: Optional[ErrorCode],
    ) -> None:
        """One line per document: counts and time only, never names."""
        try:
            log_advanced_event(
                "advanced_generation.document_prepared",
                generation_id=state.generation.generation_id,
                status="failed" if error_code else "done",
                error_code=error_code.value if error_code else None,
                document_index=source_index,
                format=fmt,
                pages=report.pages if report else None,
                chars=report.chars if report else None,
                chunks=report.chunks if report else None,
                no_text_pages=len(report.no_text_pages) if report else None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("document preparation not logged (%s)", type(e).__name__)

    def _decide_document_mode(self, state: Run) -> None:
        """The total limit, then full text or extraction."""
        total = sum(report.chars for _, _, report in state.documents)
        chunks = sum(report.chunks for _, _, report in state.documents)
        limit = int(config.ADVANCED_DOCUMENT_MAX_TOTAL_CHARS)
        if total > limit:
            raise fail(
                ErrorCode.GENERATION_SOURCE_TOO_LARGE,
                "documents over the total limit",
                total_chars=total,
                max_chars=limit,
            )
        full_text = total <= int(config.PERSONA_SOURCE_MAX_CHARS)
        state.document_mode = (
            DocumentMode.FULL_TEXT if full_text else DocumentMode.EXTRACTION
        )
        self._context.event(
            state,
            EventType.PROGRESS,
            message=documents.mode_message(total, chunks, full_text),
            detail={"mode": state.document_mode.value, "chunks": chunks},
        )
        try:
            log_advanced_event(
                "advanced_generation.document_mode",
                generation_id=state.generation.generation_id,
                mode=state.document_mode.value,
                total_chars=total,
                chunks=chunks,
            )
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("document mode not logged (%s)", type(e).__name__)
        if full_text:
            overlap = int(config.ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS)
            state.document_texts = [
                documents.join_chunks(
                    [
                        self._read_chunk(state, source.source_file_id, i)[0]
                        for i in range(report.chunks)
                    ],
                    overlap,
                )
                for _, source, report in state.documents
            ]
        else:
            self._extract_evidence(state)

    def _read_chunk(
        self, state: Run, source_id: str, index: int
    ) -> tuple[str, Mapping[str, Any]]:
        """One chunk, taken out of the session a piece at a time."""
        assert state.session is not None
        parts: list[str] = []
        offset: Optional[int] = 0
        info: Mapping[str, Any] = {}
        while offset is not None:
            value = self._context.library_value(
                self._context.sandbox.call(
                    state.session,
                    "document_chunk",
                    {
                        "source_id": source_id,
                        "index": index,
                        "offset": offset,
                        "max_chars": int(config.ADVANCED_DOCUMENT_TRANSFER_CHARS),
                    },
                ),
                strict=True,
            )
            parts.append(str(value.get("text") or ""))
            info = value
            next_offset = value.get("next_offset")
            offset = next_offset if isinstance(next_offset, int) else None
        return "".join(parts), info

    def _extract_evidence(self, state: Run) -> None:
        """Evidence out of every chunk, a few at once.

        Each task takes one chunk out of the session, asks the light model and
        returns its evidence; the chunk's text goes when the task ends. One
        failure stops the rest without waiting for them.
        """
        started = time.monotonic()
        tasks = [
            (source_index, source.source_file_id, chunk)
            for source_index, source, report in state.documents
            for chunk in range(report.chunks)
        ]
        total = len(tasks)
        step = max(1, math.ceil(total / 10))
        tokens = {"input_tokens": 0, "output_tokens": 0}
        found: list[tuple[int, int, list[Evidence]]] = []
        # Set by the first failure: later tasks do not start and running ones
        # do not retry, so no call goes on after the run has failed.
        stopped = threading.Event()
        first_failure: list[BaseException] = []
        executor = ThreadPoolExecutor(
            max_workers=max(1, int(config.ADVANCED_EVIDENCE_MAX_CONCURRENT)),
            thread_name_prefix="advanced-evidence",
        )
        try:
            futures: list[Future[tuple[int, int, list[Evidence], dict[str, int]]]] = [
                executor.submit(
                    self._extract_chunk, state, stopped, first_failure, *task
                )
                for task in tasks
            ]
            done = 0
            last_notice = self._context.clock()
            for future in as_completed(futures):
                source_index, chunk, evidence, usage = future.result()
                found.append((source_index, chunk, evidence))
                for key in tokens:
                    tokens[key] += int(usage.get(key, 0))
                done += 1
                now = self._context.clock()
                if (
                    done % step == 0
                    or done == total
                    or (now - last_notice).total_seconds() >= _EXTRACTION_NOTICE_SECONDS
                ):
                    last_notice = now
                    self._context.event(
                        state,
                        EventType.PROGRESS,
                        message=f"根拠の抽出 {done} / {total}",
                        detail={"done": done, "total": total},
                    )
        except BaseException as e:
            stopped.set()
            executor.shutdown(wait=False, cancel_futures=True)
            # A task that gave up because of the stop may finish first; the
            # failure that caused the stop is the one the run reports.
            if first_failure and first_failure[0] is not e:
                raise first_failure[0] from None
            raise
        executor.shutdown(wait=True)
        extracted = [
            e for _, _, evidence in sorted(found, key=lambda f: f[:2]) for e in evidence
        ]
        state.evidence_tokens = tokens
        state.document_evidence = self._context.check_quotes(state, extracted)
        stats = state.evidence_stats
        try:
            log_advanced_event(
                "advanced_generation.evidence_extracted",
                generation_id=state.generation.generation_id,
                chunks=total,
                extracted=stats.get("extracted", 0),
                discarded_unverified=stats.get("discarded_unverified", 0),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                evidence_input_tokens=tokens["input_tokens"],
                evidence_output_tokens=tokens["output_tokens"],
            )
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("evidence extraction not logged (%s)", type(e).__name__)

    def _extract_chunk(
        self,
        state: Run,
        stopped: threading.Event,
        first_failure: list[BaseException],
        source_index: int,
        source_id: str,
        chunk: int,
    ) -> tuple[int, int, list[Evidence], dict[str, int]]:
        """One task of the extraction: the time check, the chunk, the model.

        A failure here sets ``stopped`` before it is raised, so the next task a
        worker picks up gives up at once.
        """
        if stopped.is_set():
            raise fail(
                ErrorCode.GENERATION_OPERATION_FAILED, "extraction already stopped"
            )
        try:
            self._context.before_model(state)  # before every call
            text, info = self._read_chunk(state, source_id, chunk)
            # One agent per call: a Strands agent is never shared by two threads.
            agent = self._context.agents.create_advanced_evidence_agent(
                prompts.EVIDENCE_INSTRUCTIONS
            )
            output, usage = self._context.agents.run_evidence_extraction(
                agent,
                prompts.evidence_prompt(
                    state.generation.purpose,
                    int(config.ADVANCED_EVIDENCE_PER_CHUNK_MAX),
                    int(config.ADVANCED_EVIDENCE_QUOTE_MAX_CHARS),
                ),
                text,
                prompts.EvidenceExtraction,
                document_index=source_index,
                chunk_index=chunk,
                may_retry=lambda: not stopped.is_set(),
                purpose=state.generation.purpose,
            )
        except BaseException as e:
            # list.append is atomic: the first item is the earliest failure.
            if not stopped.is_set():
                first_failure.append(e)
            stopped.set()
            raise
        evidence = documents.evidence_from_extraction(
            output if isinstance(output, prompts.EvidenceExtraction) else None,
            source_index=source_index,
            source_id=source_id,
            chunk_index=chunk,
            info=info,
            max_items=int(config.ADVANCED_EVIDENCE_PER_CHUNK_MAX),
            max_quote_chars=int(config.ADVANCED_EVIDENCE_QUOTE_MAX_CHARS),
        )
        return source_index, chunk, evidence, usage

    def _cancel_quietly(self, state: Run, command_id: str) -> None:
        try:
            self._context.sandbox.cancel_command(state.session, command_id)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 - the session is stopped anyway
            logger.warning("preparation not cancelled (%s)", type(e).__name__)
