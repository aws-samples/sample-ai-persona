"""OwnerDirectory（所有者の名前の写し）の単体テスト。

BR5.1〜BR5.3、NFR2.7、NFR2.8、NFR5.6、NFR8.13、performance-design.md §3。
時計と Service はモック、取り直しの別スレッドは注入した実行器で走らせる。
"""

import json
from typing import Any, Callable
from unittest.mock import Mock

import pytest

from src.managers.components.owner_directory import (
    FAILURE_BACKOFF_SECONDS,
    MAX_STALENESS_SECONDS,
    OWNER_UNKNOWN,
    REFRESH_INTERVAL_SECONDS,
    OwnerDirectory,
)
from src.models.errors import CodedError, LookupUnavailableError
from src.models.sharing import OwnerSummary

USERS = [
    OwnerSummary("s1", "中村 由美", True),
    OwnerSummary("s2", "停止中", False),
    OwnerSummary("s3", None, True),
]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Deferred:
    """An executor that keeps the job until the test runs it."""

    def __init__(self) -> None:
        self.jobs: list[Callable[[], None]] = []

    def __call__(self, job: Callable[[], None]) -> None:
        self.jobs.append(job)

    def run_all(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def users() -> Mock:
    source = Mock()
    source.list_users.return_value = list(USERS)
    return source


@pytest.fixture
def deferred() -> Deferred:
    return Deferred()


def _directory(users: Mock, clock: FakeClock, executor: Any) -> OwnerDirectory:
    return OwnerDirectory(users, clock=clock, run_in_background=executor)


def _warm(users: Mock, clock: FakeClock) -> OwnerDirectory:
    directory = _directory(users, clock, lambda job: job())
    directory.labels(["s1"])  # 初回の取り直しを同期で済ませる
    return directory


def _stale_events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    return [r for r in map(json.loads, lines) if r["event"] == "authz.owners_stale"]


class TestLabels:
    def test_names_and_unknown_owners(self, users, clock):
        directory = _warm(users, clock)
        assert directory.labels(["s1", "s2", "s3", "gone"]) == {
            "s1": "中村 由美",
            "s2": OWNER_UNKNOWN,
            "s3": OWNER_UNKNOWN,
            "gone": OWNER_UNKNOWN,
        }

    def test_missing_owner_sub_is_unknown(self, users, clock):
        directory = _warm(users, clock)
        assert directory.label(None) == OWNER_UNKNOWN
        assert directory.label("s1") == "中村 由美"

    def test_no_email_is_used(self, users, clock):
        assert OwnerSummary.__dataclass_fields__.keys() == {
            "sub",
            "display_name",
            "enabled",
        }

    def test_has_no_decision_port(self):
        public = {n for n in dir(OwnerDirectory) if not n.startswith("_")}
        assert public == {"labels", "label"}


class TestCopyLifetime:
    def test_startup_without_copy_is_unavailable_and_does_not_wait(
        self, users, clock, deferred
    ):
        directory = _directory(users, clock, deferred)

        assert directory.labels(["s1"]) is None
        assert len(deferred.jobs) == 1
        users.list_users.assert_not_called()
        deferred.run_all()
        assert directory.labels(["s1"]) == {"s1": "中村 由美"}

    def test_fresh_copy_does_not_call_cognito(self, users, clock):
        directory = _warm(users, clock)
        clock.now += REFRESH_INTERVAL_SECONDS - 1
        directory.labels(["s1"])
        assert users.list_users.call_count == 1

    def test_expired_copy_is_served_while_one_refresh_runs(
        self, users, clock, deferred
    ):
        directory = _warm(users, clock)
        directory._run_in_background = deferred  # type: ignore[attr-defined]
        users.list_users.return_value = [OwnerSummary("s1", "新しい名前", True)]
        clock.now += REFRESH_INTERVAL_SECONDS + 1

        assert directory.labels(["s1"]) == {"s1": "中村 由美"}
        assert directory.labels(["s1"]) == {"s1": "中村 由美"}
        assert len(deferred.jobs) == 1  # 取り直しは 1 つだけ

        deferred.run_all()
        assert directory.labels(["s1"]) == {"s1": "新しい名前"}

    def test_failure_serves_previous_copy_and_logs(self, users, clock, capsys):
        directory = _warm(users, clock)
        capsys.readouterr()
        users.list_users.side_effect = LookupUnavailableError("down")
        clock.now += REFRESH_INTERVAL_SECONDS + 1

        assert directory.labels(["s1"]) == {"s1": "中村 由美"}

        (event,) = _stale_events(capsys)
        assert event["served"] == "stale"
        assert event["age_seconds"] == pytest.approx(REFRESH_INTERVAL_SECONDS + 1)

    def test_backoff_after_failure(self, users, clock):
        directory = _warm(users, clock)
        users.list_users.side_effect = LookupUnavailableError("down")
        clock.now += REFRESH_INTERVAL_SECONDS + 1
        directory.labels(["s1"])
        clock.now += FAILURE_BACKOFF_SECONDS - 1
        directory.labels(["s1"])
        assert users.list_users.call_count == 2
        clock.now += 2
        directory.labels(["s1"])
        assert users.list_users.call_count == 3

    def test_copy_older_than_an_hour_is_not_used(self, users, clock, capsys):
        directory = _warm(users, clock)
        users.list_users.side_effect = CodedError("down")
        clock.now += MAX_STALENESS_SECONDS + 1

        assert directory.labels(["s1"]) is None
        assert _stale_events(capsys)[-1]["served"] == "unavailable"

    def test_failure_at_startup_is_unavailable(self, users, clock, capsys):
        users.list_users.side_effect = LookupUnavailableError("down")
        directory = _directory(users, clock, lambda job: job())

        assert directory.labels(["s1"]) is None
        assert directory.labels(["s1"]) is None
        assert _stale_events(capsys)[0]["served"] == "unavailable"

    def test_unexpected_error_in_the_background_is_contained(self, users, clock):
        users.list_users.side_effect = RuntimeError("bug")
        directory = _directory(users, clock, lambda job: job())
        assert directory.labels(["s1"]) is None

    def test_thread_start_failure_is_a_failure_and_does_not_block_later_refreshes(
        self, users, clock, capsys
    ):
        """R-03: a refresh that cannot start must not stay "running" forever."""
        directory = _warm(users, clock)
        capsys.readouterr()
        broken = Mock(side_effect=RuntimeError("can't start new thread"))
        directory._run_in_background = broken  # type: ignore[attr-defined]
        clock.now += REFRESH_INTERVAL_SECONDS + 1

        assert directory.labels(["s1"]) == {"s1": "中村 由美"}  # 前の写しを返す
        (event,) = _stale_events(capsys)
        assert event["served"] == "stale"

        directory._run_in_background = lambda job: job()  # type: ignore[attr-defined]
        users.list_users.return_value = [OwnerSummary("s1", "新しい名前", True)]
        clock.now += FAILURE_BACKOFF_SECONDS + 1
        assert directory.labels(["s1"]) == {"s1": "新しい名前"}
        assert broken.call_count == 1
