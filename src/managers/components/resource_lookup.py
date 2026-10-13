"""Reading what an authorization decision needs (ADR-011, contract C3).

``ResourceLookup`` returns :class:`AccessTarget` values (owner, shared
departments, display name, existence) and nothing else; ``AccessPolicy``
judges them. How each resource type is read is registered as a
:class:`~src.models.authz.LookupReader`, implemented in the Service layer by
the Unit that owns the table (U5〜U7). U2 registers none (Q1).

Rules (BR2.10, NFR9.18, NFR Design R-01, Q2):

- Readers are registered once, at start-up, in :func:`build_resource_lookup`;
  registering a type twice is a start-up error.
- An unregistered type, an unreachable table, keys left unprocessed or a
  missed deadline all raise ``LookupUnavailableError`` (503). Nothing is ever
  treated as allowed (fail closed, NFR1.3).
- Every ``get`` / ``get_many`` call has its own 3-second deadline from its
  start. The deadline is checked before each 100-ID read and handed to the
  reader, which checks it again before every call it makes.
- ``get_many`` accepts at most 500 IDs; more is a caller bug and raises
  ``ValueError`` (an unexpected 500).
- IDs that do not exist come back as ``exists=False`` targets.

Logs: ``authz.lookup`` (sampled by ``AUTH_TIMING_SAMPLE_RATE``) and
``authz.lookup_failed``; neither carries resource IDs (NFR8.8, NFR8.9).
"""

import random
import time
from typing import Callable, Iterable, Mapping, NoReturn, Sequence

from ...config import config
from ...models.authz import AccessTarget, LookupReader, ResourceType
from ...models.errors import CodedError, LookupUnavailableError
from ..shared.structured_log import log_event

#: Per-call deadline, from the start of ``get`` / ``get_many``.
LOOKUP_DEADLINE_SECONDS = 3.0
#: IDs per reader call (the ``BatchGetItem`` limit).
LOOKUP_BATCH_SIZE = 100
#: Most IDs one ``get_many`` call accepts (Q2).
LOOKUP_MAX_IDS = 500


class ResourceLookup:
    """Registered, read-only access to decision columns."""

    def __init__(
        self,
        readers: Mapping[ResourceType, LookupReader],
        *,
        clock: Callable[[], float] = time.monotonic,
        sample_rate: float | None = None,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        """Create the lookup. Prefer :func:`build_resource_lookup`.

        Args:
            readers: The reader of each registered resource type.
            clock: Monotonic clock for the deadline and the timing log.
            sample_rate: Share of calls logged as ``authz.lookup``; defaults
                to ``AUTH_TIMING_SAMPLE_RATE``.
            random_source: Source of the sampling draw (tests).
        """
        self._readers = dict(readers)
        self._clock = clock
        self._sample_rate = (
            config.AUTH_TIMING_SAMPLE_RATE if sample_rate is None else sample_rate
        )
        self._random = random_source

    def get(self, resource_type: ResourceType, resource_id: str) -> AccessTarget:
        """Read one target (``exists=False`` when it does not exist).

        Raises:
            LookupUnavailableError: The decision columns could not be read.
        """
        return self.get_many(resource_type, [resource_id])[resource_id]

    def get_many(
        self, resource_type: ResourceType, resource_ids: Sequence[str]
    ) -> dict[str, AccessTarget]:
        """Read up to 500 targets, 100 per reader call.

        Returns:
            One AccessTarget for every requested ID.

        Raises:
            ValueError: More than 500 IDs (a caller bug).
            LookupUnavailableError: The decision columns could not be read.
        """
        ids = list(dict.fromkeys(resource_ids))
        if len(ids) > LOOKUP_MAX_IDS:
            raise ValueError(
                f"get_many accepts at most {LOOKUP_MAX_IDS} ids, got {len(ids)}"
            )
        if not ids:
            return {}
        reader = self._readers.get(resource_type)
        if reader is None:
            self._fail(resource_type, "unregistered")
        started = self._clock()
        deadline = started + LOOKUP_DEADLINE_SECONDS
        result: dict[str, AccessTarget] = {}
        for start in range(0, len(ids), LOOKUP_BATCH_SIZE):
            chunk = ids[start : start + LOOKUP_BATCH_SIZE]
            if self._clock() >= deadline:
                self._fail(resource_type, "deadline")
            found = self._read(reader, resource_type, chunk, deadline)
            for resource_id in chunk:
                result[resource_id] = found.get(resource_id) or AccessTarget.missing(
                    resource_type, resource_id
                )
        self._log_timing(resource_type, len(ids), started)
        return result

    def _read(
        self,
        reader: LookupReader,
        resource_type: ResourceType,
        chunk: list[str],
        deadline: float,
    ) -> dict[str, AccessTarget]:
        try:
            return reader.read(chunk, deadline)
        except CodedError as e:
            reason = getattr(e, "reason", None)
            self._fail(
                resource_type,
                reason if isinstance(reason, str) else "unreachable",
                cause=e,
            )

    def _fail(
        self,
        resource_type: ResourceType,
        reason: str,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        log_event(
            "authz.lookup_failed", resource_type=resource_type.value, reason=reason
        )
        raise LookupUnavailableError(
            f"authorization lookup failed: {reason}"
        ) from cause

    def _log_timing(
        self, resource_type: ResourceType, count: int, started: float
    ) -> None:
        if self._sample_rate > 0 and self._random() < self._sample_rate:
            log_event(
                "authz.lookup",
                resource_type=resource_type.value,
                count=count,
                duration_ms=round((self._clock() - started) * 1000, 2),
            )


def build_resource_lookup(
    readers: Iterable[tuple[ResourceType, LookupReader]] = (),
    *,
    clock: Callable[[], float] = time.monotonic,
    sample_rate: float | None = None,
    random_source: Callable[[], float] = random.random,
) -> ResourceLookup:
    """The one place readers are registered (BR2.10).

    Units that add owner and share columns (U5〜U7) add their reader here in
    the same change. U2 registers none, so every lookup answers 503 until then.

    Args:
        readers: ``(resource type, reader)`` pairs.
        clock: See :class:`ResourceLookup`.
        sample_rate: See :class:`ResourceLookup`.
        random_source: See :class:`ResourceLookup`.

    Raises:
        ValueError: A resource type is registered twice (start-up error).
    """
    registered: dict[ResourceType, LookupReader] = {}
    for resource_type, reader in readers:
        if resource_type in registered:
            raise ValueError(f"reader for {resource_type.value} registered twice")
        registered[resource_type] = reader
    return ResourceLookup(
        registered,
        clock=clock,
        sample_rate=sample_rate,
        random_source=random_source,
    )
