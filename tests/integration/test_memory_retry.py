"""Retry of AgentCore Memory calls (src/services/memory/retry.py).

Covers transient-error detection, exponential backoff delays, and the decorator's
retry count and raised exception. time.sleep and random.random are replaced so
the tests never wait in real time.
"""

from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from src.services.memory import retry as retry_module
from src.services.memory.retry import (
    RetryExhaustedError,
    calculate_backoff_delay,
    is_transient_error,
    with_retry,
)


def _client_error(code: str, operation: str = "ListMemoryRecords") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}}, operation)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every backoff sleep instead of waiting."""
    recorded: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", recorded.append)
    return recorded


class _Flaky:
    """A callable that raises the queued errors in order, then returns ``result``."""

    def __init__(self, errors: list[Exception], result: Any = "ok") -> None:
        self.errors = list(errors)
        self.result = result
        self.calls = 0
        self.__name__ = "flaky_call"

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


# --- is_transient_error ---------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "ServiceUnavailable",
        "InternalServerError",
        "ServiceException",
    ],
)
def test_throttling_and_server_error_codes_are_transient(code):
    assert is_transient_error(_client_error(code)) is True


@pytest.mark.parametrize(
    "code",
    [
        "ValidationException",
        "AccessDeniedException",
        "ResourceNotFoundException",
        "InvalidInputException",
    ],
)
def test_client_side_error_codes_are_not_transient(code):
    assert is_transient_error(_client_error(code)) is False


def test_client_error_without_a_code_is_not_transient():
    assert is_transient_error(ClientError({}, "ListMemoryRecords")) is False


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError(),
        ConnectionResetError(),
        TimeoutError(),
        EndpointConnectionError(endpoint_url="https://bedrock-agentcore.invalid"),
        ReadTimeoutError(endpoint_url="https://bedrock-agentcore.invalid"),
        ConnectTimeoutError(endpoint_url="https://bedrock-agentcore.invalid"),
    ],
)
def test_connection_and_timeout_errors_are_transient(error):
    assert is_transient_error(error) is True


@pytest.mark.parametrize("error", [ValueError("x"), KeyError("k"), RuntimeError()])
def test_programming_errors_are_not_transient(error):
    assert is_transient_error(error) is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "AgentCore Memory のスロットリングは ThrottledException だが、"
        "TRANSIENT_ERROR_CODES は ThrottlingException しか持たない（retry.py:24-35）"
    ),
)
def test_agentcore_throttled_exception_is_transient():
    assert is_transient_error(_client_error("ThrottledException")) is True


# --- calculate_backoff_delay ----------------------------------------------


def test_delay_doubles_per_attempt_without_jitter():
    delays = [
        calculate_backoff_delay(attempt, base_delay=0.5, jitter=False)
        for attempt in range(4)
    ]
    assert delays == [0.5, 1.0, 2.0, 4.0]


def test_delay_is_capped_at_max_delay_without_jitter():
    assert calculate_backoff_delay(10, base_delay=1.0, max_delay=8.0, jitter=False) == (
        8.0
    )


@pytest.mark.parametrize("draw, factor", [(0.0, 0.5), (0.5, 1.0), (0.999, 1.499)])
def test_jitter_scales_the_delay_between_half_and_one_and_a_half(
    monkeypatch, draw, factor
):
    monkeypatch.setattr(retry_module.random, "random", lambda: draw)
    delay = calculate_backoff_delay(2, base_delay=1.0, jitter=True)
    assert delay == pytest.approx(4.0 * factor)


def test_real_jitter_stays_inside_its_range():
    for _ in range(200):
        delay = calculate_backoff_delay(3, base_delay=1.0, max_delay=60.0)
        assert 4.0 <= delay < 12.0


# --- with_retry -----------------------------------------------------------


def test_success_on_first_attempt_does_not_sleep(sleeps):
    func = _Flaky([], result=42)
    assert with_retry(max_retries=3)(func)() == 42
    assert func.calls == 1
    assert sleeps == []


def test_transient_errors_are_retried_with_exponential_backoff(sleeps):
    func = _Flaky([_client_error("ThrottlingException"), ConnectionError()])
    wrapped = with_retry(max_retries=3, base_delay=1.0, jitter=False)(func)

    assert wrapped() == "ok"
    assert func.calls == 3
    assert sleeps == [1.0, 2.0]


def test_arguments_are_passed_through_on_every_attempt(sleeps):
    seen: list[tuple[Any, ...]] = []

    def func(a: int, *, b: str) -> str:
        seen.append((a, b))
        if len(seen) == 1:
            raise TimeoutError()
        return f"{a}{b}"

    assert with_retry(max_retries=1, jitter=False)(func)(1, b="x") == "1x"
    assert seen == [(1, "x"), (1, "x")]


def test_jittered_sleeps_stay_inside_the_backoff_range(sleeps):
    func = _Flaky([TimeoutError()] * 3)
    with_retry(max_retries=3, base_delay=1.0, max_delay=60.0)(func)()

    for attempt, delay in enumerate(sleeps):
        nominal = 1.0 * 2**attempt
        assert 0.5 * nominal <= delay < 1.5 * nominal


def test_exhausted_retries_raise_retry_exhausted_with_the_last_error(sleeps):
    errors = [_client_error("ThrottlingException") for _ in range(3)]
    func = _Flaky(list(errors))
    wrapped = with_retry(max_retries=2, base_delay=1.0, jitter=False)(func)

    with pytest.raises(RetryExhaustedError) as exc_info:
        wrapped()

    assert func.calls == 3
    assert sleeps == [1.0, 2.0]
    assert exc_info.value.attempts == 3
    assert exc_info.value.last_exception is errors[-1]
    assert exc_info.value.__cause__ is errors[-1]


@pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
def test_non_transient_client_error_is_raised_immediately(sleeps, code):
    error = _client_error(code)
    func = _Flaky([error])

    with pytest.raises(ClientError) as exc_info:
        with_retry(max_retries=3)(func)()

    assert exc_info.value is error
    assert func.calls == 1
    assert sleeps == []


def test_explicit_retryable_exceptions_replace_automatic_detection(sleeps):
    class Retryable(Exception):
        pass

    # Retryable is not transient by auto-detection, but it is retried when named explicitly
    func = _Flaky([Retryable()])
    assert with_retry(max_retries=1, retryable_exceptions=(Retryable,))(func)() == "ok"
    assert func.calls == 2

    # With an explicit list, errors that auto-detection would call transient are not retried
    throttled = _Flaky([_client_error("ThrottlingException")])
    with pytest.raises(ClientError):
        with_retry(max_retries=3, retryable_exceptions=(Retryable,))(throttled)()
    assert throttled.calls == 1


def test_on_retry_receives_error_attempt_number_and_delay(sleeps):
    first, second = TimeoutError("a"), TimeoutError("b")
    calls: list[tuple[Exception, int, float]] = []
    func = _Flaky([first, second])

    with_retry(
        max_retries=2,
        base_delay=0.25,
        jitter=False,
        on_retry=lambda e, n, d: calls.append((e, n, d)),
    )(func)()

    assert calls == [(first, 1, 0.25), (second, 2, 0.5)]


def test_failing_on_retry_callback_does_not_stop_the_retry(sleeps):
    def broken_callback(error: Exception, attempt: int, delay: float) -> None:
        raise RuntimeError("callback bug")

    func = _Flaky([TimeoutError()])
    assert with_retry(max_retries=1, on_retry=broken_callback)(func)() == "ok"
    assert func.calls == 2


def test_zero_retries_makes_a_single_attempt(sleeps):
    func = _Flaky([TimeoutError()])
    with pytest.raises(RetryExhaustedError) as exc_info:
        with_retry(max_retries=0)(func)()
    assert func.calls == 1
    assert exc_info.value.attempts == 1
    assert sleeps == []


def test_wrapper_keeps_the_function_name():
    def save_knowledge() -> None:
        pass

    assert with_retry()(save_knowledge).__name__ == "save_knowledge"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "最終試行で起きた非一時的エラーは即時送出されず RetryExhaustedError に包まれる。"
        "上限判定（retry.py:167）がリトライ可否の判定（retry.py:181-196）より先にあるため"
    ),
)
def test_non_transient_error_on_the_last_attempt_is_raised_unwrapped(sleeps):
    denied = _client_error("AccessDeniedException")
    func = _Flaky([TimeoutError(), denied])

    with pytest.raises(ClientError):
        with_retry(max_retries=1)(func)()
