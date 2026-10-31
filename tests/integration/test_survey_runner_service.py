"""SurveyRunnerService（Step Functions の実行と survey-runs/ の S3）の結合テスト。

S3 は moto、Step Functions は ``unittest.mock`` のクライアントを渡す（FR10.2、NFR8.4）。

- NFR5.2: 実行の名前は Survey の id、入力は S3 の場所だけ
- BR3.1: ``ExecutionAlreadyExists`` は既存の実行を返す（名前から ARN を組み立てる）
- reliability-requirements.md 3.1: SDK のエラーを 4 種類に分ける
- NFR3.14・NFR1.7: 画像ありの Body は 1 人 1 ファイル、結果のファイルは 1 つずつ読む
- BR10.1: 実行の場所を片付ける
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
from src.services.survey_runner_service import (  # noqa: E402
    _BOTO_CONFIG,
    BODY_UPLOAD_CONCURRENCY,
    MAX_INLINE_BODY_BYTES,
    StoredBody,
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
        """NFR5.2: 利用者の入力は名前にも入力にも入らない。"""
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
        """BR3.1: 名前から ARN を組み立てて、その実行の状態と開始時刻を返す。"""
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
        """基盤設計 2.2: ステートマシンが作り直されたら古い ARN は自分のものではない。"""
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
        """R-12: 完了のまとめの権限の失敗は、実行が終わった時刻から数える。"""
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
    """reliability-requirements.md 3.1 の表（分類できないものは一時的）。"""

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
    """BR1.5: 抽出した人は 1 度だけ書き、以後は読む。"""

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
        """NFR1.6: 1 万人分の JSONL を 1 つの文字列にせず、ファイルとして送る。"""
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
        """NFR3.14: 画像ありの Body は S3 に置き、URI だけを項目に持たせる。"""
        stored = list(
            service.write_bodies(
                SURVEY_ID,
                [("p1", '{"messages": []}', True), ("p2", '{"messages": [1]}', True)],
            )
        )

        keys = [f"survey-runs/{SURVEY_ID}/bodies/{r}.json" for r in ("p1", "p2")]
        assert stored == [
            StoredBody(record_id=r, body_s3_uri=f"s3://{BUCKET}/{k}")
            for r, k in zip(("p1", "p2"), keys)
        ]
        assert json.loads(s3.get_object(Bucket=BUCKET, Key=keys[1])["Body"].read()) == {
            "messages": [1]
        }

    def test_bodies_kept_inline_are_returned_in_order(self, service, s3):
        """S3 に置かない Body はそのまま返し、全体はもとの順に並ぶ。"""
        stored = list(
            service.write_bodies(
                SURVEY_ID,
                [("p1", "{}", False), ("p2", "{}", True), ("p3", "[]", False)],
            )
        )

        assert [b.record_id for b in stored] == ["p1", "p2", "p3"]
        assert stored[0] == StoredBody(record_id="p1", body_json="{}")
        assert stored[1].body_s3_uri is not None
        assert stored[2] == StoredBody(record_id="p3", body_json="[]")
        assert _keys(s3, f"survey-runs/{SURVEY_ID}/bodies/") == [
            f"survey-runs/{SURVEY_ID}/bodies/p2.json"
        ]

    def test_an_inline_body_does_not_wait_for_a_stored_one(self, sfn):
        """先頭の S3 に置かない Body は、あとの送信を待たずに返る。"""
        release = threading.Event()
        client = Mock()
        client.put_object.side_effect = lambda **kwargs: release.wait(2)
        service = _service_with(sfn, client)

        stored = service.write_bodies(
            SURVEY_ID, iter([("p1", "{}", False), ("p2", "{}", True)])
        )
        began = time.monotonic()
        first = next(stored)

        assert first.record_id == "p1"
        assert time.monotonic() - began < 0.5
        release.set()
        assert next(stored).record_id == "p2"

    def test_bodies_are_written_in_parallel(self, sfn):
        """画像ありの 1,000 人を 1 件ずつ順に送らない（デプロイの停止待ちを超えない）。"""
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
            service.write_bodies(SURVEY_ID, [(f"p{i}", "{}", True) for i in range(16)])
        )

        assert client.put_object.call_count == 16
        assert state["most"] > 1

    def test_one_slow_body_does_not_hold_the_others(self, sfn):
        """遅い PUT が 1 件あっても、ほかの Body は送り続ける（まとまりごとに待たない）。"""
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
            service.write_bodies(SURVEY_ID, [(f"p{i}", "{}", True) for i in range(40)])
        )

        assert done["before_slow"] >= 20
        assert [b.record_id for b in stored] == [f"p{i}" for i in range(40)]

    def test_a_failure_behind_a_slow_body_stops_the_sending(self, sfn):
        """先頭の PUT が止まっていても、後ろの失敗に気づいたら次の Body を送らない。"""
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
            list(service.write_bodies(SURVEY_ID, bodies()))

        assert info.value.failure is FailureClass.ACCESS
        # Without the check it would take 32 ahead of the stalled p0
        assert len(pulled) < 10
        assert 39 not in pulled

    def test_a_full_disk_while_writing_the_input_is_a_service_error(self, sfn):
        """一時ファイルの書き込みの失敗（ディスクがいっぱい）は、生の OSError にしない。"""
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
        """行を作る側の例外（担当の喪失など）は、そのまま通す。"""

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
                    SURVEY_ID, [("p1", "{}", True), ("p2", "{}", True)]
                )
            )
        assert info.value.failure is FailureClass.ACCESS

    def test_connection_pool_fits_the_parallel_writes(self):
        """botocore の既定の接続の上限（10）で並列の書き込みが待たされないこと。"""
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
    """functional-spec.md 6 章の ResultWriter の出力を読む（NFR1.7、NFR8.4）。"""

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
        """NFR1.7: 全ファイルを一度に読まず、1 ファイルずつ順に読む。"""
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
        """BR10.1: 抽出した人・入力・Body・結果を消す。ほかのアンケートには触れない。"""
        service.write_personas(SURVEY_ID, pl.DataFrame({"uuid": ["p1"]}))
        service.write_input(SURVEY_ID, ['{"record_id": "p1", "model_id": "m"}'])
        list(service.write_bodies(SURVEY_ID, [("p1", "{}", True)]))
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
    """再起動の前に始めた実行を、開始の前に見つける。"""

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
    """子の実行の入力の上限（256 KiB）に余裕を残して、Body を項目に入れるか決める。"""

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


def test_encode_body_keeps_non_ascii_and_is_one_line():
    encoded = SurveyRunnerService.encode_body({"system": "改行\nあり"})
    assert "\n" not in encoded
    assert "改行" in encoded
    assert json.loads(encoded) == {"system": "改行\nあり"}


def test_request_body_is_the_anthropic_messages_format():
    """BR6.2: anthropic_version、max_tokens 2048、system、messages、output_config。"""
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
