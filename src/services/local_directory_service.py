"""
Departments and users for ``AUTH_MODE=local`` (development only, U4).

Without a Cognito user pool there are no ``group:`` groups and no user list.
So that the share dialog and the owner column work on a developer machine,
the departments are the groups of the local development user
(``LOCAL_DEV_USER_GROUPS``) and the only user is that user. On AWS the app
runs with ``AUTH_MODE=oidc`` and reads Cognito instead (``service_factory``).
"""

from src.config import Config
from src.models.authz import Department
from src.models.sharing import OwnerSummary


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class LocalDirectoryService:
    """The development user's departments and the development user."""

    def __init__(self, cfg: Config) -> None:
        """Create the service from the local development settings."""
        self._groups = _split(cfg.LOCAL_DEV_USER_GROUPS)
        self._name = cfg.LOCAL_DEV_USER_NAME or None
        self._email = cfg.LOCAL_DEV_USER_EMAIL

    def list_departments(self) -> list[Department]:
        """Every group of the development user, named by its ID."""
        return [Department(group_id=g, display_name=g) for g in self._groups]

    def list_users(self) -> list[OwnerSummary]:
        """The development user (its ``sub`` is ``local:<email>``)."""
        return [OwnerSummary(f"local:{self._email}", self._name, True)]
