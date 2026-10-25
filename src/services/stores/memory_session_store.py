"""
In-process LoginSession store for ``AUTH_MODE=local`` (BR1.15).

Same port (:class:`src.models.login_session.SessionStore`) and the same
semantics as :class:`src.services.session_store_service.SessionStoreService`,
so that expiry and logout behave identically on a developer machine and in
tests. Never used on AWS: the startup check refuses ``local`` inside ECS.
"""

from dataclasses import replace
from threading import Lock

from src.models.login_challenge import LoginChallenge
from src.models.login_session import LoginSession


class MemorySessionStore:
    """Thread-safe dictionary of LoginSession keyed by the session id hash."""

    def __init__(self) -> None:
        self._items: dict[str, LoginSession] = {}
        self._challenges: dict[str, LoginChallenge] = {}
        self._lock = Lock()

    # --- LoginChallenge (U8; same semantics as SessionStoreService) -------------

    def put_challenge(self, challenge: LoginChallenge) -> None:
        """Store a challenge."""
        with self._lock:
            self._challenges[challenge.session_id_hash] = challenge

    def get_challenge(self, key: str) -> LoginChallenge | None:
        """Return the stored challenge, or ``None``."""
        with self._lock:
            return self._challenges.get(key)

    def claim_challenge(self, key: str) -> LoginChallenge | None:
        """Remove and return the challenge; ``None`` when already taken."""
        with self._lock:
            return self._challenges.pop(key, None)

    def restore_challenge(self, challenge: LoginChallenge) -> bool:
        """Write a claimed challenge back only if its key is free."""
        with self._lock:
            key = challenge.session_id_hash
            if key in self._challenges or key in self._items:
                return False
            self._challenges[key] = challenge
            return True

    def get(self, session_id_hash: str) -> LoginSession | None:
        """Return the stored session, or ``None``."""
        with self._lock:
            return self._items.get(session_id_hash)

    def put(self, session: LoginSession) -> None:
        """Store the whole session."""
        with self._lock:
            self._items[session.session_id_hash] = session

    def touch(self, session: LoginSession) -> None:
        """Copy ``last_accessed_at`` onto the stored session if it still exists."""
        with self._lock:
            stored = self._items.get(session.session_id_hash)
            if stored is not None:
                self._items[session.session_id_hash] = replace(
                    stored, last_accessed_at=session.last_accessed_at
                )

    def update_versioned(self, session: LoginSession, expected_version: int) -> bool:
        """Replace the versioned attributes if the stored version matches.

        ``last_accessed_at`` keeps the stored value, mirroring the DynamoDB
        store where the two writes touch disjoint attributes.
        """
        with self._lock:
            stored = self._items.get(session.session_id_hash)
            if stored is None or stored.version != expected_version:
                return False
            self._items[session.session_id_hash] = replace(
                session, last_accessed_at=stored.last_accessed_at
            )
            return True

    def delete(self, session_id_hash: str, expected_version: int | None = None) -> bool:
        """Delete the session, optionally only if the stored version matches."""
        with self._lock:
            stored = self._items.get(session_id_hash)
            if stored is None:
                return expected_version is None
            if expected_version is not None and stored.version != expected_version:
                return False
            del self._items[session_id_hash]
            return True
