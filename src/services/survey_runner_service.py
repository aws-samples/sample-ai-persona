"""SurveyRunnerService: the Step Functions execution of a survey and its S3 run
location (functional-spec.md ch.2 and ch.6).

- Step Functions: ``StartExecution`` (named after the survey ID, so a restart
  never runs twice, BR3.1), ``DescribeExecution`` and ``StopExecution``.
- S3 ``survey-runs/<id>/``: the sampled personas (``personas.parquet``), the
  Distributed Map input (``input.jsonl``), one body per persona for surveys
  with images or large bodies (``bodies/<record_id>.json``, stored in
  parallel as the input is built), and the ResultWriter output under
  ``results/``, read one file at a time (NFR1.7).

Every SDK failure becomes :class:`SurveyRunnerServiceError` with one of the
four :class:`FailureClass` values (reliability-requirements.md 3.1); retries
and timeouts of single calls stay inside this layer (botocore ``standard``
retry mode). What to do with a failure is decided by ``SurveyRunManager``.
Other services are not imported.
"""

import dataclasses
import io
import json
import logging
import tempfile
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Any, Optional

import boto3
import polars as pl
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from ..models.errors import CodedError, ErrorCode
from ..models.external_failure import FailureClass, classify_aws_error

logger = logging.getLogger(__name__)

#: Where everything of one execution lives.
RUN_PREFIX = "survey-runs"
#: The Anthropic Messages body of one request (BR6.2).
ANTHROPIC_VERSION = "bedrock-2023-05-31"
MAX_TOKENS = 2048
#: Largest body kept in the Distributed Map item. A child execution's input is
#: at most 256 KiB; the rest is room for the item's other keys. Larger bodies
#: go to S3 (``body_s3_uri``) like those with images.
MAX_INLINE_BODY_BYTES = 200 * 1024
#: Bodies stored at once by :meth:`SurveyRunnerService.write_bodies`. A body
#: with an image is several MB; one at a time, 1,000 personas would take
#: minutes (longer than the 30 s the task waits for a round when stopping).
BODY_UPLOAD_CONCURRENCY = 16
#: Bodies taken ahead of the oldest unfinished one. Larger than the
#: concurrency, so one slow PUT does not hold the others; it also bounds the
#: bodies waiting in memory.
_BODY_LOOKAHEAD = 2 * BODY_UPLOAD_CONCURRENCY
#: The JSONL input stays in memory up to this size, then spills to a file.
_INPUT_SPOOL_BYTES = 8 * 1024 * 1024
#: ``DeleteObjects`` accepts at most 1000 keys.
_DELETE_BATCH = 1000
_BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=30,
    # botocore's default (10) would queue the parallel body uploads
    max_pool_connections=BODY_UPLOAD_CONCURRENCY,
)


class SurveyRunnerServiceError(CodedError):
    """A Step Functions or run-location call failed.

    Attributes:
        failure: How the caller should handle it (reliability 3.1).
    """

    code = ErrorCode.SURVEY_EXECUTION_FAILED

    def __init__(self, message: str, *, failure: FailureClass) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True)
class ExecutionInfo:
    """What ``StartExecution`` / ``DescribeExecution`` tell about an execution.

    Attributes:
        execution_arn: The execution.
        status: ``RUNNING``, ``SUCCEEDED``, ``FAILED``, ``TIMED_OUT``,
            ``ABORTED`` (or ``PENDING_REDRIVE``).
        start_date: When it started.
        manifest_key: The ResultWriter manifest of a finished execution.
        reused: The execution existed already (``ExecutionAlreadyExists``).
        stop_date: When a finished execution ended.
    """

    execution_arn: str
    status: str
    start_date: datetime
    manifest_key: Optional[str] = None
    reused: bool = False
    stop_date: Optional[datetime] = None


@dataclass(frozen=True)
class RunResult:
    """One persona's item in the ResultWriter output.

    Attributes:
        record_id: From the item's ``Input``; ``None`` when unreadable.
        succeeded: The child execution succeeded.
        text: The response text (first text block), when it succeeded.
        error: The Step Functions error name, when it failed.
    """

    record_id: Optional[str]
    succeeded: bool
    text: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class StoredBody:
    """One persona's body after :meth:`SurveyRunnerService.write_bodies`.

    Attributes:
        record_id: The persona.
        body_json: The encoded body, when it was not stored.
        body_s3_uri: Where it was stored, when it was.
    """

    record_id: str
    body_json: Optional[str] = None
    body_s3_uri: Optional[str] = None


@dataclass(frozen=True)
class _PendingBody:
    record_id: str
    body_json: Optional[str]
    upload: Optional["Future[None]"] = None
    uri: Optional[str] = None

    def ready(self) -> bool:
        return self.upload is None or self.upload.done()

    def settled(self) -> StoredBody:
        if self.upload is None:
            return StoredBody(record_id=self.record_id, body_json=self.body_json)
        self.upload.result()  # re-raises the PUT's SurveyRunnerServiceError
        return StoredBody(record_id=self.record_id, body_s3_uri=self.uri)


class SurveyRunnerService:
    """Step Functions and ``survey-runs/`` operations of the survey runner."""

    def __init__(
        self,
        *,
        state_machine_arn: str,
        bucket_name: str,
        region: str,
        sfn_client: Any = None,
        s3_client: Any = None,
    ) -> None:
        """Create the service.

        Args:
            state_machine_arn: ``SURVEY_STATE_MACHINE_ARN``.
            bucket_name: The app's bucket (``survey-runs/`` lives in it).
            region: AWS region.
            sfn_client: Optional pre-built Step Functions client (tests).
            s3_client: Optional pre-built S3 client (tests).
        """
        self.state_machine_arn = state_machine_arn
        self.bucket_name = bucket_name
        self._sfn = sfn_client or boto3.client(
            "stepfunctions", region_name=region, config=_BOTO_CONFIG
        )
        self._s3 = s3_client or boto3.client(
            "s3", region_name=region, config=_BOTO_CONFIG
        )

    # --- request format -------------------------------------------------------------

    @staticmethod
    def request_body(
        system_prompt: str,
        user_content: list[dict[str, Any]],
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        """One persona's InvokeModel body in the Anthropic Messages format (BR6.2)."""
        return {
            "anthropic_version": ANTHROPIC_VERSION,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            "output_config": {
                "format": {"type": "json_schema", "schema": dict(schema)}
            },
        }

    @staticmethod
    def encode_body(body: Mapping[str, Any]) -> str:
        """``body`` as one-line JSON, encoded once for the size check and the item."""
        return json.dumps(body, ensure_ascii=False)

    @staticmethod
    def fits_inline(body_json: str) -> bool:
        """Whether an encoded body may travel in the item (UTF-8, as in the JSONL)."""
        return len(body_json.encode("utf-8")) <= MAX_INLINE_BODY_BYTES

    # --- execution ARNs -------------------------------------------------------------

    def execution_arn_for(self, survey_id: str) -> str:
        """The ARN of the execution named ``survey_id`` of this state machine."""
        head, _, name = self.state_machine_arn.rpartition(":stateMachine:")
        return f"{head}:execution:{name}:{survey_id}"

    def is_own_execution(self, execution_arn: str) -> bool:
        """Whether ``execution_arn`` belongs to the current state machine.

        A state machine created again (rollback, logical ID change) has a new
        ARN; executions of the old one can no longer be followed (基盤設計 2.2).
        """
        prefix = self.execution_arn_for("")
        return execution_arn.startswith(prefix) and len(execution_arn) > len(prefix)

    # --- Step Functions -------------------------------------------------------------

    def start_execution(self, survey_id: str) -> ExecutionInfo:
        """Start the execution named after the survey (BR3.1, NFR5.2).

        The input is only the run location; the model and bodies are in the
        JSONL lines. An execution of that name that exists already is taken
        over (``reused``) instead of failing.
        """
        run_input = {
            "bucket": self.bucket_name,
            "input_key": self._key(survey_id, "input.jsonl"),
            "result_prefix": self._key(survey_id, "results"),
        }
        try:
            response = self._sfn.start_execution(
                stateMachineArn=self.state_machine_arn,
                name=survey_id,
                input=json.dumps(run_input),
            )
        except ClientError as e:
            if _code_of(e) != "ExecutionAlreadyExists":
                raise self._failure("StartExecution", e) from e
            info = self.describe_execution(self.execution_arn_for(survey_id))
            return ExecutionInfo(
                execution_arn=info.execution_arn,
                status=info.status,
                start_date=info.start_date,
                manifest_key=info.manifest_key,
                reused=True,
                stop_date=info.stop_date,
            )
        except BotoCoreError as e:
            raise self._failure("StartExecution", e) from e
        return ExecutionInfo(
            execution_arn=response["executionArn"],
            status="RUNNING",
            start_date=response["startDate"],
        )

    def find_execution(self, survey_id: str) -> Optional[ExecutionInfo]:
        """The survey's execution if it was started already, else ``None``.

        A task that stopped between ``StartExecution`` and recording the start
        leaves a running execution behind; it is taken over (``reused``).
        """
        try:
            info = self.describe_execution(self.execution_arn_for(survey_id))
        except SurveyRunnerServiceError as e:
            if e.failure is FailureClass.NOT_FOUND:
                return None
            raise
        return dataclasses.replace(info, reused=True)

    def describe_execution(self, execution_arn: str) -> ExecutionInfo:
        """The execution's status, start and (when finished) manifest key."""
        try:
            response = self._sfn.describe_execution(executionArn=execution_arn)
        except (ClientError, BotoCoreError) as e:
            raise self._failure("DescribeExecution", e) from e
        return ExecutionInfo(
            execution_arn=response["executionArn"],
            status=response["status"],
            start_date=response["startDate"],
            manifest_key=_manifest_key(response.get("output")),
            stop_date=response.get("stopDate"),
        )

    def stop_execution(self, execution_arn: str) -> None:
        """Stop a running execution."""
        try:
            self._sfn.stop_execution(
                executionArn=execution_arn, cause="survey deleted or timed out"
            )
        except (ClientError, BotoCoreError) as e:
            raise self._failure("StopExecution", e) from e

    # --- run location (S3) ----------------------------------------------------------

    def has_personas(self, survey_id: str) -> bool:
        """Whether the sampled personas were written already (BR1.5)."""
        try:
            self._s3.head_object(
                Bucket=self.bucket_name, Key=self._key(survey_id, "personas.parquet")
            )
            return True
        except (ClientError, BotoCoreError) as e:
            failure = self._failure("HeadObject", e)
            if failure.failure is FailureClass.NOT_FOUND:
                return False
            raise failure from e

    def write_personas(self, survey_id: str, personas: pl.DataFrame) -> None:
        """Write the sampled personas once, in sampling order (BR1.5)."""
        buffer = io.BytesIO()
        personas.write_parquet(buffer)
        self._put(self._key(survey_id, "personas.parquet"), buffer.getvalue())

    def read_personas(self, survey_id: str) -> pl.DataFrame:
        """The sampled personas written by :meth:`write_personas`."""
        raw = self._get(self._key(survey_id, "personas.parquet"))
        try:
            return pl.read_parquet(io.BytesIO(raw))
        except Exception as e:
            raise SurveyRunnerServiceError(
                f"personas.parquet unreadable ({type(e).__name__})",
                failure=FailureClass.INVALID,
            ) from e

    def write_bodies(
        self, survey_id: str, bodies: Iterable[tuple[str, str, bool]]
    ) -> Iterator[StoredBody]:
        """Store the marked bodies in parallel; yield every body in order (BR6.3).

        ``bodies`` is read one at a time in the caller's thread, so a check the
        caller makes while producing it runs before each body is stored. Up to
        :data:`BODY_UPLOAD_CONCURRENCY` PUTs run at once, and up to
        :data:`_BODY_LOOKAHEAD` bodies are taken ahead of the oldest one still
        being stored. A body that is not stored is yielded as soon as every
        body before it is.

        Args:
            bodies: ``(record_id, body_json, store)`` from :meth:`encode_body`;
                only those with ``store`` go to S3.

        Raises:
            SurveyRunnerServiceError: A body could not be stored (the first
                failure in order; the bodies not yet sent are dropped).
        """
        pending: deque[_PendingBody] = deque()
        pool = ThreadPoolExecutor(max_workers=BODY_UPLOAD_CONCURRENCY)
        try:
            for record_id, body_json, store in bodies:
                if store:
                    key = self._key(survey_id, f"bodies/{record_id}.json")
                    upload = pool.submit(self._put, key, body_json.encode("utf-8"))
                    uri = f"s3://{self.bucket_name}/{key}"
                    pending.append(_PendingBody(record_id, None, upload, uri))
                else:
                    pending.append(_PendingBody(record_id, body_json))
                while pending and (
                    len(pending) > _BODY_LOOKAHEAD or pending[0].ready()
                ):
                    yield pending.popleft().settled()
            while pending:
                yield pending.popleft().settled()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def write_input(self, survey_id: str, lines: Iterable[str]) -> str:
        """Write the Distributed Map input, one JSON line each (NFR1.6).

        The lines are consumed one at a time into a spooled file, so the whole
        input never sits in memory as one string.
        """
        key = self._key(survey_id, "input.jsonl")
        with tempfile.SpooledTemporaryFile(max_size=_INPUT_SPOOL_BYTES) as spool:
            for index, line in enumerate(lines):
                if index:
                    spool.write(b"\n")
                spool.write(line.encode("utf-8"))
            spool.seek(0)
            self._put(key, spool)
        return key

    def iter_results(self, manifest_key: str) -> Iterator[RunResult]:
        """Every item of the ResultWriter output, one result file at a time.

        Only the record ID, the response text and the error name are kept;
        the request bodies in ``Input`` are dropped as each file is read
        (NFR1.7).

        Raises:
            SurveyRunnerServiceError: A file is missing (``NOT_FOUND``) or not
                the documented shape (``INVALID``).
        """
        manifest = self._json(manifest_key)
        files = manifest.get("ResultFiles") if isinstance(manifest, dict) else None
        if not isinstance(files, dict):
            raise SurveyRunnerServiceError(
                "manifest has no ResultFiles", failure=FailureClass.INVALID
            )
        for status in ("SUCCEEDED", "FAILED", "PENDING"):
            for entry in files.get(status) or []:
                items = self._json(str(entry["Key"]))
                if not isinstance(items, list):
                    raise SurveyRunnerServiceError(
                        "result file is not a list", failure=FailureClass.INVALID
                    )
                for item in items:
                    yield _run_result(item)

    def delete_run(self, survey_id: str) -> None:
        """Remove everything under ``survey-runs/<id>/`` (BR10.1)."""
        prefix = self._key(survey_id, "")
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                for start in range(0, len(keys), _DELETE_BATCH):
                    self._s3.delete_objects(
                        Bucket=self.bucket_name,
                        Delete={
                            "Objects": keys[start : start + _DELETE_BATCH],
                            "Quiet": True,
                        },
                    )
        except (ClientError, BotoCoreError) as e:
            raise self._failure("DeleteObjects", e) from e

    # --- helpers --------------------------------------------------------------------

    @staticmethod
    def _key(survey_id: str, name: str) -> str:
        return f"{RUN_PREFIX}/{survey_id}/{name}"

    def _put(self, key: str, body: bytes | IO[bytes]) -> None:
        try:
            self._s3.put_object(Bucket=self.bucket_name, Key=key, Body=body)
        except (ClientError, BotoCoreError) as e:
            raise self._failure("PutObject", e) from e

    def _get(self, key: str) -> bytes:
        try:
            response = self._s3.get_object(Bucket=self.bucket_name, Key=key)
            return bytes(response["Body"].read())
        except (ClientError, BotoCoreError) as e:
            raise self._failure("GetObject", e) from e

    def _json(self, key: str) -> Any:
        raw = self._get(key)
        try:
            return json.loads(raw)
        except ValueError as e:
            raise SurveyRunnerServiceError(
                "result file is not JSON", failure=FailureClass.INVALID
            ) from e

    @staticmethod
    def _failure(operation: str, error: Exception) -> SurveyRunnerServiceError:
        if isinstance(error, ClientError):
            code = _code_of(error)
            failure = classify_aws_error(code)
        else:
            code = type(error).__name__
            failure = FailureClass.TRANSIENT
        logger.warning("%s failed: %s (%s)", operation, code, failure.value)
        return SurveyRunnerServiceError(f"{operation} failed: {code}", failure=failure)


def _code_of(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _manifest_key(output: Optional[str]) -> Optional[str]:
    """The ``ResultWriterDetails.Key`` of a finished execution's output."""
    if not output:
        return None
    try:
        details = json.loads(output).get("ResultWriterDetails") or {}
    except (ValueError, AttributeError):
        return None
    key = details.get("Key") if isinstance(details, dict) else None
    return str(key) if key else None


def _run_result(item: Mapping[str, Any]) -> RunResult:
    """One ResultWriter entry: ``Input`` / ``Output`` are JSON strings."""
    record_id = _record_id(item.get("Input"))
    if item.get("Status") != "SUCCEEDED":
        return RunResult(
            record_id=record_id, succeeded=False, error=str(item.get("Error") or "")
        )
    return RunResult(
        record_id=record_id, succeeded=True, text=_text(item.get("Output"))
    )


def _record_id(raw: Any) -> Optional[str]:
    try:
        value = json.loads(raw).get("record_id") if isinstance(raw, str) else None
    except (ValueError, AttributeError):
        return None
    return str(value) if value else None


def _text(raw: Any) -> Optional[str]:
    """The first text block of the response kept by ``ResultSelector``."""
    try:
        content = json.loads(raw).get("content") if isinstance(raw, str) else None
    except (ValueError, AttributeError):
        return None
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", ""))
    return None
