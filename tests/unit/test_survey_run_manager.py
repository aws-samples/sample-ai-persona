"""SurveyRunManager（進行役の 1 回 ``advance()``）のユニットテスト。

DynamoDB（Surveys・SurveyRunLease）と S3（結果の CSV・画像・カスタムデータ）は moto の上の
本物のストアと S3Service（tests/unit/survey_world.py）。Step Functions と ``survey-runs/`` は
``FakeRunner``（メモリの上の SurveyRunnerService の代わり）、ペルソナの抽出はモック。
時刻と担当の延長のスレッドは差し込む（time.sleep で待たない）。

functional-spec.md WF2〜WF4、rules.md BR1〜BR6・BR10、reliability-requirements.md
NFR4.5〜NFR4.15、observability-requirements.md NFR7.2〜NFR7.9、NFR3.7〜NFR3.9。
"""

import dataclasses
import json
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from unittest.mock import Mock

import polars as pl
import pytest

from src.managers.survey_run_manager import LeaseKeeper, SurveyRunManager
from src.models.errors import CodedError, ErrorCode
from src.models.external_failure import FailureClass
from src.models.survey import Survey
from src.models.survey_template import Question, SurveyTemplate, TemplateImage
from src.services.survey_runner_service import (
    ExecutionInfo,
    RunResult,
    SurveyRunnerService,
    SurveyRunnerServiceError,
)
from tests.log_helpers import LogCapture
from tests.unit.survey_world import BUCKET, SurveyWorld

T0 = datetime(2026, 10, 30, 1, 0, 0, tzinfo=timezone.utc)
SM = "arn:aws:states:us-east-1:123456789012:stateMachine:SurveyRunSM"
MODEL = "global.anthropic.claude-haiku-5-5"
TIMEOUT = 7200


def _arn(survey_id: str) -> str:
    return f"arn:aws:states:us-east-1:123456789012:execution:SurveyRunSM:{survey_id}"


class Clock:
    """The manager's clock (UTC); tests move it."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeKeeper:
    """Stands in for the renewing thread (no thread in tests)."""

    def __init__(self, lost: bool = False) -> None:
        self.lost = lost
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class FakeRunner:
    """SurveyRunnerService in memory (Step Functions and survey-runs/)."""

    request_body = staticmethod(SurveyRunnerService.request_body)
    fits_inline = staticmethod(SurveyRunnerService.fits_inline)

    def __init__(self) -> None:
        self.state_machine_arn = SM
        self.personas: dict[str, pl.DataFrame] = {}
        self.inputs: dict[str, list[dict[str, Any]]] = {}
        self.bodies: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, ExecutionInfo] = {}
        self.results: dict[str, list[RunResult]] = {}
        self.deleted: list[str] = []
        self.stopped: list[str] = []
        self.started: list[str] = []
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    def execution_arn_for(self, survey_id: str) -> str:
        return _arn(survey_id)

    def is_own_execution(self, execution_arn: str) -> bool:
        return execution_arn.startswith(_arn(""))

    def has_personas(self, survey_id: str) -> bool:
        self._call("has_personas")
        return survey_id in self.personas

    def write_personas(self, survey_id: str, personas: pl.DataFrame) -> None:
        self._call("write_personas")
        self.personas[survey_id] = personas

    def read_personas(self, survey_id: str) -> pl.DataFrame:
        self._call("read_personas")
        if survey_id not in self.personas:
            raise SurveyRunnerServiceError("gone", failure=FailureClass.NOT_FOUND)
        return self.personas[survey_id]

    def write_body(
        self, survey_id: str, record_id: str, body: Mapping[str, Any]
    ) -> str:
        self._call("write_body")
        key = f"survey-runs/{survey_id}/bodies/{record_id}.json"
        self.bodies[key] = dict(body)
        return f"s3://{BUCKET}/{key}"

    def write_input(self, survey_id: str, lines: Any) -> str:
        self._call("write_input")
        self.inputs[survey_id] = list(lines)
        return f"survey-runs/{survey_id}/input.jsonl"

    def find_execution(self, survey_id: str) -> Optional[ExecutionInfo]:
        self._call("find_execution")
        existing = self.executions.get(survey_id)
        if existing is None:
            return None
        return dataclasses.replace(existing, reused=True)

    def start_execution(self, survey_id: str) -> ExecutionInfo:
        self._call("start_execution")
        self.started.append(survey_id)
        existing = self.executions.get(survey_id)
        if existing is not None:
            return ExecutionInfo(
                execution_arn=existing.execution_arn,
                status=existing.status,
                start_date=existing.start_date,
                reused=True,
            )
        info = ExecutionInfo(
            execution_arn=_arn(survey_id),
            status="RUNNING",
            start_date=CLOCK_FOR_RUNNER[0](),
        )
        self.executions[survey_id] = info
        return info

    def describe_execution(self, execution_arn: str) -> ExecutionInfo:
        self._call("describe_execution")
        survey_id = execution_arn.rsplit(":", 1)[-1]
        return self.executions[survey_id]

    def stop_execution(self, execution_arn: str) -> None:
        self._call("stop_execution")
        self.stopped.append(execution_arn)

    def iter_results(self, manifest_key: str) -> Iterator[RunResult]:
        self._call("iter_results")
        survey_id = manifest_key.split("/")[1]
        yield from self.results.get(survey_id, [])

    def delete_run(self, survey_id: str) -> None:
        self._call("delete_run")
        self.deleted.append(survey_id)

    # --- helpers for tests ---------------------------------------------------------

    def finish(
        self,
        survey_id: str,
        status: str = "SUCCEEDED",
        *,
        stop: Optional[datetime] = None,
    ) -> None:
        info = self.executions[survey_id]
        self.executions[survey_id] = ExecutionInfo(
            execution_arn=info.execution_arn,
            status=status,
            start_date=info.start_date,
            stop_date=stop or info.start_date + timedelta(minutes=2),
            manifest_key=f"survey-runs/{survey_id}/results/run/manifest.json"
            if status == "SUCCEEDED"
            else None,
        )


#: The clock the fake runner stamps start dates with (set per test).
CLOCK_FOR_RUNNER: list[Callable[[], datetime]] = [lambda: T0]


QUESTIONS = [
    Question(
        id="q1",
        text="好きな色は？",
        question_type="multiple_choice",
        options=["赤", "青"],
    ),
    Question(id="q2", text="満足度は？", question_type="scale_rating"),
]


def _answer_text(color: str = "赤", score: int = 4) -> str:
    return json.dumps({"answers": {"q1": color, "q2": score}}, ensure_ascii=False)


def _personas(rows: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "uuid": [f"p{i}" for i in range(rows)],
            "sex": ["女性"] * rows,
            "age": [30 + i % 40 for i in range(rows)],
            "occupation": ["会社員"] * rows,
            "country": ["日本"] * rows,
            "region": ["関東"] * rows,
            "prefecture": ["東京都"] * rows,
            "marital_status": ["未婚"] * rows,
            "persona": ["秘密のプロフィール"] * rows,
            "hobby": ["釣り"] * rows,
        }
    )


@pytest.fixture
def clock() -> Clock:
    c = Clock()
    CLOCK_FOR_RUNNER[0] = c
    return c


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def batch() -> Mock:
    service = Mock()
    service.has_parquet_uri.return_value = True
    service.filter_and_sample_personas.side_effect = lambda filters, count, **k: (
        _personas(count)
    )
    return service


@pytest.fixture
def keeper() -> FakeKeeper:
    return FakeKeeper()


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


def _manager(world, runner, batch, clock, keeper, holder="task-a") -> SurveyRunManager:
    return SurveyRunManager(
        survey_store=world.survey_store,
        lease_store=world.lease_store,
        runner=runner,
        batch_service=batch,
        s3_service=world.s3,
        custom_persona_data_store=world.custom_store,
        survey_template_store=world.template_store,
        holder=holder,
        model_id=MODEL,
        timeout_seconds=TIMEOUT,
        clock=clock,
        keeper_factory=lambda renew: keeper,
    )


@pytest.fixture
def rm(survey_world, runner, batch, clock, keeper) -> SurveyRunManager:
    return _manager(survey_world, runner, batch, clock, keeper)


def _template(
    world: SurveyWorld,
    images: Optional[list[TemplateImage]] = None,
    questions: Optional[list[Question]] = None,
):
    template = SurveyTemplate.create_new(
        name="色の調査", questions=list(questions or QUESTIONS), images=images or []
    ).owned_by("a")
    world.template_store.put_new(template)
    return template


def _queue(
    world: SurveyWorld,
    clock: Clock,
    *,
    count: int = 10,
    at: Optional[datetime] = None,
    filters: Optional[dict] = None,
    datasource: str = "nemotron",
    images: Optional[list[TemplateImage]] = None,
    questions: Optional[list[Question]] = None,
) -> Survey:
    survey = world.survey(
        "a",
        _template(world, images, questions),
        status="queued",
        persona_count=count,
        datasource=datasource,
    )
    queued_at = at or clock.now
    world.survey_store.update_fields(
        survey.id,
        {"queued_at": queued_at.isoformat(), "filters": filters},
    )
    return _get(world, survey.id)


def _get(world: SurveyWorld, survey_id: str) -> Survey:
    survey = world.survey_store.get(survey_id, consistent=True)
    assert survey is not None
    return survey


def _succeed(
    runner: FakeRunner,
    survey: Survey,
    *,
    failed: int = 0,
    invalid: int = 0,
    missing: int = 0,
) -> None:
    """Finish the execution: the last personas fail in the given ways."""
    ids = runner.personas[survey.id]["uuid"].to_list()
    results: list[RunResult] = []
    bad = failed + invalid + missing
    for i, rid in enumerate(ids):
        k = i - (len(ids) - bad)
        if k < 0:
            results.append(
                RunResult(record_id=rid, succeeded=True, text=_answer_text())
            )
        elif k < failed:
            results.append(
                RunResult(
                    record_id=rid, succeeded=False, error="Bedrock.ThrottlingException"
                )
            )
        elif k < failed + invalid:
            results.append(RunResult(record_id=rid, succeeded=True, text="not json"))
    runner.results[survey.id] = results
    runner.finish(survey.id)


def _csv_rows(world: SurveyWorld, survey: Survey) -> list[dict[str, str]]:
    import csv
    import io

    raw = world.s3.download_file(survey.s3_result_path).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(raw)))


# =============================================================================
# 担当（BR2.3、NFR3.8、NFR1.8）
# =============================================================================


class TestLease:
    def test_without_the_lease_nothing_happens(
        self, survey_world, runner, batch, clock, keeper
    ):
        survey = _queue(survey_world, clock)
        assert survey_world.lease_store.acquire(
            "other", now=int(T0.timestamp()), ttl_seconds=60
        )
        rm = _manager(survey_world, runner, batch, clock, keeper)
        spy = Mock(wraps=survey_world.survey_store.list_run_queue)
        survey_world.survey_store.list_run_queue = spy

        rm.advance()

        spy.assert_not_called()
        assert runner.calls == []
        assert _get(survey_world, survey.id).status == "queued"
        assert keeper.started == 0

    def test_two_runners_only_one_works(self, survey_world, runner, batch, clock):
        """NFR3.8: 2 つの進行役が同じテーブルを取り合うと、片方だけが仕事をする。"""
        _queue(survey_world, clock)
        first = _manager(survey_world, runner, batch, clock, FakeKeeper(), "task-a")
        second = _manager(survey_world, runner, batch, clock, FakeKeeper(), "task-b")

        first.advance()
        second.advance()

        assert len(runner.started) == 1

    def test_the_lease_is_kept_between_rounds_and_extended(
        self, survey_world, rm, clock
    ):
        rm.advance()
        clock.advance(seconds=15)
        rm.advance()

        row = survey_world.raw(survey_world.lease_table, "survey-run")
        assert row["holder"]["S"] == "task-a"
        assert int(row["expires_at"]["N"]) == int(clock.now.timestamp()) + 60

    def test_keeper_runs_during_the_work(self, survey_world, rm, clock, keeper):
        _queue(survey_world, clock)

        rm.advance()

        assert keeper.started == 1
        assert keeper.stopped == 1

    def test_lost_lease_stops_before_writing(self, survey_world, runner, batch, clock):
        """BR2.3: 延長できなくなったら、各段の書き込みの前にやめる。"""
        survey = _queue(survey_world, clock)
        rm = _manager(survey_world, runner, batch, clock, FakeKeeper(lost=True))

        rm.advance()

        assert runner.started == []
        assert "write_input" not in runner.calls
        assert _get(survey_world, survey.id).status == "queued"

    def test_release_removes_only_our_lease(
        self, survey_world, rm, runner, batch, clock
    ):
        rm.advance()
        assert rm.release_lease()
        assert survey_world.raw(survey_world.lease_table, "survey-run") is None

        other = _manager(survey_world, runner, batch, clock, FakeKeeper(), "task-b")
        other.advance()
        assert not rm.release_lease()
        assert survey_world.raw(survey_world.lease_table, "survey-run") is not None


class TestLeaseKeeper:
    def test_renews_every_20_seconds_while_working(self):
        waits: list[float] = []
        renew = Mock(return_value=True)

        def wait(seconds: float) -> bool:
            waits.append(seconds)
            return len(waits) > 3  # stopped on the 4th wait

        keeper = LeaseKeeper(renew, interval_seconds=20, wait=wait)
        keeper.run()

        assert waits == [20, 20, 20, 20]
        assert renew.call_count == 3
        assert not keeper.lost

    @pytest.mark.parametrize("failure", [False, RuntimeError("dynamodb down")])
    def test_a_failed_renewal_marks_the_lease_lost(self, failure):
        """NFR1.8: 仕事が 60 秒を超えても延長で担当を保ち、延長できなければやめる。"""
        renew = Mock(
            side_effect=[True, failure]
            if isinstance(failure, Exception)
            else [True, False]
        )
        keeper = LeaseKeeper(renew, interval_seconds=20, wait=lambda s: False)

        keeper.run()

        assert keeper.lost
        assert renew.call_count == 2

    def test_thread_starts_and_stops(self):
        renew = Mock(return_value=True)
        keeper = LeaseKeeper(renew, interval_seconds=0.01)
        keeper.start()
        keeper.stop()
        assert not keeper.lost


# =============================================================================
# 1 件ずつ、早い順（BR2.1、BR2.2、BR2.4、NFR3.7、NFR3.9）
# =============================================================================


class TestQueue:
    def test_the_oldest_queued_starts_with_ties_by_id(
        self, survey_world, rm, runner, clock
    ):
        later = _queue(survey_world, clock, at=T0 + timedelta(seconds=5))
        first = _queue(survey_world, clock, at=T0)
        twin = _queue(survey_world, clock, at=T0)
        expected = min(first.id, twin.id)

        rm.advance()

        assert runner.started == [expected]
        assert _get(survey_world, expected).status == "running"
        assert _get(survey_world, later.id).status == "queued"

    def test_nothing_starts_while_one_is_running(self, survey_world, rm, runner, clock):
        first = _queue(survey_world, clock, at=T0)
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        rm.advance()
        clock.advance(seconds=15)

        rm.advance()

        assert runner.started == [first.id]
        assert _get(survey_world, second.id).status == "queued"

    def test_watching_round_makes_few_calls(
        self, survey_world, rm, runner, clock, logs
    ):
        """NFR3.9・NFR7.6: 監視だけの回は Describe 1 回で、ログを出さない。"""
        _queue(survey_world, clock)
        rm.advance()
        before = len(logs.records())
        runner.calls.clear()
        query = Mock(wraps=survey_world.client.query)
        survey_world.client.query = query
        clock.advance(seconds=15)

        rm.advance()

        assert runner.calls == ["describe_execution"]
        assert query.call_count == 1
        assert logs.records()[before:] == []

    def test_next_starts_in_the_same_round_after_completion(
        self, survey_world, rm, runner, clock
    ):
        """BR2.4・NFR1.3: 終わった回のうちに次を始める。"""
        first = _queue(survey_world, clock, at=T0)
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        rm.advance()
        _succeed(runner, first)

        rm.advance()

        assert _get(survey_world, first.id).status == "completed"
        assert _get(survey_world, second.id).status == "running"


# =============================================================================
# 監視と完了（WF3、BR3.4、BR4、NFR4.12）
# =============================================================================


class TestCompletion:
    @pytest.mark.parametrize(
        ("total", "failed", "status"),
        [
            (10, 0, "completed"),
            (10, 1, "error"),
            (100, 5, "completed"),
            (100, 6, "error"),
            (1000, 50, "completed"),
            (1000, 51, "error"),
            (10, 10, "error"),
        ],
    )
    def test_five_percent_rule(
        self, survey_world, rm, runner, clock, total, failed, status
    ):
        survey = _queue(survey_world, clock, count=total)
        rm.advance()
        _succeed(runner, survey, failed=failed)

        rm.advance()

        done = _get(survey_world, survey.id)
        assert done.status == status
        assert done.run_queue is None
        if status == "completed":
            assert done.failed_count == failed
            assert done.persona_count == total
            assert done.s3_result_path == (
                f"s3://{BUCKET}/survey-results/{survey.id}/results.csv"
            )
        else:
            assert done.error_code == ErrorCode.SURVEY_TOO_MANY_FAILED_ANSWERS.value
            assert done.error_context == {"failed_count": failed, "total": total}
            assert done.s3_result_path is None

    def test_csv_keeps_attributes_and_blanks_missing_answers(
        self, survey_world, rm, runner, clock
    ):
        """BR4.3: 取れなかった人は回答の列だけ空欄。"""
        survey = _queue(survey_world, clock, count=100)
        rm.advance()
        _succeed(runner, survey, failed=2)

        rm.advance()

        rows = _csv_rows(survey_world, _get(survey_world, survey.id))
        assert len(rows) == 100
        assert rows[0]["persona_id"] == "p0"
        assert rows[0]["q1_answer"] == "赤"
        assert rows[0]["q2_answer"] == "4"
        assert rows[0]["q1_text"] == "好きな色は？"
        assert rows[-1]["q1_answer"] == ""
        assert rows[-1]["sex"] == "女性"
        assert rows[-1]["q1_text"] == "好きな色は？"

    def test_three_kinds_of_failure_are_added_once_each(
        self, survey_world, rm, runner, clock, logs
    ):
        """BR4.2: 失敗・読めない応答・結果にない人を合算し、同じ人は 1 回。"""
        survey = _queue(survey_world, clock, count=100)
        rm.advance()
        _succeed(runner, survey, failed=2, invalid=2, missing=1)
        runner.results[survey.id].append(
            RunResult(
                record_id="p95", succeeded=False, error="Bedrock.ThrottlingException"
            )
        )

        rm.advance()

        done = _get(survey_world, survey.id)
        assert done.status == "completed"
        assert done.failed_count == 5
        completed = logs.events("survey.run_completed")[0]
        assert completed["failed_count"] == 5
        assert completed["total"] == 100
        assert completed["invalid_count"] == 2
        assert completed["missing_count"] == 1
        assert completed["failed_by_error"] == {"Bedrock.ThrottlingException": 2}
        assert completed["execution_arn"] == _arn(survey.id)

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            ("FAILED", ErrorCode.SURVEY_INFERENCE_FAILED),
            ("ABORTED", ErrorCode.SURVEY_INFERENCE_FAILED),
            ("TIMED_OUT", ErrorCode.SURVEY_EXECUTION_TIMED_OUT),
        ],
    )
    def test_other_endings_are_errors_and_the_next_starts(
        self, survey_world, rm, runner, clock, status, code
    ):
        first = _queue(survey_world, clock, at=T0)
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        rm.advance()
        runner.finish(first.id, status)

        rm.advance()

        failed = _get(survey_world, first.id)
        assert failed.status == "error"
        assert failed.error_code == code.value
        assert failed.run_queue is None
        assert first.id in runner.deleted
        assert _get(survey_world, second.id).status == "running"

    def test_run_location_is_cleaned_after_completion(
        self, survey_world, rm, runner, clock
    ):
        survey = _queue(survey_world, clock)
        rm.advance()
        _succeed(runner, survey)

        rm.advance()

        assert runner.deleted == [survey.id]

    def test_survey_deleted_meanwhile_removes_the_csv(
        self, survey_world, rm, runner, clock, monkeypatch
    ):
        survey = _queue(survey_world, clock)
        rm.advance()
        _succeed(runner, survey)
        real = survey_world.survey_store.transition

        def gone(survey_id, *, expected, fields, remove=()):
            if fields.get("status") == "completed":
                survey_world.client.delete_item(
                    TableName=survey_world.surveys_table, Key={"id": {"S": survey_id}}
                )
            return real(survey_id, expected=expected, fields=fields, remove=remove)

        monkeypatch.setattr(survey_world.survey_store, "transition", gone)

        rm.advance()

        assert not survey_world.s3.exists(
            f"s3://{BUCKET}/survey-results/{survey.id}/results.csv"
        )
        assert survey.id in runner.deleted

    @pytest.mark.parametrize(
        "winner",
        [
            {"status": "completed", "persona_count": 10, "failed_count": 0},
            {"status": "error", "error_code": "SURVEY_EXECUTION_FAILED"},
        ],
    )
    def test_finished_by_another_task_keeps_its_csv_and_run(
        self, survey_world, rm, runner, clock, monkeypatch, winner
    ):
        """NFR4.4: 遷移が通らなくても、記録が残っていれば CSV も実行の場所も消さない。"""
        survey = _queue(survey_world, clock)
        rm.advance()
        _succeed(runner, survey)
        real = survey_world.survey_store.transition
        reads: list[bool] = []
        real_get = survey_world.survey_store.get

        def other_task_first(survey_id, *, expected, fields, remove=()):
            if fields.get("status") == "completed":
                real(survey_id, expected="running", fields=dict(winner))
            return real(survey_id, expected=expected, fields=fields, remove=remove)

        def spy_get(survey_id, *, consistent=False):
            reads.append(consistent)
            return real_get(survey_id, consistent=consistent)

        monkeypatch.setattr(survey_world.survey_store, "transition", other_task_first)
        monkeypatch.setattr(survey_world.survey_store, "get", spy_get)

        rm.advance()

        assert _get(survey_world, survey.id).status == winner["status"]
        assert survey_world.s3.exists(
            f"s3://{BUCKET}/survey-results/{survey.id}/results.csv"
        )
        assert survey.id not in runner.deleted
        assert reads and all(reads)

    def test_execution_of_another_state_machine_is_an_error(
        self, survey_world, rm, runner, clock
    ):
        """基盤設計 2.2: ステートマシンが作り直されたら、古い実行は追えない。"""
        survey = _queue(survey_world, clock)
        survey_world.survey_store.transition(
            survey.id,
            expected="queued",
            fields={
                "status": "running",
                "execution_arn": "arn:aws:states:us-east-1:1:execution:OldSM:x",
                "started_at": T0.isoformat(),
            },
        )

        rm.advance()

        failed = _get(survey_world, survey.id)
        assert failed.status == "error"
        assert failed.error_code == ErrorCode.SURVEY_EXECUTION_FAILED.value
        assert "describe_execution" not in runner.calls


class TestStaleIndex:
    """索引（GSI）は結果整合なので、running の記録は強い整合性で読み直してから監視する。"""

    @pytest.mark.parametrize("now", ["completed", "error", "deleted"])
    def test_a_survey_no_longer_running_is_not_watched(
        self, survey_world, rm, runner, clock, monkeypatch, now
    ):
        survey = _queue(survey_world, clock, at=T0)
        rm.advance()
        stale = _get(survey_world, survey.id)
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        if now == "deleted":
            survey_world.client.delete_item(
                TableName=survey_world.surveys_table, Key={"id": {"S": survey.id}}
            )
        else:
            survey_world.survey_store.update_fields(survey.id, {"status": now})
        monkeypatch.setattr(
            survey_world.survey_store,
            "list_run_queue",
            lambda: [stale, _get(survey_world, second.id)],
        )
        runner.calls.clear()

        rm.advance()

        assert "describe_execution" not in runner.calls
        assert survey.id not in runner.deleted
        assert _get(survey_world, second.id).status == "running"
        if now != "deleted":
            kept = _get(survey_world, survey.id)
            assert kept.status == now
            assert kept.error_code is None

    def test_a_survey_already_running_blocks_the_next_start(
        self, survey_world, rm, runner, clock, monkeypatch
    ):
        """NFR3.7: 索引ではまだ queued でも、読み直して running なら次を始めない。"""
        first = _queue(survey_world, clock, at=T0)
        stale = _get(survey_world, first.id)
        rm.advance()
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        monkeypatch.setattr(
            survey_world.survey_store,
            "list_run_queue",
            lambda: [stale, _get(survey_world, second.id)],
        )
        clock.advance(seconds=15)

        rm.advance()

        assert runner.started == [first.id]
        assert _get(survey_world, first.id).status == "running"
        assert _get(survey_world, second.id).status == "queued"

    def test_a_later_survey_already_running_blocks_an_earlier_start(
        self, survey_world, rm, runner, clock, monkeypatch
    ):
        """NFR3.7: 実行中が受付順で後ろにあっても、索引の候補を全件読み直してから始める。

        1 回目は A が索引にまだ見えず B を始める。2 回目は索引で A・B とも queued に見える。
        """
        a = _queue(survey_world, clock, at=T0)
        b = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        stale_a, stale_b = _get(survey_world, a.id), _get(survey_world, b.id)
        seen = [[stale_b], [stale_a, stale_b]]
        monkeypatch.setattr(
            survey_world.survey_store, "list_run_queue", lambda: seen.pop(0)
        )

        rm.advance()
        clock.advance(seconds=15)
        rm.advance()

        assert runner.started == [b.id]
        assert _get(survey_world, a.id).status == "queued"
        assert _get(survey_world, b.id).status == "running"

    def test_the_fresh_record_is_the_one_watched(
        self, survey_world, rm, runner, clock, monkeypatch
    ):
        """索引の値が古くても、読み直した execution_arn で監視する。"""
        survey = _queue(survey_world, clock)
        rm.advance()
        stale = dataclasses.replace(_get(survey_world, survey.id), execution_arn=None)
        monkeypatch.setattr(
            survey_world.survey_store, "list_run_queue", lambda: [stale]
        )
        _succeed(runner, survey)

        rm.advance()

        assert _get(survey_world, survey.id).status == "completed"


# =============================================================================
# 失敗の分類ごとの扱い（reliability 3.1、NFR4.5〜NFR4.9a、BR3.7、R-12）
# =============================================================================


def _error(failure: FailureClass) -> SurveyRunnerServiceError:
    return SurveyRunnerServiceError(f"{failure.value} failure", failure=failure)


class TestMonitorFailures:
    def _running(self, world, rm, clock) -> Survey:
        survey = _queue(world, clock)
        rm.advance()
        return survey

    def test_not_found_is_an_error_at_once(self, survey_world, rm, runner, clock):
        survey = self._running(survey_world, rm, clock)
        runner.fail["describe_execution"] = _error(FailureClass.NOT_FOUND)

        rm.advance()

        assert _get(survey_world, survey.id).error_code == (
            ErrorCode.SURVEY_EXECUTION_FAILED.value
        )

    @pytest.mark.parametrize("failure", [FailureClass.TRANSIENT, FailureClass.ACCESS])
    def test_transient_and_access_wait_until_the_time_limit(
        self, survey_world, rm, runner, clock, failure
    ):
        """NFR4.5・NFR4.7・NFR4.8: 打ち切り時間 + 300 秒までは状態を変えない。"""
        survey = self._running(survey_world, rm, clock)
        runner.fail["describe_execution"] = _error(failure)

        clock.advance(seconds=TIMEOUT + 300)
        rm.advance()
        assert _get(survey_world, survey.id).status == "running"
        assert _get(survey_world, survey.id).finish_attempts == 0

        clock.advance(seconds=1)
        rm.advance()
        timed_out = _get(survey_world, survey.id)
        assert timed_out.status == "error"
        assert timed_out.error_code == ErrorCode.SURVEY_EXECUTION_TIMED_OUT.value
        assert runner.stopped == [_arn(survey.id)]

    def test_running_past_the_time_limit_is_stopped(
        self, survey_world, rm, runner, clock
    ):
        survey = self._running(survey_world, rm, clock)
        clock.advance(seconds=TIMEOUT + 301)

        rm.advance()

        assert _get(survey_world, survey.id).error_code == (
            ErrorCode.SURVEY_EXECUTION_TIMED_OUT.value
        )
        assert runner.stopped == [_arn(survey.id)]

    def test_failed_stop_still_ends_the_survey(self, survey_world, rm, runner, clock):
        survey = self._running(survey_world, rm, clock)
        runner.fail["stop_execution"] = _error(FailureClass.TRANSIENT)
        clock.advance(seconds=TIMEOUT + 301)

        rm.advance()

        assert _get(survey_world, survey.id).status == "error"


class TestFinishFailures:
    def _succeeded(self, world, rm, runner, clock) -> Survey:
        survey = _queue(world, clock)
        rm.advance()
        _succeed(runner, survey)
        return survey

    @pytest.mark.parametrize("failure", [FailureClass.NOT_FOUND, FailureClass.INVALID])
    def test_lasting_failures_are_errors_at_once(
        self, survey_world, rm, runner, clock, failure
    ):
        survey = self._succeeded(survey_world, rm, runner, clock)
        runner.fail["iter_results"] = _error(failure)

        rm.advance()

        assert _get(survey_world, survey.id).error_code == (
            ErrorCode.SURVEY_EXECUTION_FAILED.value
        )

    def test_transient_failure_retries_three_times(
        self, survey_world, rm, runner, clock
    ):
        """BR3.7: 一時的な失敗は finish_attempts を数え、3 回目でエラーにする。"""
        first = self._succeeded(survey_world, rm, runner, clock)
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        runner.fail["read_personas"] = _error(FailureClass.TRANSIENT)

        rm.advance()
        rm.advance()
        assert _get(survey_world, first.id).status == "running"
        assert _get(survey_world, first.id).finish_attempts == 2
        assert _get(survey_world, second.id).status == "queued"

        rm.advance()
        assert _get(survey_world, first.id).error_code == (
            ErrorCode.SURVEY_EXECUTION_FAILED.value
        )
        assert _get(survey_world, second.id).status == "running"

    def test_access_failure_gives_up_30_minutes_after_the_end(
        self, survey_world, rm, runner, clock
    ):
        """R-12: 実行が終わってから 30 分で SURVEY_RUNNER_UNAVAILABLE（打ち切りまで待たない）。"""
        survey = self._succeeded(survey_world, rm, runner, clock)
        end = runner.executions[survey.id].stop_date
        runner.fail["read_personas"] = _error(FailureClass.ACCESS)

        clock.now = end + timedelta(minutes=30)
        rm.advance()
        assert _get(survey_world, survey.id).status == "running"
        assert _get(survey_world, survey.id).finish_attempts == 0

        clock.now = end + timedelta(minutes=30, seconds=1)
        rm.advance()
        assert _get(survey_world, survey.id).error_code == (
            ErrorCode.SURVEY_RUNNER_UNAVAILABLE.value
        )

    def test_csv_upload_failure_is_classified(
        self, survey_world, rm, runner, clock, monkeypatch
    ):
        from src.services.s3_service import S3ServiceError

        survey = self._succeeded(survey_world, rm, runner, clock)
        monkeypatch.setattr(
            survey_world.s3,
            "upload_file",
            Mock(side_effect=S3ServiceError("x", failure=FailureClass.TRANSIENT)),
        )

        rm.advance()

        assert _get(survey_world, survey.id).finish_attempts == 1


# =============================================================================
# 開始（WF4、BR1.4、BR1.5、BR3.1、BR3.2、BR5、BR6、NFR4.9、NFR4.9a）
# =============================================================================


class TestStart:
    def test_start_writes_input_and_records_the_execution(
        self, survey_world, rm, runner, clock
    ):
        survey = _queue(survey_world, clock, count=10)

        rm.advance()

        running = _get(survey_world, survey.id)
        assert running.status == "running"
        assert running.execution_arn == _arn(survey.id)
        assert running.started_at == T0
        assert running.model_id == MODEL
        assert running.run_queue == "active"
        lines = runner.inputs[survey.id]
        assert [line["record_id"] for line in lines] == [f"p{i}" for i in range(10)]
        assert all(line["model_id"] == MODEL for line in lines)
        body = lines[0]["body"]
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert body["max_tokens"] == 2048
        assert "秘密のプロフィール" in body["system"]
        schema = body["output_config"]["format"]["schema"]
        assert schema["properties"]["answers"]["required"] == ["q1", "q2"]

    def test_personas_are_sampled_once(self, survey_world, rm, runner, batch, clock):
        """BR1.5・NFR4.3: 始め直しでは personas.parquet を読み、抽出し直さない。"""
        survey = _queue(survey_world, clock, count=10)
        runner.personas[survey.id] = _personas(12)

        rm.advance()

        batch.filter_and_sample_personas.assert_not_called()
        assert len(runner.inputs[survey.id]) == 12

    def test_personas_file_keeps_only_the_needed_columns(
        self, survey_world, rm, runner, batch, clock
    ):
        survey = _queue(survey_world, clock, count=10)
        batch.filter_and_sample_personas.side_effect = lambda *a, **k: _personas(
            10
        ).with_columns(pl.lit("x").alias("unrelated_column"))

        rm.advance()

        columns = runner.personas[survey.id].columns
        assert columns[0] == "uuid"
        assert "unrelated_column" not in columns
        assert "persona" in columns

    @pytest.mark.parametrize(
        ("filters", "code", "context"),
        [
            (
                None,
                ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS,
                {"row_count": 9, "min_rows": 10},
            ),
            (
                {"性別": "女性"},
                ErrorCode.SURVEY_AVAILABLE_PERSONAS_TOO_FEW,
                {"available_count": 9, "min_count": 10},
            ),
        ],
    )
    def test_too_few_personas_is_an_error_and_the_next_starts(
        self, survey_world, rm, runner, batch, clock, filters, code, context
    ):
        """BR1.4: 開始の直前に 10 人以上かを確かめる。"""
        first = _queue(survey_world, clock, at=T0, filters=filters)
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        batch.filter_and_sample_personas.side_effect = [_personas(9), _personas(10)]

        rm.advance()

        failed = _get(survey_world, first.id)
        assert failed.status == "error"
        assert failed.error_code == code.value
        assert failed.error_context == context
        assert failed.run_queue is None
        assert first.id in runner.deleted
        assert runner.started == [second.id]

    def test_existing_execution_is_taken_over(self, survey_world, rm, runner, clock):
        """BR3.1: ExecutionAlreadyExists は失敗として数えず、その実行を引き継ぐ。"""
        survey = _queue(survey_world, clock)
        began = T0 - timedelta(minutes=3)
        runner.executions[survey.id] = ExecutionInfo(
            execution_arn=_arn(survey.id), status="RUNNING", start_date=began
        )

        rm.advance()

        running = _get(survey_world, survey.id)
        assert running.status == "running"
        assert running.started_at == began
        assert running.start_attempts == 0

    def test_execution_started_before_a_restart_is_recorded_without_preparing(
        self, survey_world, rm, runner, batch, clock
    ):
        """開始の直後に落ちた実行は、入力を作り直さずに running として記録する。

        元データが消えていても、動いている実行をエラーにせず、実行用ファイルも消さない。
        """
        survey = _queue(survey_world, clock)
        began = T0 - timedelta(minutes=1)
        runner.executions[survey.id] = ExecutionInfo(
            execution_arn=_arn(survey.id), status="RUNNING", start_date=began
        )
        batch.has_parquet_uri.return_value = False

        rm.advance()

        running = _get(survey_world, survey.id)
        assert running.status == "running"
        assert running.started_at == began
        assert running.execution_arn == _arn(survey.id)
        assert "write_input" not in runner.calls
        assert "start_execution" not in runner.calls
        assert survey.id not in runner.deleted

    def test_large_bodies_go_to_s3_without_images(
        self, survey_world, rm, runner, clock
    ):
        """子の実行の入力の上限（256 KiB）を超えそうな Body は、画像がなくても S3 に置く。"""
        options = [f"選択肢{i}・" + "あ" * 100 for i in range(400)]
        questions = [
            Question(
                id="q1",
                text="当てはまるものは？",
                question_type="multiple_choice",
                options=options,
            )
        ]
        survey = _queue(survey_world, clock, count=10, questions=questions)

        rm.advance()

        lines = runner.inputs[survey.id]
        assert all(
            set(line) == {"record_id", "model_id", "body_s3_uri"} for line in lines
        )
        assert len(runner.bodies) == 10
        body = next(iter(runner.bodies.values()))
        assert not SurveyRunnerService.fits_inline(body)

    def test_deleted_while_starting_stops_the_execution(
        self, survey_world, rm, runner, clock, monkeypatch
    ):
        survey = _queue(survey_world, clock)
        real = runner.start_execution

        def start_then_delete(survey_id):
            info = real(survey_id)
            survey_world.client.delete_item(
                TableName=survey_world.surveys_table, Key={"id": {"S": survey_id}}
            )
            return info

        runner.start_execution = start_then_delete

        rm.advance()

        assert runner.stopped == [_arn(survey.id)]
        assert survey.id in runner.deleted

    def test_started_by_another_task_is_not_stopped(
        self, survey_world, rm, runner, clock
    ):
        """NFR4.4: 先にほかのタスクが running にしたら、その実行を止めず片付けない。"""
        survey = _queue(survey_world, clock, at=T0)
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        real = runner.start_execution

        def other_task_records_first(survey_id):
            info = real(survey_id)
            survey_world.survey_store.transition(
                survey_id,
                expected="queued",
                fields={
                    "status": "running",
                    "execution_arn": info.execution_arn,
                    "started_at": info.start_date.isoformat(),
                },
            )
            return info

        runner.start_execution = other_task_records_first

        rm.advance()

        assert runner.stopped == []
        assert survey.id not in runner.deleted
        assert _get(survey_world, survey.id).status == "running"
        assert _get(survey_world, second.id).status == "queued"

    def test_failure_after_another_task_started_keeps_its_run(
        self, survey_world, rm, runner, batch, clock
    ):
        """queued → error が通らず、ほかのタスクが running にしていたら片付けない。"""
        survey = _queue(survey_world, clock)

        def started_elsewhere_then_too_few(filters, count, **kwargs):
            survey_world.survey_store.transition(
                survey.id,
                expected="queued",
                fields={"status": "running", "execution_arn": _arn(survey.id)},
            )
            return _personas(2)

        batch.filter_and_sample_personas.side_effect = started_elsewhere_then_too_few

        rm.advance()

        assert _get(survey_world, survey.id).status == "running"
        assert survey.id not in runner.deleted

    def test_images_go_to_s3_one_body_per_persona(
        self, survey_world, rm, runner, clock
    ):
        """BR6.3・NFR3.14: 画像ありは Body を S3 に置き、項目は URI だけを持つ。"""
        image = _png_image(survey_world)
        survey = _queue(survey_world, clock, count=10, images=[image])

        rm.advance()

        lines = runner.inputs[survey.id]
        assert all(
            set(line) == {"record_id", "model_id", "body_s3_uri"} for line in lines
        )
        assert len(runner.bodies) == 10
        body = next(iter(runner.bodies.values()))
        content = body["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image"
        assert content[-1]["type"] == "text"

    def test_unreadable_image_is_an_error(self, survey_world, rm, runner, clock):
        image = _png_image(survey_world, upload=False)
        survey = _queue(survey_world, clock, images=[image])

        rm.advance()

        assert _get(survey_world, survey.id).error_code == (
            ErrorCode.SURVEY_TEMPLATE_IMAGE_UNAVAILABLE.value
        )
        assert runner.started == []

    def test_missing_custom_data_is_an_error(self, survey_world, rm, runner, clock):
        data = survey_world.custom_data("営業データ", "sales")
        survey = _queue(survey_world, clock, datasource=f"custom:{data.id}")
        survey_world.custom_store.delete(data.id, data.name)

        rm.advance()

        assert _get(survey_world, survey.id).error_code == (
            ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND.value
        )

    def test_custom_data_columns_reach_the_prompt(
        self, survey_world, rm, runner, clock
    ):
        data = survey_world.custom_data("営業データ", "sales")
        survey = _queue(survey_world, clock, datasource=f"custom:{data.id}")

        rm.advance()

        assert "趣味" in runner.inputs[survey.id][0]["body"]["system"]
        assert "hobby" in runner.personas[survey.id].columns

    def test_dataset_not_placed_is_an_error_at_once(
        self, survey_world, rm, runner, batch, clock, monkeypatch
    ):
        from src.config import config

        monkeypatch.setattr(config, "NEMOTRON_SOURCE", "s3")
        batch.has_parquet_uri.return_value = False
        survey = _queue(survey_world, clock)

        rm.advance()

        assert _get(survey_world, survey.id).error_code == (
            ErrorCode.NEMOTRON_DATASET_NOT_PLACED.value
        )

    @pytest.mark.parametrize("operation", ["write_input", "start_execution"])
    def test_access_failure_keeps_the_survey_waiting(
        self, survey_world, rm, runner, clock, operation, logs
    ):
        """NFR4.9: 権限・設定の失敗では start_attempts を増やさず、start_blocked_since を書く。"""
        survey = _queue(survey_world, clock)
        runner.fail[operation] = _error(FailureClass.ACCESS)

        rm.advance()

        waiting = _get(survey_world, survey.id)
        assert waiting.status == "queued"
        assert waiting.start_attempts == 0
        assert waiting.start_blocked_since == T0
        assert logs.events("survey.runner_waiting")[0]["level"] == "ERROR"

    def test_blocked_start_gives_up_after_30_minutes_for_that_survey_only(
        self, survey_world, rm, runner, clock
    ):
        """NFR4.9a: 30 分たっても続いたら、その 1 件だけ SURVEY_RUNNER_UNAVAILABLE。"""
        first = _queue(survey_world, clock, at=T0)
        second = _queue(survey_world, clock, at=T0 + timedelta(seconds=1))
        runner.fail["start_execution"] = _error(FailureClass.ACCESS)
        rm.advance()

        clock.advance(minutes=30)
        rm.advance()
        assert _get(survey_world, first.id).status == "queued"

        clock.advance(seconds=1)
        rm.advance()
        assert _get(survey_world, first.id).error_code == (
            ErrorCode.SURVEY_RUNNER_UNAVAILABLE.value
        )
        assert _get(survey_world, second.id).status == "queued"

    def test_successful_start_clears_start_blocked_since(
        self, survey_world, rm, runner, clock
    ):
        survey = _queue(survey_world, clock)
        runner.fail["start_execution"] = _error(FailureClass.ACCESS)
        rm.advance()
        del runner.fail["start_execution"]

        rm.advance()

        running = _get(survey_world, survey.id)
        assert running.status == "running"
        assert running.start_blocked_since is None

    def test_transient_failure_retries_three_times(
        self, survey_world, rm, runner, clock
    ):
        """BR3.2: 一時的な失敗は 3 回まで次の回で始め直す。"""
        survey = _queue(survey_world, clock)
        runner.fail["start_execution"] = _error(FailureClass.TRANSIENT)

        rm.advance()
        rm.advance()
        assert _get(survey_world, survey.id).start_attempts == 2
        assert _get(survey_world, survey.id).status == "queued"

        rm.advance()
        failed = _get(survey_world, survey.id)
        assert failed.error_code == ErrorCode.SURVEY_EXECUTION_FAILED.value
        assert survey.id in runner.deleted

    def test_sampling_failure_is_transient(self, survey_world, rm, batch, clock):
        survey = _queue(survey_world, clock)
        batch.filter_and_sample_personas.side_effect = RuntimeError("duckdb s3 timeout")

        rm.advance()

        assert _get(survey_world, survey.id).start_attempts == 1


def _png_image(world: SurveyWorld, *, upload: bool = True) -> TemplateImage:
    import io

    from PIL import Image

    key = "uploads/a/survey_image/11111111-1111-4111-8111-111111111111_a.png"
    if upload:
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
        world.s3.upload_file(buffer.getvalue(), key)
    return TemplateImage(
        id="img1",
        name="商品画像",
        file_path=f"s3://{BUCKET}/{key}",
        mime_type="image/png",
        original_filename="a.png",
    )


# =============================================================================
# ログと例外（NFR7.2〜NFR7.9、NFR5.12、NFR4.13、NFR4.15）
# =============================================================================


class TestLogsAndRobustness:
    def test_start_and_completion_are_logged_with_id_and_arn(
        self, survey_world, rm, runner, clock, logs
    ):
        survey = _queue(survey_world, clock)
        rm.advance()
        _succeed(runner, survey)
        rm.advance()

        started = logs.events("survey.run_started")[0]
        assert started["survey_id"] == survey.id
        assert started["execution_arn"] == _arn(survey.id)
        completed = logs.events("survey.run_completed")[0]
        assert completed["survey_id"] == survey.id

    def test_errors_are_logged_with_code_and_arn(
        self, survey_world, rm, runner, clock, logs
    ):
        survey = _queue(survey_world, clock)
        rm.advance()
        runner.finish(survey.id, "FAILED")
        rm.advance()

        failed = logs.events("survey.execution_failed")[0]
        assert failed["survey_id"] == survey.id
        assert failed["error_code"] == ErrorCode.SURVEY_INFERENCE_FAILED.value
        assert failed["execution_arn"] == _arn(survey.id)
        assert failed["level"] == "ERROR"

    def test_no_persona_or_answer_reaches_the_logs(
        self, survey_world, rm, runner, clock, capsys, caplog
    ):
        """NFR5.12・NFR7.9: 属性・依頼の本文・回答はログに出さない。"""
        survey = _queue(survey_world, clock)
        rm.advance()
        _succeed(runner, survey, failed=0)
        rm.advance()

        written = capsys.readouterr().out + caplog.text
        for secret in ("秘密のプロフィール", "釣り", "好きな色", "赤"):
            assert secret not in written

    def test_a_slow_round_is_warned(self, survey_world, rm, clock, logs, monkeypatch):
        """NFR7.8: 1 回の仕事が 60 秒を超えたら WARNING。"""
        real = survey_world.survey_store.list_run_queue

        def slow_query():
            clock.advance(seconds=61)
            return real()

        monkeypatch.setattr(survey_world.survey_store, "list_run_queue", slow_query)

        rm.advance()

        slow = logs.events("survey.runner_slow")
        assert slow and slow[0]["level"] == "WARNING"
        assert slow[0]["seconds"] >= 61

    def test_exceptions_never_escape(self, survey_world, rm, logs, monkeypatch):
        """NFR4.13・NFR4.15: DynamoDB が使えなくても回は例外を外に出さない。"""
        from src.services.stores.survey_store import SurveyStoreError

        monkeypatch.setattr(
            survey_world.survey_store,
            "list_run_queue",
            Mock(side_effect=SurveyStoreError("down")),
        )

        rm.advance()

        assert logs.events("survey.runner_round_failed")[0]["error_type"] == (
            "SurveyStoreError"
        )

    def test_lease_table_failure_does_not_escape(self, survey_world, rm, monkeypatch):
        from src.services.stores.survey_run_lease_store import SurveyRunLeaseStoreError

        monkeypatch.setattr(
            survey_world.lease_store,
            "acquire",
            Mock(side_effect=SurveyRunLeaseStoreError("down")),
        )

        rm.advance()


def test_coded_error_kinds_decide_start_failures():
    """VALIDATION・NOT_FOUND・CONFIG のコードはその場でエラー、ほかは一時的（WF4 の 6）。"""
    from src.managers.survey_run_manager import is_lasting_failure

    assert is_lasting_failure(CodedError(code=ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS))
    assert is_lasting_failure(CodedError(code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND))
    assert is_lasting_failure(CodedError(code=ErrorCode.NEMOTRON_DATASET_NOT_PLACED))
    assert is_lasting_failure(
        CodedError(code=ErrorCode.SURVEY_TEMPLATE_IMAGE_UNAVAILABLE)
    )
    assert not is_lasting_failure(CodedError(code=ErrorCode.SURVEY_OPERATION_FAILED))
    assert not is_lasting_failure(RuntimeError("x"))
