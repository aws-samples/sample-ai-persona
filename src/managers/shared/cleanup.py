"""Retrying a cleanup step after the main write succeeded (U7 NFR1.84, NFR1.85).

A pure helper with no decision in it: the caller decides what to clean up and
that a failure must not fail its operation. The step is tried three times
(0.2 / 0.4 seconds apart); what is still left is logged once as
``survey.cleanup_failed`` with IDs and the exception type only (NFR3.13).
"""

from typing import Callable

from .structured_log import log_event

#: Attempts of each cleanup step before it is logged.
CLEANUP_ATTEMPTS = 3
#: Waits between cleanup attempts.
CLEANUP_BACKOFF_SECONDS: tuple[float, ...] = (0.2, 0.4, 0.8)


def run_cleanup(
    action: Callable[[], object],
    *,
    resource_type: str,
    resource_id: str,
    step: str,
    sleep: Callable[[float], None],
) -> bool:
    """Run ``action`` up to three times; log ``survey.cleanup_failed`` when it never works.

    Returns:
        Whether the step finally succeeded.
    """
    error: Exception | None = None
    for attempt in range(CLEANUP_ATTEMPTS):
        try:
            action()
            return True
        except Exception as e:
            error = e
            if attempt < CLEANUP_ATTEMPTS - 1:
                sleep(CLEANUP_BACKOFF_SECONDS[attempt])
    log_event(
        "survey.cleanup_failed",
        level="ERROR",
        resource_type=resource_type,
        resource_id=resource_id,
        step=step,
        error_type=type(error).__name__,
    )
    return False
