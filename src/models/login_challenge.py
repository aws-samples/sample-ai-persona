"""
LoginChallenge data model (v1.0.0 U8, ``closed-network-groundwork/entities.md``).

The state between Cognito answering ``NEW_PASSWORD_REQUIRED`` to
``AdminInitiateAuth`` and the first password change (M3) completing. It lives
in the Sessions table so any task can continue the change (FR7.4, NFR9.42);
the browser only holds the raw challenge id in the signed ``login_tx`` cookie.

Rules (``closed-network-groundwork/rules.md``):

- BR2.2: the key is ``challenge#`` + SHA-256 hex of the raw id, so it never
  collides with a LoginSession key (an unprefixed SHA-256 hex). The
  ``item_type`` marker lets either reader treat the other kind as absent.
- BR2.3: the Cognito session string is stored encrypted only.
- BR2.4: usable for 3 minutes from ``created_at``; ``ttl`` is only DynamoDB's
  cleanup horizon and never decides expiry.
- No password (current or new) is ever part of this model.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

#: How long a challenge stays usable (Cognito's NEW_PASSWORD_REQUIRED session).
CHALLENGE_TTL = timedelta(minutes=3)
#: Prefix of the Sessions table key of a challenge item (BR2.2).
CHALLENGE_KEY_PREFIX = "challenge#"
#: Value of the ``item_type`` marker attribute of a challenge item.
CHALLENGE_ITEM_TYPE = "challenge"

_REQUIRED = (
    "session_id_hash",
    "cognito_session_enc",
    "key_version",
    "username",
    "next",
    "created_at",
    "ttl",
)


def challenge_key(challenge_id: str) -> str:
    """Sessions table key of the challenge with this raw id (BR2.2)."""
    digest = hashlib.sha256(challenge_id.encode("utf-8")).hexdigest()
    return f"{CHALLENGE_KEY_PREFIX}{digest}"


@dataclass(frozen=True)
class LoginChallenge:
    """A pending first password change. Immutable."""

    session_id_hash: str
    cognito_session_enc: str
    key_version: str
    username: str
    next: str
    created_at: datetime
    ttl: int

    @property
    def item_type(self) -> str:
        """The marker that tells a challenge item from a LoginSession item."""
        return CHALLENGE_ITEM_TYPE

    @classmethod
    def create_new(
        cls,
        *,
        challenge_id: str,
        cognito_session_enc: str,
        key_version: str,
        username: str,
        next: str,
        now: datetime,
    ) -> "LoginChallenge":
        """Create a challenge whose ``ttl`` is ``now + 3 minutes``.

        Args:
            challenge_id: The raw random id handed to the browser (only its
                hash is stored).
            cognito_session_enc: The Cognito session, already encrypted.
            key_version: Version of the key that encrypted it.
            username: Cognito's internal user name (``USER_ID_FOR_SRP``).
            next: The validated in-app return path.
            now: Creation time.
        """
        return cls(
            session_id_hash=challenge_key(challenge_id),
            cognito_session_enc=cognito_session_enc,
            key_version=key_version,
            username=username,
            next=next,
            created_at=now,
            ttl=int((now + CHALLENGE_TTL).timestamp()),
        )

    def is_expired(self, now: datetime) -> bool:
        """Whether more than 3 minutes have passed since creation (BR2.4)."""
        return now - self.created_at > CHALLENGE_TTL

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage, including the ``item_type`` marker."""
        return {
            "session_id_hash": self.session_id_hash,
            "item_type": CHALLENGE_ITEM_TYPE,
            "cognito_session_enc": self.cognito_session_enc,
            "key_version": self.key_version,
            "username": self.username,
            "next": self.next,
            "created_at": self.created_at.isoformat(),
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoginChallenge | None":
        """Deserialize a challenge item.

        Returns:
            ``None`` when the item is not a challenge (another ``item_type``,
            such as a LoginSession), so callers treat it as absent (BR2.2).

        Raises:
            ValueError: The item is marked as a challenge but is incomplete.
        """
        if data.get("item_type") != CHALLENGE_ITEM_TYPE:
            return None
        missing = [name for name in _REQUIRED if data.get(name) in (None, "")]
        if missing:
            raise ValueError(f"LoginChallenge is missing: {', '.join(missing)}")
        return cls(
            session_id_hash=str(data["session_id_hash"]),
            cognito_session_enc=str(data["cognito_session_enc"]),
            key_version=str(data["key_version"]),
            username=str(data["username"]),
            next=str(data["next"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            ttl=int(data["ttl"]),
        )
