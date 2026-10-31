"""進行役のループ（web/routers/_survey_runner.py）と lifespan の API テスト。

- BR9.1: ステートマシンが設定されているときだけループを起こす
- NFR1.5: 各回はスレッドで動き、イベントループを止めない
- 同じプロセスでは回を直列にする（functional-spec.md WF2）
- NFR4.10・R-08: 停止の順序。止まる印を立てたら、ループの回も受付・削除のあとの回も
  新しく始めない。走っている回を最長 30 秒待ち、終わっていれば担当者の記録を手放す。
  終わらなければ手放さずに止まる（期限切れに任せる）
"""

import asyncio
import threading
import time
from unittest.mock import Mock, patch

import pytest

from web.routers._survey_runner import SurveyRunnerLoop


def _wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class Rounds:
    """A fake ``SurveyRunManager.advance`` that records concurrency."""

    def __init__(self, hold: threading.Event | None = None) -> None:
        self.calls = 0
        self.running = 0
        self.max_running = 0
        self.hold = hold
        self.started = threading.Event()
        self._lock = threading.Lock()

    def __call__(self) -> None:
        with self._lock:
            self.calls += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.started.set()
        if self.hold is not None:
            self.hold.wait(5)
        with self._lock:
            self.running -= 1


class TestWake:
    def test_wake_returns_at_once_and_runs_a_round_in_a_thread(self):
        """NFR1.4: 受付は進行役の回を待たない。"""
        hold = threading.Event()
        rounds = Rounds(hold)
        loop = SurveyRunnerLoop(rounds, Mock(return_value=True))

        began = time.monotonic()
        assert loop.wake()
        assert time.monotonic() - began < 0.5
        assert rounds.started.wait(2)
        assert threading.current_thread() is threading.main_thread()
        hold.set()

    def test_rounds_are_serialized_in_one_process(self):
        hold = threading.Event()
        rounds = Rounds(hold)
        loop = SurveyRunnerLoop(rounds, Mock(return_value=True))

        loop.wake()
        assert rounds.started.wait(2)
        loop.wake()
        loop.wake()
        time.sleep(0.05)
        hold.set()

        assert _wait_until(lambda: rounds.running == 0 and rounds.calls >= 2)
        assert rounds.max_running == 1

    def test_wakes_during_a_long_round_wait_in_one_thread(self):
        """長い回の間の起動の通知は、待つスレッド 1 本にまとまる。"""
        hold = threading.Event()
        rounds = Rounds(hold)
        loop = SurveyRunnerLoop(rounds, Mock(return_value=True))

        def wake_threads() -> int:
            return sum(t.name == "survey-run-wake" for t in threading.enumerate())

        loop.wake()
        assert rounds.started.wait(2)
        woken = [loop.wake() for _ in range(15)]
        time.sleep(0.05)

        assert woken == [True] + [False] * 14
        assert wake_threads() == 2
        hold.set()
        assert _wait_until(lambda: rounds.running == 0 and rounds.calls == 2)
        assert _wait_until(lambda: wake_threads() == 0)

    def test_a_failing_round_does_not_break_the_next(self):
        """NFR4.15: 回の中の例外でループは止まらない。"""
        advance = Mock(side_effect=[RuntimeError("boom"), None])
        loop = SurveyRunnerLoop(advance, Mock(return_value=True))

        loop.wake()
        assert _wait_until(lambda: advance.call_count == 1)
        time.sleep(0.02)
        loop.wake()

        assert _wait_until(lambda: advance.call_count == 2)


class TestRunForever:
    def test_rounds_repeat_at_the_interval_off_the_event_loop(self):
        """NFR1.5: 各回はスレッドで動く（イベントループのスレッドで回さない）。"""
        threads: list[threading.Thread] = []

        def advance() -> None:
            threads.append(threading.current_thread())

        loop = SurveyRunnerLoop(advance, Mock(return_value=True), interval_seconds=0.01)

        async def scenario() -> None:
            task = asyncio.create_task(loop.run_forever())
            while len(threads) < 3:
                await asyncio.sleep(0.01)
            await loop.stop()
            await asyncio.wait_for(task, 2)

        asyncio.run(scenario())
        assert len(threads) >= 3
        assert all(t is not threading.main_thread() for t in threads)


class TestStop:
    def test_no_new_round_after_the_stop_mark(self):
        rounds = Rounds()
        release = Mock(return_value=True)
        loop = SurveyRunnerLoop(rounds, release)

        asyncio.run(loop.stop())

        assert not loop.wake()
        time.sleep(0.05)
        assert rounds.calls == 0
        release.assert_called_once()

    def test_running_round_is_waited_for_then_the_lease_released(self):
        hold = threading.Event()
        rounds = Rounds(hold)
        release = Mock(return_value=True)
        loop = SurveyRunnerLoop(rounds, release, stop_wait_seconds=2)
        loop.wake()
        assert rounds.started.wait(2)

        async def scenario() -> None:
            stopping = asyncio.create_task(loop.stop())
            await asyncio.sleep(0.05)
            assert not release.called  # the round still runs
            hold.set()
            await asyncio.wait_for(stopping, 3)

        asyncio.run(scenario())
        release.assert_called_once()

    def test_the_loop_runs_again_after_a_stop(self):
        """同じプロセスで lifespan をもう一度始めても（テストの TestClient など）、回が動く。"""
        rounds = Rounds()
        loop = SurveyRunnerLoop(rounds, Mock(return_value=True), interval_seconds=60)

        woken: list[bool] = []

        async def one_lifespan() -> None:
            before = rounds.calls
            async with loop.running():
                for _ in range(200):
                    if rounds.calls > before:
                        break
                    await asyncio.sleep(0.01)
                woken.append(loop.wake())

        asyncio.run(one_lifespan())
        before = rounds.calls
        asyncio.run(one_lifespan())

        assert rounds.calls > before
        assert woken == [True, True]

    def test_round_longer_than_the_wait_keeps_the_lease(self):
        """走っている回があるうちに記録を消すと、ほかのタスクが同じ仕事を始めるため。"""
        hold = threading.Event()
        rounds = Rounds(hold)
        release = Mock(return_value=True)
        loop = SurveyRunnerLoop(rounds, release, stop_wait_seconds=0.05)
        loop.wake()
        assert rounds.started.wait(2)

        asyncio.run(loop.stop())

        release.assert_not_called()
        hold.set()

    def test_the_default_wait_is_30_seconds(self):
        loop = SurveyRunnerLoop(Mock(), Mock())
        assert loop.stop_wait_seconds == 30
        assert loop.interval_seconds == 15


class TestLifespan:
    """BR9.1: 設定があるときだけループを起こし、止めるときは stop を待つ。"""

    def test_configured_runner_loop_starts_and_stops_with_the_app(self, test_app):
        from fastapi.testclient import TestClient

        started = threading.Event()
        advance = Mock(side_effect=lambda: started.set())
        release = Mock(return_value=True)
        loop = SurveyRunnerLoop(advance, release, interval_seconds=0.01)
        with patch("web.routers.survey.get_survey_runner_loop", return_value=loop):
            with TestClient(test_app):
                assert started.wait(2)
            release.assert_called_once()
            calls = advance.call_count
        time.sleep(0.05)
        assert advance.call_count == calls  # no round after the stop
        assert not loop.wake()

    def test_no_loop_without_configuration(self, test_app):
        from fastapi.testclient import TestClient

        with patch("web.routers.survey.get_survey_runner_loop", return_value=None):
            with TestClient(test_app) as client:
                assert client.get("/health").status_code == 200

    def test_loop_is_none_when_surveys_cannot_run(self, monkeypatch):
        import web.routers.survey as survey_router
        from src.config import config

        monkeypatch.setattr(config, "SURVEY_STATE_MACHINE_ARN", None)
        monkeypatch.setattr(survey_router, "_runner_loop", None)

        assert survey_router.get_survey_runner_loop() is None


@pytest.fixture(autouse=True)
def _no_real_runner(monkeypatch):
    """Never build the real SurveyRunManager in these tests."""
    import web.routers.survey as survey_router

    monkeypatch.setattr(survey_router, "_runner_loop", None)
