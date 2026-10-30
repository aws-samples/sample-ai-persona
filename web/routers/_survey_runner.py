"""SurveyRunnerLoop: when the survey runner's rounds happen in this Web task.

``SurveyRunManager.advance`` does one round (functional-spec.md WF2). This
module only decides *when* it runs (Router layer, no business logic):

- every 15 seconds from the app's lifespan (:meth:`run_forever`), and once
  right after a survey is accepted or deleted (:meth:`wake`, WF1 / WF6);
- always in a worker thread, so the event loop keeps serving requests
  (NFR1.5); rounds of one process never overlap (a lock serializes them);
- on a clean stop (:meth:`stop`, NFR4.10 / R-08): mark stopping so neither the
  loop nor a wake starts a new round, wait up to 30 seconds for the running
  round, then give the lease back. A round still running after the wait
  keeps the lease (another task takes it over when it expires), so two
  tasks never do the same work.
"""

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

#: Seconds between rounds (functional-spec.md WF2).
ROUND_INTERVAL_SECONDS = 15.0
#: Longest wait for the running round on a clean stop (NFR4.10). It fits in
#: uvicorn's graceful shutdown (60 seconds) and ECS's stopTimeout (120 seconds).
STOP_WAIT_SECONDS = 30.0


class SurveyRunnerLoop:
    """Schedules the survey runner's rounds of one process."""

    def __init__(
        self,
        advance: Callable[[], None],
        release: Callable[[], bool],
        *,
        interval_seconds: float = ROUND_INTERVAL_SECONDS,
        stop_wait_seconds: float = STOP_WAIT_SECONDS,
    ) -> None:
        """Create the loop.

        Args:
            advance: One round (``SurveyRunManager.advance``; never raises).
            release: Gives the lease back (``SurveyRunManager.release_lease``).
            interval_seconds: Time between the loop's rounds.
            stop_wait_seconds: Longest wait for a running round on stop.
        """
        self._advance = advance
        self._release = release
        self.interval_seconds = interval_seconds
        self.stop_wait_seconds = stop_wait_seconds
        self._round_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = False
        self._stopping = threading.Event()
        self._stopped = asyncio.Event()

    def wake(self) -> bool:
        """Run one round soon in a thread; return at once (NFR1.4).

        Returns:
            ``False`` when stopping, or when a woken round is already waiting
            to run (it will see the new work too).
        """
        if self._stopping.is_set():
            return False
        with self._pending_lock:
            if self._pending:
                return False
            self._pending = True
        thread = threading.Thread(
            target=self._woken_round, name="survey-run-wake", daemon=True
        )
        thread.start()
        return True

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Rounds in the background for the app's lifetime, then a clean stop."""
        task = asyncio.create_task(self.run_forever())
        try:
            yield
        finally:
            await self.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run_forever(self) -> None:
        """A round every ``interval_seconds`` until :meth:`stop`."""
        while not self._stopping.is_set():
            await asyncio.to_thread(self._round)
            try:
                await asyncio.wait_for(self._stopped.wait(), self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        """Stop: no new round, wait for the running one, then release (NFR4.10)."""
        self._stopping.set()
        self._stopped.set()
        finished = await asyncio.to_thread(
            self._round_lock.acquire, True, self.stop_wait_seconds
        )
        if not finished:
            logger.warning(
                "Survey runner round still running after %ss; the lease is left to expire",
                self.stop_wait_seconds,
            )
            return
        try:
            await asyncio.to_thread(self._release)
        except Exception:
            logger.warning("Releasing the survey runner lease failed", exc_info=True)
        finally:
            self._round_lock.release()

    def _woken_round(self) -> None:
        with self._pending_lock:
            self._pending = False
        self._round()

    def _round(self) -> None:
        with self._round_lock:
            if self._stopping.is_set():
                return
            try:
                self._advance()
            except Exception:
                # advance() never raises; this keeps the loop alive regardless
                logger.error("Survey runner round raised", exc_info=True)
