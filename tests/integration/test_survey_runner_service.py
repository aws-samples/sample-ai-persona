"""Integration tests for SurveyRunnerService (Step Functions executions and the survey-runs/ S3 prefix).

S3 uses moto; Step Functions gets a ``unittest.mock`` client.

- The execution name is the Survey id, and the input is only the S3 location
- ``ExecutionAlreadyExists`` returns the existing execution (the ARN is built from the name)
- SDK errors are split into four kinds
- A Body with images is one file per person, and result files are read one at a time
- The execution location is cleaned up
"""

import io
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import polars as pl
import pytest

moto = pytest.importorskip("moto", reason="moto is required for S3 tests")

import boto3  # noqa: E402
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

from src.models.external_failure import FailureClass  # noqa: E402
from src.models.survey_run import SurveyRunItem  # noqa: E402
from src.services.survey_runner_service import (  # noqa: E402
    _BOTO_CONFIG,
    BODY_UPLOAD_CONCURRENCY,
    MAX_INLINE_BODY_BYTES,
    SurveyRunnerService,
    SurveyRunnerServiceError,
)

REGION = "ap-northeast-1"
BUCKET = "runs-bucket"
SM_ARN = "arn:aws:states:ap-northeast-1:123456789012:stateMachine:SurveyRunSM-abc"
SURVEY_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
EXEC_ARN = (
    f"arn:aws:states:ap-northeast-1:123456789012:execution:SurveyRunSM-abc:{SURVEY_ID}"
)
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "survey_runs"
STARTED = datetime(2026, 10, 30, 1, 0, 0, tzinfo=timezone.utc)


def _client_error(code: str, status: int = 400, op: str = "Op") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "m"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        op,
    )


@pytest.fixture
def s3(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield client


@pytest.fixture
def sfn() -> Mock:
    client = Mock()
    client.start_execution.return_value = {
        "executionArn": EXEC_ARN,
        "startDate": STARTED,
    }
    client.describe_execution.return_value = {
        "executionArn": EXEC_ARN,
        "status": "RUNNING",
        "startDate": STARTED,
    }
    return client


@pytest.fixture
def service(s3, sfn) -> SurveyRunnerService:
    return SurveyRunnerService(
        state_machine_arn=SM_ARN,
        bucket_name=BUCKET,
        region=REGION,
        sfn_client=sfn,
        s3_client=s3,
    )


def _keys(s3: Any, prefix: str = "") -> list[str]:
    response = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return sorted(o["Key"] for o in response.get("Contents", []))


class TestStartExecution:
    def test_name_is_the_survey_id_and_input_is_the_run_location(self, service, sfn):
        """User input appears in neither the name nor the input."""
        info = service.start_execution(SURVEY_ID)

        kwargs = sfn.start_execution.call_args.kwargs
        assert kwargs["stateMachineArn"] == SM_ARN
        assert kwargs["name"] == SURVEY_ID
        assert json.loads(kwargs["input"]) == {
            "bucket": BUCKET,
            "input_key": f"survey-runs/{SURVEY_ID}/input.jsonl",
            "result_prefix": f"survey-runs/{SURVEY_ID}/results",
        }
        assert info.execution_arn == EXEC_ARN
        assert info.start_date == STARTED
        assert not info.reused

    def test_already_exists_takes_over_the_existing_execution(self, service, sfn):
        """Build the ARN from the name and return that execution's status and start time."""
        sfn.start_execution.side_effect = _client_error("ExecutionAlreadyExists")
        sfn.describe_execution.return_value = {
            "executionArn": EXEC_ARN,
            "status": "SUCCEEDED",
            "startDate": STARTED,
        }

        info = service.start_execution(SURVEY_ID)

        sfn.describe_execution.assert_called_once_with(executionArn=EXEC_ARN)
        assert info.reused
        assert info.execution_arn == EXEC_ARN
        assert info.status == "SUCCEEDED"

    def test_access_denied_is_access(self, service, sfn):
        sfn.start_execution.side_effect = _client_error("AccessDeniedException")

        with pytest.raises(SurveyRunnerServiceError) as info:
            service.start_execution(SURVEY_ID)
        assert info.value.failure is FailureClass.ACCESS


class TestExecutionArns:
    def test_execution_arn_is_built_from_the_state_machine(self, service):
        assert service.execution_arn_for(SURVEY_ID) == EXEC_ARN

    def test_own_execution(self, service):
        assert service.is_own_execution(EXEC_ARN)

    @pytest.mark.parametrize(
        "arn",
        [
            EXEC_ARN.replace("SurveyRunSM-abc", "SurveyRunSM-old"),
            "arn:aws:states:us-east-1:123456789012:execution:SurveyRunSM-abc:x",
            "not-an-arn",
        ],
    )
    def test_other_state_machine_is_not_own(self, service, arn):
        """If the state machine is recreated, an old ARN is no longer ours."""
        assert not service.is_own_execution(arn)


class TestDescribeAndStop:
    def test_running(self, service):
        info = service.describe_execution(EXEC_ARN)

        assert info.status == "RUNNING"
        assert info.start_date == STARTED
        assert info.manifest_key is None

    def test_succeeded_reads_the_manifest_key_from_the_output(self, service, sfn):
        key = f"survey-runs/{SURVEY_ID}/results/abc/manifest.json"
        sfn.describe_execution.return_value = {
            "executionArn": EXEC_ARN,
            "status": "SUCCEEDED",
            "startDate": STARTED,
            "output": json.dumps(
                {
                    "MapRunArn": "arn:x",
                    "ResultWriterDetails": {"Bucket": BUCKET, "Key": key},
                }
            ),
        }

        assert service.describe_execution(EXEC_ARN).manifest_key == key

    def test_finished_execution_has_its_stop_date(self, service, sfn):
        """A permission failure on the completion summary is counted from the time the execution ended."""
        stopped = datetime(2026, 10, 30, 1, 5, tzinfo=timezone.utc)
        sfn.describe_execution.return_value = {
            "executionArn": EXEC_ARN,
            "status": "FAILED",
            "startDate": STARTED,
            "stopDate": stopped,
        }

        info = service.describe_execution(EXEC_ARN)
        assert info.stop_date == stopped
        assert service.describe_execution(EXEC_ARN).status == "FAILED"

    def test_running_has_no_stop_date(self, service):
        assert service.describe_execution(EXEC_ARN).stop_date is None

    def test_stop(self, service, sfn):
        service.stop_execution(EXEC_ARN)

        sfn.stop_execution.assert_called_once()
        assert sfn.stop_execution.call_args.kwargs["executionArn"] == EXEC_ARN


class TestFailureClassification:
    """The SDK error classification table (anything unclassified is transient)."""

    @pytest.mark.parametrize(
        ("code", "failure"),
        [
            ("ExecutionDoesNotExist", FailureClass.NOT_FOUND),
            ("InvalidArn", FailureClass.NOT_FOUND),
            ("InvalidExecutionInput", FailureClass.INVALID),
            ("InvalidName", FailureClass.INVALID),
            ("AccessDeniedException", FailureClass.ACCESS),
            ("StateMachineDoesNotExist", FailureClass.ACCESS),
            ("StateMachineDeleting", FailureClass.ACCESS),
            ("UnrecognizedClientException", FailureClass.ACCESS),
            ("ExpiredToken", FailureClass.ACCESS),
            ("InvalidClientTokenId", FailureClass.ACCESS),
            ("ThrottlingException", FailureClass.TRANSIENT),
            ("ServiceUnavailable", FailureClass.TRANSIENT),
            ("SomethingNobodyKnows", FailureClass.TRANSIENT),
        ],
    )
    def test_step_functions_codes(self, service, sfn, code, failure):
        sfn.describe_execution.side_effect = _client_error(code)

        with pytest.raises(SurveyRunnerServiceError) as info:
            service.describe_execution(EXEC_ARN)
        assert info.value.failure is failure

    def test_server_errors_are_transient(self, service, sfn):
        sfn.describe_execution.side_effect = _client_error("InternalFailure", 500)

        with pytest.raises(SurveyRunnerServiceError) as info:
            service.describe_execution(EXEC_ARN)
        assert info.value.failure is FailureClass.TRANSIENT

    def test_connection_failures_are_transient(self, service, sfn):
        sfn.describe_execution.side_effect = EndpointConnectionError(endpoint_url="x")

        with pytest.raises(SurveyRunnerServiceError) as info:
            service.describe_execution(EXEC_ARN)
        assert info.value.failure is FailureClass.TRANSIENT

    @pytest.mark.parametrize(
        ("code", "failure"),
        [
            ("NoSuchKey", FailureClass.NOT_FOUND),
            ("404", FailureClass.NOT_FOUND),
            ("NotFound", FailureClass.NOT_FOUND),
            ("NoSuchBucket", FailureClass.ACCESS),
            ("AccessDenied", FailureClass.ACCESS),
            ("403", FailureClass.ACCESS),
            ("KMS.AccessDeniedException", FailureClass.ACCESS),
            ("KMS.DisabledException", FailureClass.ACCESS),
            ("SlowDown", FailureClass.TRANSIENT),
        ],
    )
    def test_s3_codes(self, sfn, code, failure):
        s3 = Mock()
        s3.get_object.side_effect = _client_error(code)
        service = SurveyRunnerService(
            state_machine_arn=SM_ARN,
            bucket_name=BUCKET,
            region=REGION,
            sfn_client=sfn,
            s3_client=s3,
        )

        with pytest.raises(SurveyRunnerServiceError) as info:
            service.read_personas(SURVEY_ID)
        assert info.value.failure is failure


class TestPersonasFile:
    """Extracted people are written once and read afterwards."""

    def test_write_then_read_keeps_rows_and_order(self, service, s3):
        df = pl.DataFrame({"uuid": ["b", "a"], "sex": ["女", "男"], "age": [30, 40]})

        assert not service.has_personas(SURVEY_ID)
        service.write_personas(SURVEY_ID, df)

        assert service.has_personas(SURVEY_ID)
        assert _keys(s3) == [f"survey-runs/{SURVEY_ID}/personas.parquet"]
        assert service.read_personas(SURVEY_ID).to_dicts() == df.to_dicts()

    def test_missing_file_is_not_found(self, service):
        with pytest.raises(SurveyRunnerServiceError) as info:
            service.read_personas(SURVEY_ID)
        assert info.value.failure is FailureClass.NOT_FOUND


class TestInputAndBodies:
    def test_input_is_one_json_line_per_item(self, service, s3):
        key = service.write_input(
            SURVEY_ID,
            (
                line
                for line in [
                    '{"record_id": "p1", "model_id": "m", "body": {"x": "あ"}}',
                    '{"record_id": "p2", "model_id": "m", "body_s3_uri": "s3://b/k"}',
                ]
            ),
        )

        assert key == f"survey-runs/{SURVEY_ID}/input.jsonl"
        text = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
        lines = [json.loads(line) for line in text.splitlines()]
        assert [line["record_id"] for line in lines] == ["p1", "p2"]
        assert lines[0]["body"] == {"x": "あ"}

    def test_input_is_streamed_not_built_in_memory(self, s3, sfn):
        """Send 10,000 people's JSONL as a file, not as one string."""
        sent: dict[str, Any] = {}
        client = Mock(wraps=s3)

        def put_object(**kwargs: Any) -> Any:
            sent["body_type"] = type(kwargs["Body"])
            return s3.put_object(**kwargs)

        client.put_object.side_effect = put_object
        service = SurveyRunnerService(
            state_machine_arn=SM_ARN,
            bucket_name=BUCKET,
            region=REGION,
            sfn_client=sfn,
            s3_client=client,
        )

        service.write_input(SURVEY_ID, (f'{{"record_id": "p{i}"}}' for i in range(3)))

        assert sent["body_type"] not in (bytes, str)

    def test_bodies_are_one_file_per_persona(self, service, s3):
        """A Body with images is placed in S3 and the item holds only the URI."""
        stored = list(
            service.write_bodies(
                SURVEY_ID,
                "m",
                [("p1", '{"messages": []}', True), ("p2", '{"messages": [1]}', True)],
            )
        )

        keys = [f"survey-runs/{SURVEY_ID}/bodies/{r}.json" for r in ("p1", "p2")]
        assert stored == [
            SurveyRunItem(record_id=r, model_id="m", body_s3_uri=f"s3://{BUCKET}/{k}")
            for r, k in zip(("p1", "p2"), keys)
        ]
        assert json.loads(s3.get_object(Bucket=BUCKET, Key=keys[1])["Body"].read()) == {
            "messages": [1]
        }

    def test_bodies_kept_inline_are_returned_in_order(self, service, s3):
        """A Body not placed in S3 is returned as is, and the whole keeps the original order."""
        stored = list(
            service.write_bodies(
                SURVEY_ID,
                "m",
                [("p1", "{}", False), ("p2", "{}", True), ("p3", "[]", False)],
            )
        )

        assert [b.record_id for b in stored] == ["p1", "p2", "p3"]
        assert stored[0] == SurveyRunItem(record_id="p1", model_id="m", body_json="{}")
        assert stored[1].body_s3_uri is not None
        assert stored[2] == SurveyRunItem(record_id="p3", model_id="m", body_json="[]")
        assert _keys(s3, f"survey-runs/{SURVEY_ID}/bodies/") == [
            f"survey-runs/{SURVEY_ID}/bodies/p2.json"
        ]

    def test_an_inline_body_does_not_wait_for_a_stored_one(self, sfn):
        """The leading Bodies not placed in S3 return without waiting for later uploads."""
        release = threading.Event()
        client = Mock()
        client.put_object.side_effect = lambda **kwargs: release.wait(2)
        service = _service_with(sfn, client)

        stored = service.write_bodies(
            SURVEY_ID, "m", iter([("p1", "{}", False), ("p2", "{}", True)])
        )
        began = time.monotonic()
        first = next(stored)

        assert first.record_id == "p1"
        assert time.monotonic() - began < 0.5
        release.set()
        assert next(stored).record_id == "p2"

    def test_bodies_are_written_in_parallel(self, sfn):
        """1,000 people with images are not sent one by one in order (so a deploy stop does not wait too long)."""
        lock = threading.Lock()
        state = {"now": 0, "most": 0}

        def put_object(**kwargs: Any) -> None:
            with lock:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1

        client = Mock()
        client.put_object.side_effect = put_object
        service = _service_with(sfn, client)

        list(
            service.write_bodies(
                SURVEY_ID, "m", [(f"p{i}", "{}", True) for i in range(16)]
            )
        )

        assert client.put_object.call_count == 16
        assert state["most"] > 1

    def test_one_slow_body_does_not_hold_the_others(self, sfn):
        """One slow PUT does not stop the other Bodies from being sent (no waiting per batch)."""
        lock = threading.Lock()
        done = {"others": 0, "before_slow": -1}

        def put_object(**kwargs: Any) -> None:
            if kwargs["Key"].endswith("/p0.json"):
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with lock:
                        if done["others"] >= 20:
                            break
                    time.sleep(0.005)
                with lock:
                    done["before_slow"] = done["others"]
                return
            with lock:
                done["others"] += 1

        client = Mock()
        client.put_object.side_effect = put_object
        service = _service_with(sfn, client)

        stored = list(
            service.write_bodies(
                SURVEY_ID, "m", [(f"p{i}", "{}", True) for i in range(40)]
            )
        )

        assert done["before_slow"] >= 20
        assert [b.record_id for b in stored] == [f"p{i}" for i in range(40)]

    def test_a_failure_behind_a_slow_body_stops_the_sending(self, sfn):
        """Even if the first PUT is stalled, the next Body is not sent once a later failure is noticed."""
        p1_failed = threading.Event()
        pulled: list[int] = []

        def put_object(**kwargs: Any) -> None:
            if kwargs["Key"].endswith("/p0.json"):
                time.sleep(1)  # still running when p1 fails; released by itself
            elif kwargs["Key"].endswith("/p1.json"):
                p1_failed.set()
                raise _client_error("AccessDenied", 403)

        def bodies() -> Any:
            for i in range(40):
                if i == 2:
                    # The order is fixed by the event, not by how long it takes
                    assert p1_failed.wait(5)
                pulled.append(i)
                yield f"p{i}", "{}", True

        client = Mock()
        client.put_object.side_effect = put_object
        service = _service_with(sfn, client)

        with pytest.raises(SurveyRunnerServiceError) as info:
            list(service.write_bodies(SURVEY_ID, "m", bodies()))

        assert info.value.failure is FailureClass.ACCESS
        # Without the check it would take 32 ahead of the stalled p0
        assert len(pulled) < 10
        assert 39 not in pulled

    def test_a_full_disk_while_writing_the_input_is_a_service_error(self, sfn):
        """A temp-file write failure (disk full) does not surface as a raw OSError."""
        service = _service_with(sfn, Mock())

        class FullSpool(io.BytesIO):
            def write(self, data: Any) -> int:
                raise OSError(28, "No space left on device")

        with patch(
            "src.services.survey_runner_service.tempfile.SpooledTemporaryFile",
            lambda **kwargs: FullSpool(),
        ):
            with pytest.raises(SurveyRunnerServiceError) as info:
                service.write_input(SURVEY_ID, iter(['{"record_id": "p1"}']))

        assert info.value.failure is FailureClass.TRANSIENT

    def test_an_error_from_the_lines_passes_through(self, service):
        """Exceptions from the row-building side (such as a lost assignment) pass through unchanged."""

        class LeaseLost(Exception):
            pass

        def lines() -> Any:
            yield '{"record_id": "p1"}'
            raise LeaseLost()

        with pytest.raises(LeaseLost):
            service.write_input(SURVEY_ID, lines())

    def test_a_failed_body_is_classified(self, sfn):
        client = Mock()
        client.put_object.side_effect = _client_error("AccessDenied", 403)
        service = _service_with(sfn, client)

        with pytest.raises(SurveyRunnerServiceError) as info:
            list(
                service.write_bodies(
                    SURVEY_ID, "m", [("p1", "{}", True), ("p2", "{}", True)]
                )
            )
        assert info.value.failure is FailureClass.ACCESS

    def test_connection_pool_fits_the_parallel_writes(self):
        """Parallel writes are not made to wait by botocore's default connection limit (10)."""
        assert _BOTO_CONFIG.max_pool_connections >= BODY_UPLOAD_CONCURRENCY


def _service_with(sfn: Any, s3_client: Any) -> SurveyRunnerService:
    return SurveyRunnerService(
        state_machine_arn=SM_ARN,
        bucket_name=BUCKET,
        region=REGION,
        sfn_client=sfn,
        s3_client=s3_client,
    )


def _place_results(s3: Any) -> str:
    prefix = f"survey-runs/{SURVEY_ID}/results"
    manifest = (FIXTURES / "manifest.json").read_text(encoding="utf-8")
    manifest = manifest.replace("<prefix>", prefix).replace("<bucket>", BUCKET)
    run = "0b2c4e1a-1111-2222-3333-444455556666"
    manifest_key = f"{prefix}/{run}/manifest.json"
    s3.put_object(Bucket=BUCKET, Key=manifest_key, Body=manifest.encode("utf-8"))
    for name in ("SUCCEEDED_0.json", "FAILED_0.json"):
        body = (FIXTURES / name).read_text(encoding="utf-8").replace("<bucket>", BUCKET)
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}/{run}/{name}", Body=body.encode())
    return manifest_key


class TestResults:
    """Read the ResultWriter output."""

    def test_reads_succeeded_and_failed_items(self, service, s3):
        manifest_key = _place_results(s3)

        results = {r.record_id: r for r in service.iter_results(manifest_key)}

        assert set(results) == {"p-1", "p-2", "p-3", "p-4"}
        assert results["p-1"].succeeded
        assert json.loads(results["p-1"].text)["answers"]["q1"] == "A"
        assert json.loads(results["p-2"].text)["answers"]["q3"] == 2
        assert results["p-3"].succeeded
        assert results["p-3"].text == "これはJSONではない"
        assert not results["p-4"].succeeded
        assert results["p-4"].error == "Bedrock.ThrottlingException"
        assert results["p-4"].text is None

    def test_reads_one_result_file_at_a_time(self, service, s3):
        """Read files one at a time, not all at once."""
        manifest_key = _place_results(s3)
        reads: list[str] = []
        original = s3.get_object

        def tracking_get_object(**kwargs: Any) -> Any:
            reads.append(kwargs["Key"].rsplit("/", 1)[-1])
            return original(**kwargs)

        service._s3 = Mock(wraps=s3)
        service._s3.get_object = tracking_get_object
        iterator = service.iter_results(manifest_key)

        first = next(iterator)
        assert first.record_id == "p-1"
        assert reads == ["manifest.json", "SUCCEEDED_0.json"]
        list(iterator)
        assert reads == ["manifest.json", "SUCCEEDED_0.json", "FAILED_0.json"]

    def test_missing_manifest_is_not_found(self, service):
        with pytest.raises(SurveyRunnerServiceError) as info:
            list(
                service.iter_results(f"survey-runs/{SURVEY_ID}/results/x/manifest.json")
            )
        assert info.value.failure is FailureClass.NOT_FOUND

    def test_broken_result_file_is_invalid(self, service, s3):
        manifest_key = _place_results(s3)
        prefix = manifest_key.rsplit("/", 1)[0]
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}/SUCCEEDED_0.json", Body=b"{broken")

        with pytest.raises(SurveyRunnerServiceError) as info:
            list(service.iter_results(manifest_key))
        assert info.value.failure is FailureClass.INVALID


class TestCleanup:
    def test_removes_everything_under_the_run_location(self, service, s3):
        """Delete extracted people, inputs, Bodies and results without touching other surveys."""
        service.write_personas(SURVEY_ID, pl.DataFrame({"uuid": ["p1"]}))
        service.write_input(SURVEY_ID, ['{"record_id": "p1", "model_id": "m"}'])
        list(service.write_bodies(SURVEY_ID, "m", [("p1", "{}", True)]))
        _place_results(s3)
        s3.put_object(Bucket=BUCKET, Key="survey-runs/other/input.jsonl", Body=b"x")
        s3.put_object(Bucket=BUCKET, Key="survey-results/x/results.csv", Body=b"x")

        service.delete_run(SURVEY_ID)

        assert _keys(s3, f"survey-runs/{SURVEY_ID}/") == []
        assert _keys(s3) == [
            "survey-results/x/results.csv",
            "survey-runs/other/input.jsonl",
        ]

    def test_nothing_to_remove_is_fine(self, service):
        service.delete_run(SURVEY_ID)


class TestFindExecution:
    """An execution started before a restart is found before starting a new one."""

    def test_existing_execution_is_returned_as_reused(self, service, sfn):
        info = service.find_execution(SURVEY_ID)

        sfn.describe_execution.assert_called_once_with(executionArn=EXEC_ARN)
        assert info is not None
        assert info.reused
        assert info.start_date == STARTED

    def test_no_execution_is_none(self, service, sfn):
        sfn.describe_execution.side_effect = _client_error("ExecutionDoesNotExist")

        assert service.find_execution(SURVEY_ID) is None

    def test_other_failures_are_raised(self, service, sfn):
        sfn.describe_execution.side_effect = _client_error("AccessDeniedException")

        with pytest.raises(SurveyRunnerServiceError) as info:
            service.find_execution(SURVEY_ID)
        assert info.value.failure is FailureClass.ACCESS


class TestFitsInline:
    """Decide whether to put the Body in the item, leaving headroom under the child execution input limit (256 KiB)."""

    @staticmethod
    def _encoded(size: int) -> str:
        return "a" * size

    def test_up_to_the_limit_fits(self):
        assert SurveyRunnerService.fits_inline(self._encoded(MAX_INLINE_BODY_BYTES))

    def test_over_the_limit_does_not_fit(self):
        assert not SurveyRunnerService.fits_inline(
            self._encoded(MAX_INLINE_BODY_BYTES + 1)
        )

    def test_size_is_counted_in_utf8_bytes(self):
        assert not SurveyRunnerService.fits_inline(
            "あ" * (MAX_INLINE_BODY_BYTES // 3 + 1)
        )

    def test_limit_leaves_room_below_256_kib(self):
        assert MAX_INLINE_BODY_BYTES <= 256 * 1024 - 16 * 1024


def test_encode_body_escapes_the_unicode_line_separators():
    """U+2028 and U+2029 can appear raw in a JSON string, but some readers treat them as line separators."""
    body = {"system": "段落\u2028改行\u2029終わり"}

    encoded = SurveyRunnerService.encode_body(body)

    assert "\u2028" not in encoded and "\u2029" not in encoded
    assert len(encoded.splitlines()) == 1
    assert json.loads(encoded) == body


def test_a_failure_reading_the_input_file_is_a_service_error(sfn):
    """An OSError raised while PUT reads the temp file is not exposed raw either."""
    client = Mock()
    client.put_object.side_effect = OSError(5, "Input/output error")
    service = _service_with(sfn, client)

    with pytest.raises(SurveyRunnerServiceError) as info:
        service.write_input(SURVEY_ID, iter(['{"record_id": "p1"}']))

    assert info.value.failure is FailureClass.TRANSIENT


def test_encode_body_keeps_non_ascii_and_is_one_line():
    encoded = SurveyRunnerService.encode_body({"system": "改行\nあり"})
    assert "\n" not in encoded
    assert "改行" in encoded
    assert json.loads(encoded) == {"system": "改行\nあり"}


def test_request_body_is_the_anthropic_messages_format():
    """anthropic_version, max_tokens 2048, system, messages, output_config."""
    schema = {"type": "object"}
    content = [{"type": "text", "text": "設問"}]

    body = SurveyRunnerService.request_body("ペルソナ", content, schema)

    assert body == {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "system": "ペルソナ",
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }


def test_parquet_bytes_are_readable_by_polars(service, s3):
    """The file is a plain Parquet file (polars reads it back without the service)."""
    service.write_personas(SURVEY_ID, pl.DataFrame({"uuid": ["p1"], "age": [20]}))

    raw = s3.get_object(Bucket=BUCKET, Key=f"survey-runs/{SURVEY_ID}/personas.parquet")
    assert pl.read_parquet(io.BytesIO(raw["Body"].read()))["uuid"].to_list() == ["p1"]
