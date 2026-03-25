"""
Retry Logic with Exponential Backoff
指数バックオフでリトライするデコレータとユーティリティ

Requirements:
    - 10.4: リトライロジック（指数バックオフ）
"""

import functools
import logging
import random
import time
from typing import Callable, Optional, Tuple, Type, TypeVar

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Type variable for generic return type
T = TypeVar("T")


# Transient error codes that should trigger retry
TRANSIENT_ERROR_CODES = frozenset(
    [
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "InternalServerError",
        "InternalFailure",
        "ServiceException",
        "TransientError",
    ]
)


class RetryExhaustedError(Exception):
    """リトライ回数を超過した場合のエラー"""

    def __init__(
        self, message: str, attempts: int, last_exception: Optional[Exception] = None
    ):
        super().__init__(message)
        self.attempts = attempts
        self.last_exception = last_exception


def is_transient_error(exception: Exception) -> bool:
    """
    一時的なエラー（リトライ可能）かどうかを判定

    Args:
        exception: 判定する例外

    Returns:
        リトライ可能な一時的エラーの場合True
    """
    # Check for botocore ClientError with transient error codes
    if isinstance(exception, ClientError):
        error_code = exception.response.get("Error", {}).get("Code", "")
        return error_code in TRANSIENT_ERROR_CODES

    # Check for connection/network errors
    error_name = type(exception).__name__
    transient_error_names = {
        "ConnectionError",
        "TimeoutError",
        "ConnectionResetError",
        "BrokenPipeError",
        "ConnectionRefusedError",
        "ConnectionAbortedError",
        "EndpointConnectionError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
    }

    return error_name in transient_error_names


def calculate_backoff_delay(
    attempt: int, base_delay: float = 1.0, max_delay: float = 60.0, jitter: bool = True
) -> float:
    """
    指数バックオフの遅延時間を計算

    Args:
        attempt: 現在の試行回数（0から開始）
        base_delay: 基本遅延時間（秒）
        max_delay: 最大遅延時間（秒）
        jitter: ジッターを追加するか（thundering herd防止）

    Returns:
        計算された遅延時間（秒）
    """
    # Exponential backoff: base_delay * 2^attempt
    delay = base_delay * (2**attempt)

    # Cap at max_delay
    delay = min(delay, max_delay)

    # Add jitter (0-100% of delay) to prevent thundering herd
    if jitter:
        delay = delay * (0.5 + random.random())

    return delay


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    retryable_exceptions: Optional[Tuple[Type[Exception], ...]] = None,
    on_retry: Optional[Callable[[Exception, int, float], None]] = None,
):
    """
    指数バックオフでリトライするデコレータ

    一時的なエラー（ネットワークエラー、スロットリング等）が発生した場合、
    指数バックオフで自動的にリトライする。

    Args:
        max_retries: 最大リトライ回数（デフォルト: 3）
        base_delay: 基本遅延時間（秒）（デフォルト: 1.0）
        max_delay: 最大遅延時間（秒）（デフォルト: 60.0）
        jitter: ジッターを追加するか（デフォルト: True）
        retryable_exceptions: リトライ対象の例外タプル（Noneの場合は自動判定）
        on_retry: リトライ時に呼び出されるコールバック関数

    Returns:
        デコレートされた関数

    Example:
        @with_retry(max_retries=3, base_delay=1.0)
        def call_external_service():
            # This will be retried on transient errors
            pass

    Requirements:
        - 10.4: リトライロジック（指数バックオフ）
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception: Optional[Exception] = None

            for attempt in range(max_retries + 1):  # +1 for initial attempt
                try:
                    return func(*args, **kwargs)

                except Exception as e:
                    last_exception = e

                    # Check if this is the last attempt
                    if attempt >= max_retries:
                        logger.error(
                            "All %d retry attempts exhausted for %s: %s",
                            max_retries + 1,
                            func.__name__,
                            str(e),
                        )
                        raise RetryExhaustedError(
                            f"Operation '{func.__name__}' failed after "
                            f"{max_retries + 1} attempts",
                            attempts=max_retries + 1,
                            last_exception=e,
                        ) from e

                    # Check if error is retryable
                    should_retry = False

                    if retryable_exceptions:
                        # Use explicit exception list
                        should_retry = isinstance(e, retryable_exceptions)
                    else:
                        # Use automatic transient error detection
                        should_retry = is_transient_error(e)

                    if not should_retry:
                        # Non-retryable error, re-raise immediately
                        logger.debug(
                            "Non-retryable error in %s: %s", func.__name__, str(e)
                        )
                        raise

                    # Calculate delay with exponential backoff
                    delay = calculate_backoff_delay(
                        attempt=attempt,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        jitter=jitter,
                    )

                    logger.warning(
                        "Retry attempt %d/%d for %s after %.2fs delay: %s",
                        attempt + 1,
                        max_retries,
                        func.__name__,
                        delay,
                        str(e),
                    )

                    # Call retry callback if provided
                    if on_retry:
                        try:
                            on_retry(e, attempt + 1, delay)
                        except Exception as callback_error:
                            logger.warning("Retry callback failed: %s", callback_error)

                    # Wait before retry
                    time.sleep(delay)

            # This should never be reached, but just in case
            raise RetryExhaustedError(
                f"Operation '{func.__name__}' failed unexpectedly",
                attempts=max_retries + 1,
                last_exception=last_exception,
            )

        return wrapper

    return decorator


class RetryContext:
    """
    リトライコンテキストマネージャー

    with文でリトライロジックを適用する場合に使用。

    Example:
        with RetryContext(max_retries=3) as ctx:
            while ctx.should_retry():
                try:
                    result = call_external_service()
                    break
                except Exception as e:
                    ctx.record_failure(e)
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        jitter: bool = True,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self._attempt = 0
        self._last_exception: Optional[Exception] = None
        self._success = False

    def __enter__(self) -> "RetryContext":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        # Don't suppress exceptions
        return False

    def should_retry(self) -> bool:
        """リトライすべきかどうかを返す"""
        return self._attempt <= self.max_retries and not self._success

    def record_failure(self, exception: Exception) -> None:
        """
        失敗を記録し、必要に応じて待機

        Args:
            exception: 発生した例外

        Raises:
            RetryExhaustedError: リトライ回数を超過した場合
        """
        self._last_exception = exception

        if self._attempt >= self.max_retries:
            raise RetryExhaustedError(
                f"Operation failed after {self.max_retries + 1} attempts",
                attempts=self.max_retries + 1,
                last_exception=exception,
            ) from exception

        # Check if error is retryable
        if not is_transient_error(exception):
            raise exception

        # Calculate and apply delay
        delay = calculate_backoff_delay(
            attempt=self._attempt,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            jitter=self.jitter,
        )

        logger.warning(
            "Retry attempt %d/%d after %.2fs delay: %s",
            self._attempt + 1,
            self.max_retries,
            delay,
            str(exception),
        )

        time.sleep(delay)
        self._attempt += 1

    def record_success(self) -> None:
        """成功を記録"""
        self._success = True

    @property
    def attempts(self) -> int:
        """現在の試行回数を返す"""
        return self._attempt + 1

    @property
    def last_exception(self) -> Optional[Exception]:
        """最後に発生した例外を返す"""
        return self._last_exception
