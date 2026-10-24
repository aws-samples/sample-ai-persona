"""Reading the participants of a discussion or an interview, with the decision (U6).

A Component (``.claude/rules/architecture.md``): ``PersonaManager`` (the start
of a discussion or an interview, ``get_personas_for_use``) and the discussion,
agent-discussion and interview Managers (the re-checks while running, the
participants of a saved discussion, an interview's rebuild) share it, because
Managers never import each other (logical-components.md "Manager をまたぐ読み取り").

- :meth:`read_for_use`: every participant through ``ResourceLookup.get_many``
  (one read) and ``ensure_readable``; only then one batch read of the bodies
  (BR2.8, NFR1.46). One unreadable or deleted participant stops it with
  ``PERSONA_NOT_FOUND`` without telling which or why (NFR1.56).
- :meth:`ensure_all_readable` / :meth:`can_read_all`: the re-check before each
  agent statement, each interview send and each save (BR7.1, BR7.2), one
  ``get_many`` per call (NFR2.17); nothing is carried over between calls.
- :meth:`statuses`: ``readable`` / ``no_access`` / ``deleted`` of each
  participant for the reader of a saved discussion (BR7.3, M9).

A decision read that cannot complete raises ``LookupUnavailableError`` (503,
fail closed, NFR1.63); a body read that fails does the same.
"""

import logging
from typing import Protocol, Sequence

from ...models.authz import AccessTarget, Principal, ResourceType
from ...models.errors import (
    CodedError,
    ErrorCode,
    LookupUnavailableError,
    ParticipantNotFoundError,
)
from ...models.persona import Persona
from .access_policy import AccessPolicy
from .resource_lookup import ResourceLookup

logger = logging.getLogger(__name__)

_PERSONA = ResourceType.PERSONA


class PersonaBodies(Protocol):
    """What this component needs from ``PersonaStore``."""

    def get_many(self, persona_ids: Sequence[str]) -> dict[str, Persona]: ...


class ParticipantReader:
    """Decision-first reads of participant personas."""

    def __init__(self, lookup: ResourceLookup, bodies: PersonaBodies) -> None:
        """Create the component.

        Args:
            lookup: The per-task ``ResourceLookup`` (persona reader registered).
            bodies: Batch reads of persona bodies (``PersonaStore``).
        """
        self._lookup = lookup
        self._bodies = bodies

    def targets(self, persona_ids: Sequence[str]) -> dict[str, AccessTarget]:
        """The decision columns of the participants (one ``get_many``).

        Raises:
            LookupUnavailableError: The columns could not be read.
        """
        return self._lookup.get_many(_PERSONA, list(persona_ids))

    def ensure_all_readable(
        self, principal: Principal, persona_ids: Sequence[str]
    ) -> dict[str, AccessTarget]:
        """Every participant must be readable by ``principal`` (BR2.8, BR7.1).

        Returns:
            The targets (owner and shares, for ``ExecutionContext``).

        Raises:
            ParticipantNotFoundError: ``PERSONA_NOT_FOUND`` (unreadable or
                deleted alike).
            LookupUnavailableError: The decision read failed.
        """
        targets = self.targets(persona_ids)
        for persona_id in dict.fromkeys(persona_ids):
            try:
                AccessPolicy.ensure_readable(
                    principal, targets[persona_id], ErrorCode.PERSONA_NOT_FOUND
                )
            except CodedError as e:
                if e.code is not ErrorCode.PERSONA_NOT_FOUND:
                    raise
                raise ParticipantNotFoundError("a participant is unreadable") from e
        return targets

    def can_read_all(self, principal: Principal, persona_ids: Sequence[str]) -> bool:
        """Whether every participant is still readable (the re-check, BR7.1)."""
        targets = self.targets(persona_ids)
        return all(
            AccessPolicy.can_read(principal, targets[pid])
            for pid in dict.fromkeys(persona_ids)
        )

    def read_for_use(
        self, principal: Principal, persona_ids: Sequence[str]
    ) -> list[Persona]:
        """The participants' bodies, read only after all of them passed (BR2.8).

        Returns:
            The personas in the given order (duplicates dropped).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when any participant is unreadable,
                deleted, or vanished before its body was read.
            LookupUnavailableError: The decision or the body read failed.
        """
        ids = list(dict.fromkeys(persona_ids))
        self.ensure_all_readable(principal, ids)
        if not ids:
            return []
        try:
            found = self._bodies.get_many(ids)
        except Exception as e:
            logger.warning("Reading participant bodies failed: %s", type(e).__name__)
            raise LookupUnavailableError("participant bodies unreadable") from e
        missing = [i for i in ids if i not in found]
        if missing:
            raise ParticipantNotFoundError("a participant vanished after the check")
        return [found[i] for i in ids]

    def statuses(
        self, principal: Principal, persona_ids: Sequence[str]
    ) -> dict[str, str]:
        """``readable`` / ``no_access`` / ``deleted`` per participant (BR7.3).

        Nothing but the decision columns is read (NFR2.16).
        """
        if not persona_ids:
            return {}
        targets = self.targets(persona_ids)
        return {pid: _status(principal, targets[pid]) for pid in persona_ids}

    def statuses_with_bodies(
        self, principal: Principal, persona_ids: Sequence[str]
    ) -> tuple[dict[str, str], dict[str, Persona]]:
        """The states, plus the bodies of the participants the reader can read.

        Unreadable and deleted participants' bodies are never read (BR7.3).
        The bodies are display-only: when their batch read fails the states
        are still returned with no bodies.

        Raises:
            LookupUnavailableError: The decision columns could not be read.
        """
        statuses = self.statuses(principal, persona_ids)
        readable = [pid for pid, s in statuses.items() if s == "readable"]
        if not readable:
            return statuses, {}
        try:
            bodies = self._bodies.get_many(readable)
        except Exception as e:
            # 表示用だけなので、読めなくても参加者の状態は出す
            logger.warning("Reading participant bodies failed: %s", type(e).__name__)
            return statuses, {}
        return statuses, {pid: bodies[pid] for pid in readable if pid in bodies}


def _status(principal: Principal, target: AccessTarget) -> str:
    if not target.exists:
        return "deleted"
    if AccessPolicy.can_read(principal, target):
        return "readable"
    return "no_access"
