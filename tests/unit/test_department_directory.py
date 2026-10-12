"""DepartmentDirectory（部署の一覧、約束 C4、ADR-009）の単体テスト。

BR2.11（5 分ごとに取り直し、失敗時は 1 時間まで前の一覧）、NFR8.8
（authz.departments_stale）、NFR9.14（5 分以内は Cognito を呼ばない）、NFR9.17
（失敗後 60 秒は呼び直さない）、NFR9.19、NFR Design R-02 の 4 つの穴。
時刻は注入した時計で進める。並行のテストだけ threading の待ちを使う。
"""

import json
import threading

import pytest

from src.managers.components.department_directory import (
    FAILURE_BACKOFF_SECONDS,
    INITIAL_WAIT_SECONDS,
    MAX_STALENESS_SECONDS,
    REFRESH_INTERVAL_SECONDS,
    DepartmentDirectory,
)
from src.models.authz import Department
from src.models.errors import CodedError, ErrorCode, LookupUnavailableError
from tests.error_helpers import raises_code

SALES = Department(group_id="sales", display_name="営業部")
DEV = Department(group_id="dev", display_name="開発部")
HR = Department(group_id="hr", display_name="人事部")
#: list_all() の順（表示名のコードポイント順: 営 U+55B6 < 開 U+958B）
SORTED = [SALES, DEV]


class FakeClock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class GroupsDown(CodedError):
    code = ErrorCode.LOOKUP_UNAVAILABLE


class StubGroups:
    """CognitoGroupsService stand-in that counts ListGroups calls."""

    def __init__(self, departments: list[Department] | None = None) -> None:
        self.departments = departments if departments is not None else [SALES, DEV]
        self.calls = 0
        self.fail = False
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def list_departments(self) -> list[Department]:
        with self._lock:
            self.calls += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise GroupsDown("cognito down")
        return list(self.departments)


def _directory(
    groups: StubGroups, clock: FakeClock, **kwargs: float
) -> DepartmentDirectory:
    return DepartmentDirectory(groups, clock=clock, **kwargs)


def _stale_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    return [line for line in lines if line.get("event") == "authz.departments_stale"]


class TestCaching:
    def test_limits(self):
        assert REFRESH_INTERVAL_SECONDS == 300
        assert FAILURE_BACKOFF_SECONDS == 60
        assert MAX_STALENESS_SECONDS == 3600
        assert INITIAL_WAIT_SECONDS == 5

    def test_within_five_minutes_cognito_is_not_called(self):
        groups, clock = StubGroups(), FakeClock()
        directory = _directory(groups, clock)
        directory.list_all()
        clock.now += REFRESH_INTERVAL_SECONDS - 1
        directory.list_all()
        assert groups.calls == 1

    def test_after_five_minutes_the_list_is_read_again(self):
        groups, clock = StubGroups(), FakeClock()
        directory = _directory(groups, clock)
        directory.list_all()
        groups.departments = [HR]
        clock.now += REFRESH_INTERVAL_SECONDS
        assert directory.list_all() == [HR]
        assert groups.calls == 2


class TestFailures:
    def _stale(self, groups: StubGroups, clock: FakeClock) -> DepartmentDirectory:
        directory = _directory(groups, clock)
        directory.list_all()
        groups.fail = True
        return directory

    def test_failure_serves_the_previous_list_for_up_to_an_hour(self, capsys):
        groups, clock = StubGroups(), FakeClock()
        directory = self._stale(groups, clock)
        clock.now += REFRESH_INTERVAL_SECONDS + 1

        assert directory.list_all() == SORTED

        (line,) = _stale_lines(capsys)
        assert line["served"] == "stale"
        assert line["age_seconds"] == REFRESH_INTERVAL_SECONDS + 1

    def test_no_new_call_for_sixty_seconds_after_a_failure(self):
        groups, clock = StubGroups(), FakeClock()
        directory = self._stale(groups, clock)
        clock.now += REFRESH_INTERVAL_SECONDS + 1
        directory.list_all()
        clock.now += FAILURE_BACKOFF_SECONDS - 1
        directory.list_all()
        assert groups.calls == 2
        clock.now += 1
        directory.list_all()
        assert groups.calls == 3

    def test_older_than_an_hour_is_503(self, capsys):
        groups, clock = StubGroups(), FakeClock()
        directory = self._stale(groups, clock)
        clock.now += MAX_STALENESS_SECONDS + 1

        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            directory.list_all()

        assert _stale_lines(capsys)[-1]["served"] == "unavailable"

    def test_first_read_failing_is_503(self):
        groups, clock = StubGroups(), FakeClock()
        groups.fail = True
        with pytest.raises(LookupUnavailableError):
            _directory(groups, clock).list_all()

    def test_hundred_calls_within_a_minute_of_failure_call_cognito_once(self):
        groups, clock = StubGroups(), FakeClock()
        groups.fail = True
        directory = _directory(groups, clock)
        for _ in range(100):
            with pytest.raises(LookupUnavailableError):
                directory.list_all()
            clock.now += 0.5
        assert groups.calls == 1


class TestConcurrentRefresh:
    def test_others_get_the_previous_list_without_waiting(self):
        groups, clock = StubGroups(), FakeClock()
        directory = _directory(groups, clock)
        directory.list_all()
        clock.now += REFRESH_INTERVAL_SECONDS + 1
        groups.gate = threading.Event()
        groups.entered.clear()
        refresher = threading.Thread(target=directory.list_all)
        refresher.start()
        assert groups.entered.wait(5)

        assert directory.list_all() == SORTED  # 取り直しの完了を待たない

        groups.gate.set()
        refresher.join(5)
        assert groups.calls == 2

    def test_others_get_503_when_the_previous_list_is_too_old(self):
        groups, clock = StubGroups(), FakeClock()
        directory = _directory(groups, clock)
        directory.list_all()
        clock.now += MAX_STALENESS_SECONDS + 1
        groups.gate = threading.Event()
        groups.entered.clear()
        refresher = threading.Thread(target=directory.list_all)
        refresher.start()
        assert groups.entered.wait(5)

        with pytest.raises(LookupUnavailableError):
            directory.list_all()

        groups.gate.set()
        refresher.join(5)

    def test_waiting_at_start_up_gets_the_result(self):
        groups, clock = StubGroups(), FakeClock()
        directory = _directory(groups, clock, initial_wait_seconds=5)
        groups.gate = threading.Event()
        refresher = threading.Thread(target=directory.list_all)
        refresher.start()
        assert groups.entered.wait(5)
        threading.Timer(0.05, groups.gate.set).start()

        assert directory.list_all() == SORTED
        refresher.join(5)

    def test_waiting_at_start_up_gives_up_after_the_limit(self, capsys):
        groups, clock = StubGroups(), FakeClock()
        directory = _directory(groups, clock, initial_wait_seconds=0.05)
        groups.gate = threading.Event()
        refresher = threading.Thread(target=directory.list_all)
        refresher.start()
        assert groups.entered.wait(5)

        with pytest.raises(LookupUnavailableError):
            directory.list_all()

        groups.gate.set()
        refresher.join(5)
        assert any(line["served"] == "unavailable" for line in _stale_lines(capsys))

    def test_ten_threads_at_start_up_call_cognito_once(self):
        groups, clock = StubGroups(), FakeClock()
        directory = _directory(groups, clock)
        groups.gate = threading.Event()
        results: list[list[Department]] = []
        threads = [
            threading.Thread(target=lambda: results.append(directory.list_all()))
            for _ in range(10)
        ]
        for thread in threads:
            thread.start()
        assert groups.entered.wait(5)
        groups.gate.set()
        for thread in threads:
            thread.join(5)

        assert groups.calls == 1
        assert results == [SORTED] * 10


class TestQueries:
    def test_list_all_is_sorted_by_display_name(self):
        directory = _directory(StubGroups([SALES, HR, DEV]), FakeClock())
        # 人事部 < 営業部 < 開発部（コードポイント順）
        assert directory.list_all() == sorted(
            [SALES, HR, DEV], key=lambda d: d.display_name
        )

    def test_get_many_leaves_out_unknown_ids(self):
        directory = _directory(StubGroups(), FakeClock())
        assert directory.get_many(frozenset({"sales", "gone"})) == {"sales": SALES}

    def test_exists(self):
        directory = _directory(StubGroups(), FakeClock())
        assert directory.exists("dev")
        assert not directory.exists("gone")
