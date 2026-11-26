"""
Retry Logic with Exponential Backoff
Decorator and utilities that retry with exponential backoff
"""

import functools
import logging
import random
import time
from typing import Any, Callable, Optional, Tuple, Type, TypeVar

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
    """Error raised when the retry count is exceeded."""

    def __init__(
        self, message: str, attempts: int, last_exception: Optional[Exception] = None
    ):
        super().__init__(message)
        self.attempts = attempts
        self.last_exception = last_exception


def is_transient_error(exception: Exception) -> bool:
    """
    Decide whether an error is transient (retryable)

    Args:
        exception: The exception to check

    Returns:
        True if the error is a retryable transient error
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
    Compute the delay for exponential backoff

    Args:
        attempt: Current attempt number (starting at 0)
        base_delay: Base delay (seconds)
        max_delay: Maximum delay (seconds)
        jitter: Whether to add jitter (prevents thundering herd)

    Returns:
        Computed delay (seconds)
    """
    # Exponential backoff: base_delay * 2^attempt
    delay = base_delay * (2**attempt)

    # Cap at max_delay
    delay = min(delay, max_delay)

    # Add jitter (0-100% of delay) to prevent thundering herd
    if jitter:
        delay = delay * (0.5 + random.random())

    return float(delay)


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    retryable_exceptions: Optional[Tuple[Type[Exception], ...]] = None,
    on_retry: Optional[Callable[[Exception, int, float], None]] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator that retries with exponential backoff

    On a transient error (network error, throttling, etc.),
    retries automatically with exponential backoff.

    Args:
        max_retries: Maximum number of retries (default: 3)
        base_delay: Base delay in seconds (default: 1.0)
        max_delay: Maximum delay in seconds (default: 60.0)
        jitter: Whether to add jitter (default: True)
        retryable_exceptions: Tuple of exceptions to retry (auto-detected if None)
        on_retry: Callback invoked on each retry

    Returns:
        The decorated function

    Example:
        @with_retry(max_retries=3, base_delay=1.0)
        def call_external_service():
            # This will be retried on transient errors
            pass
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
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
