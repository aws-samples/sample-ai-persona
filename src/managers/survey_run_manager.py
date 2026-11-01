"""SurveyRunManager: the survey runner's round (functional-spec.md WF2〜WF4).

The Web task calls :meth:`SurveyRunManager.advance` every 15 seconds and right
after a survey is accepted or deleted. One round:

1. **Lease** (BR2.3): take or extend the deployment's single lease; without it
   the round does nothing. While the round works, :class:`LeaseKeeper` extends
   the lease every 20 seconds from another thread; once it cannot, no further
   write is made. The lease is not given back at the end of a round (the next
   round takes it again); :meth:`release_lease` gives it back on a clean stop.
2. **Monitor** (WF3): the running survey's execution. Still running: the
   round ends (one survey at a time, BR2.1). Finished: the results become the
   CSV (5% rule, BR4) or an error code, and the round goes on (BR2.4).
3. **Start** (WF4): the oldest queued survey (``(queued_at, id)``, BR2.2). The
   sampled personas are written once (BR1.5); the execution is named after
   the survey (BR3.1).

Every failed external call is handled by its class (reliability 3.1):
not found → error now; transient → retried within a count or time limit;
permissions / configuration → the survey waits (queued: up to 30 minutes from
``start_blocked_since``; finishing: up to 30 minutes after the execution ended;
running: the time limit). Every status change is conditional on the expected
status (NFR4.4). When one does not hold, the record is read again strongly
consistently: only a deleted survey has its CSV, run location or execution
removed; what another task wrote first is left alone. A running survey from
the (eventually consistent) index is also read again before it is watched.
Exceptions never leave :meth:`advance` (NFR4.15).

Logs carry IDs, execution ARNs, counts and codes only — never persona
attributes, request bodies or answers (NFR5.12, NFR7.9).
"""

import base64
import json
import logging
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Protocol

import polars as pl

from ..config import config
from ..models.custom_persona_data import PersonaDataRef
from ..models.errors import CodedError, ErrorCode, ErrorKind
from ..models.external_failure import FailureClass
from ..models.survey import Survey, SurveyStatus, TemplateSnapshot
from ..models.survey_run import SurveyRunItem, SurveyRunOutcome
from ..prompts.survey_prompts import (
    PERSONA_ATTRIBUTE_COLUMNS,
    PERSONA_PROFILE_COLUMNS,
    build_answer_schema,
    build_persona_system_prompt,
    build_survey_user_content,
    format_questions_for_prompt,
)
from ..services.service_factory import service_factory
from .components.survey_persona_source import SurveyPersonaSource
from .shared.file_utils import build_results_csv_bytes, compress_image_for_batch
from .shared.structured_log import log_event

logger = logging.getLogger(__name__)

#: Seconds a lease lasts after each take or extension (BR2.3).
LEASE_TTL_SECONDS = 60
#: How often the keeper extends the lease while a round works (BR2.3).
LEASE_RENEW_SECONDS = 20
#: Extra time past the execution time limit before the app gives up (BR3.3).
TIMEOUT_GRACE_SECONDS = 300
#: How long a permissions / configuration failure may block a survey (NFR4.9a, R-12).
BLOCKED_LIMIT = timedelta(minutes=30)
#: Transient failures before the start or the finish gives up (BR3.2, BR3.7).
MAX_ATTEMPTS = 3
#: A round longer than this is logged as a warning (NFR7.8).
SLOW_ROUND_SECONDS = 60
#: Highest failure ratio of a completed survey: 5 in 100 (BR4.1).
_FAILED_PER_HUNDRED = 5
#: Columns of the result CSV before the questions (unchanged).
_CSV_ATTRIBUTES = (
    "sex",
    "age",
    "occupation",
    "country",
    "region",
    "prefecture",
    "marital_status",
)
#: Codes that stop a start at once although their kind is TRANSIENT (BR2.2 (b)).
_LASTING_CODES = frozenset({ErrorCode.SURVEY_TEMPLATE_IMAGE_UNAVAILABLE})
_LASTING_KINDS = frozenset(
    {ErrorKind.VALIDATION, ErrorKind.CAPACITY, ErrorKind.NOT_FOUND, ErrorKind.CONFIG}
)
#: Lasting codes recorded as they are when a survey cannot start or be summed
#: up: the survey's own reasons, the ones its result page can explain. Any
#: other lasting code (an infrastructure one such as S3_OBJECT_NOT_FOUND)
#: becomes SURVEY_EXECUTION_FAILED; the original is logged.
_SHOWN_CODES = frozenset(
    {
        ErrorCode.SURVEY_TEMPLATE_NOT_FOUND,
        ErrorCode.SURVEY_TEMPLATE_IMAGE_UNAVAILABLE,
        ErrorCode.SURVEY_AVAILABLE_PERSONAS_TOO_FEW,
        ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS,
        ErrorCode.NEMOTRON_DATASET_NOT_PLACED,
        ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED,
        ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND,
    }
)


class SurveyRunError(CodedError):
    """A survey cannot be started or finished (stored as its code)."""

    code = ErrorCode.SURVEY_EXECUTION_FAILED


class _UnusableRunResult(SurveyRunError):
    """The finished execution left nothing readable; retrying cannot help."""

    failure = FailureClass.INVALID


class _LeaseLost(Exception):
    """The lease was taken by another task while this round worked."""


class _Outcome(Enum):
    """What one start attempt did to the queue."""

    STARTED = "started"  # the survey is running now
    WAITING = "waiting"  # it stays first in the queue (retry next round)
    SKIPPED = "skipped"  # it left the queue; the next one may start


class Keeper(Protocol):
    """What the manager needs from the lease keeper (tests pass a fake)."""

    @property
    def lost(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class LeaseKeeper:
    """Extends the lease every ``interval_seconds`` while a round works.

    Args:
        renew: Extends the lease; ``False`` (or an exception) means it was lost.
        interval_seconds: Time between extensions (20 seconds, BR2.3).
        wait: Waits up to the given seconds and returns ``True`` when stopped
            (tests pass a fake; the default waits on an event).
    """

    def __init__(
        self,
        renew: Callable[[], bool],
        *,
        interval_seconds: float = LEASE_RENEW_SECONDS,
        wait: Optional[Callable[[float], bool]] = None,
    ) -> None:
        self._renew = renew
        self._interval = interval_seconds
        self._stopped = threading.Event()
        self._wait = wait or self._stopped.wait
        self._lost = False
        self._thread: Optional[threading.Thread] = None

    @property
    def lost(self) -> bool:
        """The lease could not be extended (no further writes)."""
        return self._lost

    def start(self) -> None:
        """Start extending in a daemon thread."""
        self._thread = threading.Thread(
            target=self.run, name="survey-run-lease", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop extending (also when the round ended with an exception)."""
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def run(self) -> None:
        """Extend until stopped or the lease is lost."""
        while not self._wait(self._interval):
            try:
                renewed = self._renew()
            except Exception as e:
                logger.warning(
                    "Extending the survey run lease failed: %s", type(e).__name__
                )
                renewed = False
            if not renewed:
                self._lost = True
                return


def is_lasting_failure(error: BaseException) -> bool:
    """A coded failure of the survey itself, not to be retried (WF4 6)."""
    code = getattr(error, "code", None)
    if not isinstance(code, ErrorCode):
        return False
    return code in _LASTING_CODES or code.kind in _LASTING_KINDS


def parse_answers(
    text: Optional[str], questions: Sequence[Any]
) -> Optional[dict[str, str]]:
    """The CSV values of a response, or ``None`` when it is unusable (BR5.5).

    A response that is not JSON, lacks a question, or holds a value outside
    the schema makes the whole persona unusable; no question is kept alone.
    Multiple answers lose duplicates and are cut at ``max_selections``
    (BR5.3) and are joined with ``|`` as before.
    """
    if not text:
        return None
    try:
        answers = json.loads(text).get("answers")
    except (ValueError, AttributeError):
        return None
    if not isinstance(answers, dict):
        return None
    values: dict[str, str] = {}
    for question in questions:
        value = _csv_value(answers.get(question.id), question)
        if value is None:
            return None
        values[question.id] = value
    return values


def _csv_value(value: Any, question: Any) -> Optional[str]:
    """One answer as written to the CSV, or ``None`` when outside the schema."""
    if question.question_type == "multiple_choice":
        if not question.allow_multiple:
            return (
                value if isinstance(value, str) and value in question.options else None
            )
        if not isinstance(value, list) or not value:
            return None
        if not all(isinstance(v, str) and v in question.options for v in value):
            return None
        chosen = list(dict.fromkeys(value))
        if question.max_selections > 0:
            chosen = chosen[: question.max_selections]
        return "|".join(chosen)
    if question.question_type == "scale_rating":
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if not question.scale_min <= value <= question.scale_max:
            return None
        return str(value)
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class _Request:
    """What every persona's request of one survey shares."""

    schema: dict[str, Any]
    questions_text: str
    image_names: tuple[str, ...]
    image_blocks: tuple[dict[str, Any], ...]
    extra_columns: Optional[list[dict[str, str]]]


class SurveyRunManager:
    """The survey runner (one round per :meth:`advance`)."""

    def __init__(
        self,
        *,
        survey_store: Any = None,
        lease_store: Any = None,
        runner: Any = None,
        batch_service: Any = None,
        s3_service: Any = None,
        custom_persona_data_store: Any = None,
        survey_template_store: Any = None,
        holder: Optional[str] = None,
        model_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        keeper_factory: Optional[Callable[[Callable[[], bool]], Keeper]] = None,
    ) -> None:
        """Create the manager (services default to ``service_factory``).

        Args:
            survey_store: ``SurveyStore``.
            lease_store: ``SurveyRunLeaseStore``.
            runner: ``SurveyRunnerService`` (Step Functions and survey-runs/).
            batch_service: ``SurveyBatchService`` (persona sampling).
            s3_service: ``S3Service`` (CSV, images, custom data files).
            custom_persona_data_store: Custom persona data records.
            survey_template_store: Templates (surveys without a snapshot).
            holder: This task's lease holder ID (a random one by default).
            model_id: ``SURVEY_MODEL_ID``.
            timeout_seconds: ``SURVEY_EXECUTION_TIMEOUT_SECONDS``.
            clock: Current UTC time (tests).
            keeper_factory: Builds the lease keeper of a round (tests).
        """
        self._surveys = survey_store or service_factory.get_survey_store()
        self._lease = lease_store or service_factory.get_survey_run_lease_store()
        self._runner = runner or service_factory.get_survey_runner_service()
        batch = batch_service or service_factory.get_survey_batch_service()
        self._batch = batch
        self._s3 = s3_service or service_factory.get_s3_service()
        self._custom_store = custom_persona_data_store
        self._templates = survey_template_store
        self._personas = SurveyPersonaSource(batch, self._s3)
        self.holder = holder or f"task-{uuid.uuid4()}"
        self._model_id = model_id or config.SURVEY_MODEL_ID
        self._timeout = timedelta(
            seconds=timeout_seconds or config.SURVEY_EXECUTION_TIMEOUT_SECONDS
        )
        self._clock = clock
        self._keeper_factory = keeper_factory or (lambda renew: LeaseKeeper(renew))
        self._keeper: Optional[Keeper] = None
        self._holding = False

    # =========================================================================
    # 1 回（WF2）
    # =========================================================================

    def advance(self) -> None:
        """Do one round of work. Never raises (NFR4.13, NFR4.15)."""
        began = self._clock()
        try:
            if not self._take_lease():
                return
            keeper = self._keeper_factory(self._renew_lease)
            self._keeper = keeper
            keeper.start()
            try:
                self._work()
            finally:
                keeper.stop()
                self._keeper = None
        except _LeaseLost:
            self._holding = False
            log_event("survey.runner_lease_lost", level="WARNING", holder=self.holder)
        except Exception as e:
            logger.warning("Survey runner round failed", exc_info=True)
            log_event(
                "survey.runner_round_failed",
                level="WARNING",
                error_type=type(e).__name__,
            )
        finally:
            seconds = (self._clock() - began).total_seconds()
            if seconds > SLOW_ROUND_SECONDS:
                log_event(
                    "survey.runner_slow", level="WARNING", seconds=round(seconds, 1)
                )

    def release_lease(self) -> bool:
        """Give the lease back on a clean stop (only when it is ours, NFR4.10)."""
        try:
            released = bool(self._lease.release(self.holder))
        except Exception as e:
            logger.warning(
                "Releasing the survey run lease failed: %s", type(e).__name__
            )
            return False
        if released:
            self._holding = False
            log_event("survey.runner_lease_released", holder=self.holder)
        return released

    def _take_lease(self) -> bool:
        taken = bool(
            self._lease.acquire(
                self.holder, now=self._epoch(), ttl_seconds=LEASE_TTL_SECONDS
            )
        )
        if taken and not self._holding:
            log_event("survey.runner_lease_acquired", holder=self.holder)
        self._holding = taken
        return taken

    def _renew_lease(self) -> bool:
        return bool(
            self._lease.renew(
                self.holder, now=self._epoch(), ttl_seconds=LEASE_TTL_SECONDS
            )
        )

    def _check_lease(self) -> None:
        """Stop before a write once the lease is lost (BR2.3)."""
        if self._keeper is not None and self._keeper.lost:
            raise _LeaseLost()

    def _epoch(self) -> int:
        return int(self._clock().timestamp())

    def _work(self) -> None:
        indexed = self._surveys.list_run_queue()
        # The index is eventually consistent: a survey started in an earlier
        # round may still show queued there. Re-read every row before choosing
        # what to start, so a running one anywhere in the queue holds the slot
        fresh = self._surveys.get_many([s.id for s in indexed])
        surveys = [fresh[s.id] for s in indexed if s.id in fresh]
        for survey in (s for s in surveys if s.status == SurveyStatus.RUNNING):
            if not self._watch(survey):
                return
        queued = sorted(
            (s for s in surveys if s.status == SurveyStatus.QUEUED),
            key=lambda s: (s.queued_at or s.created_at, s.id),
        )
        for survey in queued:
            if self._start(survey) is not _Outcome.SKIPPED:
                return

    # =========================================================================
    # 監視（WF3）。戻り値は「実行の席が空いたか」
    # =========================================================================

    def _watch(self, survey: Survey) -> bool:
        # ``survey`` was re-read this round (strongly consistent, see _work)
        arn = survey.execution_arn
        if not arn or not self._runner.is_own_execution(arn):
            # The state machine was created again; the old one cannot be followed
            self._fail(survey, ErrorCode.SURVEY_EXECUTION_FAILED)
            return True
        try:
            info = self._runner.describe_execution(arn)
        except Exception as e:
            return self._watch_failed(survey, e)
        if info.status == "RUNNING":
            if self._past_deadline(survey):
                self._stop_and_time_out(survey)
                return True
            return False
        if info.status == "SUCCEEDED":
            return self._finish(survey, info)
        code = (
            ErrorCode.SURVEY_EXECUTION_TIMED_OUT
            if info.status == "TIMED_OUT"
            else ErrorCode.SURVEY_INFERENCE_FAILED
        )
        self._fail(survey, code)
        return True

    def _watch_failed(self, survey: Survey, error: Exception) -> bool:
        failure = _failure_of(error)
        if failure is FailureClass.NOT_FOUND:
            self._fail(survey, ErrorCode.SURVEY_EXECUTION_FAILED)
            return True
        if self._past_deadline(survey):
            self._stop_and_time_out(survey)
            return True
        self._waiting(survey, "monitor", failure, error)
        return False

    def _past_deadline(self, survey: Survey) -> bool:
        started = survey.started_at or survey.queued_at or survey.created_at
        deadline = (
            _aware(started) + self._timeout + timedelta(seconds=TIMEOUT_GRACE_SECONDS)
        )
        return self._clock() > deadline

    def _stop_and_time_out(self, survey: Survey) -> None:
        self._check_lease()
        self._stop_quietly(survey.id, survey.execution_arn or "", "timed_out")
        self._fail(survey, ErrorCode.SURVEY_EXECUTION_TIMED_OUT)

    def _stop_quietly(self, survey_id: str, arn: str, reason: str) -> None:
        try:
            self._runner.stop_execution(arn)
            log_event(
                "survey.run_stopped",
                survey_id=survey_id,
                execution_arn=arn,
                reason=reason,
            )
        except Exception as e:
            log_event(
                "survey.run_stop_failed",
                level="WARNING",
                survey_id=survey_id,
                execution_arn=arn,
                error_type=type(e).__name__,
            )

    # =========================================================================
    # 完了のまとめ（WF3 の 4、BR3.7、BR4）
    # =========================================================================

    def _finish(self, survey: Survey, info: Any) -> bool:
        try:
            if not info.manifest_key:
                # Decided before any read: a failing read must not retry it
                raise _UnusableRunResult("succeeded without a ResultWriter manifest")
            # Read once for the summary and the CSV
            personas = self._runner.read_personas(survey.id)
            questions = list(self._snapshot(survey).questions)
            outcome = self._collect(survey, info, personas, questions)
        except MemoryError:
            self._fail(survey, ErrorCode.SURVEY_EXECUTION_FAILED)
            return True
        except _LeaseLost:
            raise
        except Exception as e:
            return self._finish_failed(survey, info, e)
        if not self._acceptable(outcome):
            self._fail(
                survey,
                ErrorCode.SURVEY_TOO_MANY_FAILED_ANSWERS,
                {"failed_count": outcome.failed_count, "total": outcome.total},
            )
            return True
        try:
            self._complete(survey, outcome, personas, questions)
        except _LeaseLost:
            raise
        except Exception as e:
            return self._finish_failed(survey, info, e)
        return True

    def _collect(
        self,
        survey: Survey,
        info: Any,
        personas: pl.DataFrame,
        questions: list[Any],
    ) -> SurveyRunOutcome:
        ids = [str(v) for v in personas["uuid"].to_list()]
        expected = set(ids)
        answers: dict[str, dict[str, str]] = {}
        errors: dict[str, str] = {}
        invalid: set[str] = set()
        seen: set[str] = set()
        for result in self._runner.iter_results(info.manifest_key):
            record_id = result.record_id
            if record_id not in expected:
                continue
            seen.add(record_id)
            if not result.succeeded:
                errors.setdefault(record_id, result.error or "unknown")
                continue
            parsed = parse_answers(result.text, questions)
            if parsed is None:
                invalid.add(record_id)
            else:
                answers[record_id] = parsed
        missing = expected - seen
        failed = (set(errors) | invalid | missing) - set(answers)
        return SurveyRunOutcome(
            execution_status=info.status,
            answers=answers,
            failed_record_ids=frozenset(failed),
            total=len(ids),
            failed_by_error=dict(Counter(errors[r] for r in errors if r in failed)),
            invalid_count=len(invalid & failed),
            missing_count=len(missing),
        )

    @staticmethod
    def _acceptable(outcome: SurveyRunOutcome) -> bool:
        """BR4.1: failed ÷ total ≤ 5% (no rounding), and not everyone failed."""
        failed = outcome.failed_count
        return (
            failed < outcome.total
            and failed * 100 <= outcome.total * _FAILED_PER_HUNDRED
        )

    def _complete(
        self,
        survey: Survey,
        outcome: SurveyRunOutcome,
        personas: pl.DataFrame,
        questions: list[Any],
    ) -> None:
        csv_bytes = self._results_csv(personas, questions, outcome)
        self._check_lease()
        key = f"survey-results/{survey.id}/results.csv"
        path = self._s3.upload_file(csv_bytes, key)
        completed = self._surveys.transition(
            survey.id,
            expected=SurveyStatus.RUNNING.value,
            fields={
                "status": SurveyStatus.COMPLETED.value,
                "s3_result_path": path,
                "persona_count": outcome.total,
                "failed_count": outcome.failed_count,
                "updated_at": _now_local(),
            },
            remove=("run_queue",),
        )
        if not completed:
            # Deleted meanwhile: nothing is written back (BR2.2 (c)). Another
            # task finished it first: the CSV and the run location are its own.
            if self._lost_to_deletion(survey.id, "complete"):
                self._delete_quietly(survey.id, path)
                self._cleanup_run(survey.id)
            return
        log_event(
            "survey.run_completed",
            survey_id=survey.id,
            execution_arn=survey.execution_arn,
            total=outcome.total,
            failed_count=outcome.failed_count,
            failed_by_error=dict(outcome.failed_by_error),
            invalid_count=outcome.invalid_count,
            missing_count=outcome.missing_count,
        )
        self._cleanup_run(survey.id)

    @staticmethod
    def _results_csv(
        personas: pl.DataFrame, questions: list[Any], outcome: SurveyRunOutcome
    ) -> bytes:
        """The result CSV (columns unchanged; unusable personas have blanks, BR4.3)."""
        headers = ["persona_id", *_CSV_ATTRIBUTES]
        for q in questions:
            headers.extend([f"{q.id}_text", f"{q.id}_answer"])
        rows: list[list[str]] = []
        for persona in personas.iter_rows(named=True):
            record_id = str(persona.get("uuid", ""))
            row = [record_id]
            for column in _CSV_ATTRIBUTES:
                value = persona.get(column)
                row.append("" if value is None else str(value))
            answers = outcome.answers.get(record_id, {})
            for q in questions:
                row.extend([q.text, answers.get(q.id, "")])
            rows.append(row)
        return build_results_csv_bytes(headers, rows)

    def _finish_failed(self, survey: Survey, info: Any, error: Exception) -> bool:
        code = getattr(error, "code", None)
        if isinstance(code, ErrorCode) and is_lasting_failure(error):
            # e.g. SURVEY_TEMPLATE_NOT_FOUND: keep the code, do not retry
            self._fail_lasting(survey, "finish", code, error)
            return True
        failure = _failure_of(error)
        if failure in (FailureClass.NOT_FOUND, FailureClass.INVALID):
            self._fail(survey, ErrorCode.SURVEY_EXECUTION_FAILED)
            return True
        if failure is FailureClass.ACCESS:
            ended = _aware(info.stop_date or info.start_date)
            if self._clock() > ended + BLOCKED_LIMIT:
                self._fail(survey, ErrorCode.SURVEY_RUNNER_UNAVAILABLE)
                return True
            self._waiting(survey, "finish", failure, error)
            return False
        # True: the survey left running (gave up, or deleted meanwhile)
        return not self._retry_later(survey, "finish_attempts", "finish", error)

    def _retry_later(
        self, survey: Survey, counter: str, stage: str, error: Exception
    ) -> bool:
        """Count a transient failure; ``True`` while the survey keeps waiting.

        The third failure turns the survey into ``SURVEY_EXECUTION_FAILED``
        (BR3.2, BR3.7). ``False`` also when the status changed meanwhile.
        """
        self._check_lease()
        attempts = self._surveys.add_attempt(survey.id, counter, expected=survey.status)
        if attempts is None:
            return False
        if attempts >= MAX_ATTEMPTS:
            self._fail(survey, ErrorCode.SURVEY_EXECUTION_FAILED)
            return False
        self._waiting(survey, stage, FailureClass.TRANSIENT, error)
        return True

    # =========================================================================
    # 開始（WF4）
    # =========================================================================

    def _start(self, queued: Survey) -> _Outcome:
        survey = self._surveys.get(queued.id, consistent=True)
        if survey is not None and survey.status == SurveyStatus.RUNNING:
            # The index still shows it queued; it holds the run slot (NFR3.7)
            return _Outcome.STARTED
        if survey is None or survey.status != SurveyStatus.QUEUED:
            return _Outcome.SKIPPED
        try:
            # Started before a restart but not recorded: take it over without
            # rebuilding the input, whose sources may be gone by now
            info = self._runner.find_execution(survey.id)
            if info is None:
                self._prepare(survey)
                self._check_lease()
                info = self._runner.start_execution(survey.id)
        except _LeaseLost:
            raise
        except Exception as e:
            return self._start_failed(survey, e)
        return self._record_start(survey, info)

    def _prepare(self, survey: Survey) -> None:
        """Personas (once, BR1.5), then the Distributed Map input (BR5, BR6)."""
        ref = PersonaDataRef.parse(survey.datasource)
        extra_columns = self._extra_columns(survey.id, ref)
        snapshot = self._snapshot(survey)
        personas = self._sampled_personas(survey, ref, extra_columns)
        request = _Request(
            schema=build_answer_schema(list(snapshot.questions)),
            questions_text=format_questions_for_prompt(list(snapshot.questions)),
            image_names=tuple(img.name for img in snapshot.images),
            image_blocks=tuple(self._image_blocks(survey.id, snapshot)),
            extra_columns=extra_columns,
        )
        self._check_lease()
        # Built lazily: the input is never held in memory as a whole (NFR1.6).
        # Closed at once on any error, so no body is sent after it
        with closing(self._items(survey.id, personas, request)) as items:
            self._runner.write_input(survey.id, self._lines(items))

    def _lines(self, items: Iterable[SurveyRunItem]) -> Iterator[str]:
        """The JSONL lines; the lease is checked once more after the last one.

        ``write_input`` stores the input only after reading every line, and the
        last bodies may take a while to finish: a task that lost the lease
        meanwhile must not overwrite the input of the one that holds it (BR2.3).
        """
        for item in items:
            yield item.to_json_line()
        self._check_lease()

    def _sampled_personas(
        self,
        survey: Survey,
        ref: PersonaDataRef,
        extra_columns: Optional[list[dict[str, str]]],
    ) -> pl.DataFrame:
        if self._runner.has_personas(survey.id):
            return self._runner.read_personas(survey.id)
        self._personas.ensure_dataset(ref.value, SurveyRunError)
        sampled = self._batch.filter_and_sample_personas(
            survey.filters or {}, survey.persona_count, datasource=ref.value
        )
        # The data may change between accepting and starting (BR1.4)
        SurveyPersonaSource.require_enough(len(sampled), survey.filters, SurveyRunError)
        personas = _kept_columns(sampled, extra_columns)
        self._check_lease()
        self._runner.write_personas(survey.id, personas)
        return personas

    def _items(
        self, survey_id: str, personas: pl.DataFrame, request: _Request
    ) -> Generator[SurveyRunItem, None, None]:
        """One item per persona, in sampling order, built as they are read.

        Bodies that go to S3 (images, or too large for the item, BR6.3) are
        stored in parallel while the input is written.
        """
        items = self._runner.write_bodies(
            survey_id, self._model_id, self._bodies(personas, request)
        )
        with closing(items):
            yield from items

    def _bodies(
        self, personas: pl.DataFrame, request: _Request
    ) -> Iterator[tuple[str, str, bool]]:
        """``(record_id, body_json, to_s3)`` per persona, each encoded once.

        The lease is checked before each persona (BR2.3), so no new body is
        sent after it is lost (the PUTs already running are waited for).
        """
        content = build_survey_user_content(
            request.questions_text, request.image_names, request.image_blocks
        )
        for row in personas.iter_rows(named=True):
            self._check_lease()
            body_json = self._runner.encode_body(
                self._runner.request_body(
                    build_persona_system_prompt(
                        row, extra_columns=request.extra_columns
                    ),
                    content,
                    request.schema,
                )
            )
            to_s3 = bool(request.image_blocks) or not self._runner.fits_inline(
                body_json
            )
            yield str(row["uuid"]), body_json, to_s3

    def _record_start(self, survey: Survey, info: Any) -> _Outcome:
        self._check_lease()
        started = self._surveys.transition(
            survey.id,
            expected=SurveyStatus.QUEUED.value,
            fields={
                "status": SurveyStatus.RUNNING.value,
                "execution_arn": info.execution_arn,
                "started_at": _aware(info.start_date).isoformat(),
                "model_id": self._model_id,
                "updated_at": _now_local(),
            },
            remove=("start_blocked_since",),
        )
        if not started:
            current = self._reread_after_lost_transition(survey.id, "start")
            if current is None:
                # Deleted while starting: stop what was started and clean up
                self._stop_quietly(survey.id, info.execution_arn, "deleted")
                self._cleanup_run(survey.id)
                return _Outcome.SKIPPED
            # Another task recorded the start first: its execution keeps running
            if current.status == SurveyStatus.RUNNING:
                return _Outcome.STARTED
            # Ended elsewhere meanwhile (e.g. another task's error): nobody
            # would watch this execution, so stop it
            self._stop_quietly(survey.id, info.execution_arn, "ended_elsewhere")
            return _Outcome.SKIPPED
        log_event(
            "survey.run_started",
            survey_id=survey.id,
            execution_arn=info.execution_arn,
            reused=info.reused,
        )
        return _Outcome.STARTED

    def _start_failed(self, survey: Survey, error: Exception) -> _Outcome:
        code = getattr(error, "code", None)
        if isinstance(code, ErrorCode) and is_lasting_failure(error):
            self._fail_lasting(survey, "start", code, error)
            return _Outcome.SKIPPED
        failure = _failure_of(error)
        if failure in (FailureClass.NOT_FOUND, FailureClass.INVALID):
            self._fail(survey, ErrorCode.SURVEY_EXECUTION_FAILED)
            return _Outcome.SKIPPED
        if failure is FailureClass.ACCESS:
            return self._start_blocked(survey, error)
        if self._retry_later(survey, "start_attempts", "start", error):
            return _Outcome.WAITING
        return _Outcome.SKIPPED

    def _fail_lasting(
        self, survey: Survey, stage: str, code: ErrorCode, error: Exception
    ) -> None:
        """A failure retrying cannot fix: its code if the survey's own, else generic."""
        if code in _SHOWN_CODES:
            self._fail(survey, code, getattr(error, "context", None) or None)
            return
        # Not shown on the result page; the log keeps what actually failed
        log_event(
            "survey.error_code_replaced",
            level="WARNING",
            survey_id=survey.id,
            stage=stage,
            error_code=code.value,
            error_type=type(error).__name__,
        )
        self._fail(survey, ErrorCode.SURVEY_EXECUTION_FAILED)

    def _start_blocked(self, survey: Survey, error: Exception) -> _Outcome:
        """NFR4.9・NFR4.9a: wait without counting; give up after 30 minutes."""
        since = survey.start_blocked_since
        now = self._clock()
        if since is not None and now > _aware(since) + BLOCKED_LIMIT:
            self._fail(survey, ErrorCode.SURVEY_RUNNER_UNAVAILABLE)
            return _Outcome.SKIPPED
        if since is None:
            self._check_lease()
            self._surveys.transition(
                survey.id,
                expected=SurveyStatus.QUEUED.value,
                fields={"start_blocked_since": now.isoformat()},
            )
        self._waiting(survey, "start", FailureClass.ACCESS, error)
        return _Outcome.WAITING

    # --- inputs of a start ------------------------------------------------------------

    def _extra_columns(
        self, survey_id: str, ref: PersonaDataRef
    ) -> Optional[list[dict[str, str]]]:
        """The custom data's columns from its record, by ID (BR2.2 (a))."""
        if ref.custom_id is None:
            return None
        store = self._custom_store or service_factory.get_custom_persona_data_store()
        record = store.get(ref.custom_id)
        present = record is not None and self._s3.exists(
            self._s3.path_of(record.s3_key)
        )
        if record is None or not present:
            _dependency_missing(survey_id, "custom_persona_data")
            raise SurveyRunError(
                "custom persona data is gone",
                code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND,
            )
        columns: list[dict[str, str]] = record.extra_column_dicts()
        return columns

    def _snapshot(self, survey: Survey) -> TemplateSnapshot:
        """The template copy; a survey before v1.0.0 reads its template."""
        if survey.template_snapshot is not None:
            return survey.template_snapshot
        store = self._templates or service_factory.get_survey_template_store()
        template = store.get(survey.template_id, consistent=True)
        if template is None:
            raise SurveyRunError(
                "survey template is gone", code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
            )
        return TemplateSnapshot.from_template(template)

    def _image_blocks(
        self, survey_id: str, snapshot: TemplateSnapshot
    ) -> Iterable[dict[str, Any]]:
        """The snapshot's images, compressed; any failure stops the start (BR2.2 (b))."""
        blocks: list[dict[str, Any]] = []
        for image in snapshot.images:
            try:
                raw = self._s3.download_file(image.file_path)
                compressed, media_type = compress_image_for_batch(raw)
            except Exception as e:
                _dependency_missing(survey_id, "image")
                raise SurveyRunError(
                    f"template image unreadable ({type(e).__name__})",
                    code=ErrorCode.SURVEY_TEMPLATE_IMAGE_UNAVAILABLE,
                ) from e
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(compressed).decode("utf-8"),
                    },
                }
            )
        return blocks

    # =========================================================================
    # 状態の書き込みとログ
    # =========================================================================

    def _fail(
        self,
        survey: Survey,
        code: ErrorCode,
        context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """``queued`` / ``running`` → ``error`` with the code (BR10.1 cleanup)."""
        self._check_lease()
        fields: dict[str, Any] = {
            "status": SurveyStatus.ERROR.value,
            "error_code": code.value,
            "updated_at": _now_local(),
        }
        remove: list[str] = ["run_queue"]
        if context:
            fields["error_context"] = dict(context)
        else:
            remove.append("error_context")
        failed = self._surveys.transition(
            survey.id, expected=survey.status, fields=fields, remove=tuple(remove)
        )
        if failed:
            log_event(
                "survey.execution_failed",
                level="ERROR",
                survey_id=survey.id,
                error_code=code.value,
                execution_arn=survey.execution_arn,
            )
        # A run another task started or finished meanwhile is not cleaned here
        if failed or self._lost_to_deletion(survey.id, "fail"):
            self._cleanup_run(survey.id)

    def _lost_to_deletion(self, survey_id: str, step: str) -> bool:
        """After a failed conditional write: ``True`` only when the record is gone."""
        return self._reread_after_lost_transition(survey_id, step) is None

    def _reread_after_lost_transition(
        self, survey_id: str, step: str
    ) -> Optional[Survey]:
        """Read the record strongly consistently after a transition did not hold.

        ``None`` means it was deleted; otherwise another task changed it first
        (NFR4.4) and what it wrote must be left alone.
        """
        current = self._surveys.get(survey_id, consistent=True)
        log_event(
            "survey.runner_transition_lost",
            level="WARNING",
            survey_id=survey_id,
            step=step,
            status="deleted" if current is None else str(current.status),
        )
        return current

    def _waiting(
        self, survey: Survey, stage: str, failure: FailureClass, error: Exception
    ) -> None:
        """A failure that keeps the survey as it is (NFR7.7: access is ERROR)."""
        log_event(
            "survey.runner_waiting",
            level="ERROR" if failure is FailureClass.ACCESS else "WARNING",
            survey_id=survey.id,
            execution_arn=survey.execution_arn,
            stage=stage,
            failure=failure.value,
            error_type=type(error).__name__,
        )

    def _cleanup_run(self, survey_id: str) -> None:
        try:
            self._runner.delete_run(survey_id)
        except Exception as e:
            # The lifecycle rule removes it after 7 days anyway (BR10.1)
            log_event(
                "survey.cleanup_failed",
                level="WARNING",
                resource_type="survey",
                resource_id=survey_id,
                step="run_location",
                error_type=type(e).__name__,
            )

    def _delete_quietly(self, survey_id: str, path: str) -> None:
        try:
            self._s3.delete_file(path)
        except Exception as e:
            log_event(
                "survey.cleanup_failed",
                level="WARNING",
                resource_type="survey",
                resource_id=survey_id,
                step="results",
                error_type=type(e).__name__,
            )


def _failure_of(error: BaseException) -> FailureClass:
    """The class carried by Service errors; anything else is transient."""
    failure = getattr(error, "failure", None)
    return failure if isinstance(failure, FailureClass) else FailureClass.TRANSIENT


def _kept_columns(
    sampled: pl.DataFrame, extra_columns: Optional[list[dict[str, str]]]
) -> pl.DataFrame:
    """``uuid``, the CSV attributes and the prompt's columns, in sampling order."""
    wanted = ["uuid", *PERSONA_ATTRIBUTE_COLUMNS, *PERSONA_PROFILE_COLUMNS]
    wanted += [c.get("csv_column", "") for c in extra_columns or []]
    kept = [c for c in dict.fromkeys(wanted) if c in sampled.columns]
    return sampled.select(kept).with_columns(pl.col("uuid").cast(pl.Utf8))


def _dependency_missing(survey_id: str, missing: str) -> None:
    log_event(
        "survey.dependency_missing",
        level="WARNING",
        survey_id=survey_id,
        missing=missing,
    )


def _aware(moment: datetime) -> datetime:
    """Records before v1.0.0 hold naive local times; treat them as UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _now_local() -> str:
    """``updated_at`` keeps the format the other survey writes use."""
    return datetime.now().isoformat()
