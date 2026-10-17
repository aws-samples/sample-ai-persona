"""CircuitBreaker（src/services/cognito/circuit_breaker.py）のテスト。

U8 NFR Design Q1 A: OidcService の中にあった回路遮断を Cognito のサブパッケージに
移し、OidcService と CognitoAuthService が同じ部品を使う。遮断中の ``before_call()``
は中立の ``CircuitOpenError``（CodedError ではない）を投げ、各 Service が自分の
エラーコード（OIDC_UNAVAILABLE、LOGIN_UNAVAILABLE）に変える（NFR9.44）。
"""

import pytest

from src.models.errors import CodedError
from src.services.cognito.circuit_breaker import CircuitBreaker, CircuitOpenError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def opened() -> list[str]:
    return []


@pytest.fixture
def breaker(clock, opened) -> CircuitBreaker:
    b = CircuitBreaker(clock=clock)
    b.add_open_listener(lambda: opened.append("opened"))
    return b


def _fail(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        breaker.before_call()
        breaker.record_failure()


def test_open_error_is_neutral():
    """Service ごとのコードに変えられるよう、コード付きの例外ではない。"""
    assert issubclass(CircuitOpenError, Exception)
    assert not issubclass(CircuitOpenError, CodedError)


def test_four_failures_keep_it_closed(breaker, opened):
    _fail(breaker, 4)
    breaker.before_call()
    assert opened == []


def test_opens_at_five_consecutive_failures(breaker, opened):
    _fail(breaker, 5)
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    assert opened == ["opened"]


def test_success_resets_the_count(breaker, opened):
    _fail(breaker, 4)
    breaker.record_success()
    _fail(breaker, 4)
    breaker.before_call()
    assert opened == []


def test_stays_open_for_sixty_seconds_then_allows_one_trial(breaker, clock):
    _fail(breaker, 5)
    clock.now += 59
    with pytest.raises(CircuitOpenError):
        breaker.before_call()

    clock.now += 1
    breaker.before_call()  # 半開の 1 回
    with pytest.raises(CircuitOpenError):
        breaker.before_call()  # 結果が出るまで他は通さない


def test_trial_success_closes_it(breaker, clock):
    _fail(breaker, 5)
    clock.now += 60
    breaker.before_call()
    breaker.record_success()
    breaker.before_call()
    breaker.before_call()


def test_trial_failure_reopens_and_notifies_again(breaker, clock, opened):
    _fail(breaker, 5)
    clock.now += 60
    breaker.before_call()
    breaker.record_failure()
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    assert opened == ["opened", "opened"]


def test_failures_while_open_do_not_notify_twice(breaker, opened):
    _fail(breaker, 5)
    breaker.record_failure()
    assert opened == ["opened"]
