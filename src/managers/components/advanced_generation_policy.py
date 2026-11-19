"""Who may use the advanced generation (beta) (U1, BR1.1〜BR1.3).

Two Managers (``SourceUploadManager``, ``AdvancedPersonaGenerationManager``)
and, in U6, ``SettingsManager`` ask the same question, and Managers cannot
import each other, so the decision lives in this component
(``.claude/rules/architecture.md``, Component layer).

The feature is usable only when every one of these holds (BR1.1):

1. ``ENABLE_ADVANCED_GENERATION`` is true (default true; the closed network
   Stage passes false, BR1.3, BR1.4).
2. An analysis sandbox is configured (``CODE_INTERPRETER_ID``).
3. The user has ``ai:execute``.
4. The administrator opened it to the user: administrators always; with the
   ``everyone`` audience every user; with ``departments`` the members of the
   chosen departments. While nothing is set, administrators only (BR1.2).
   A setting that cannot be read is treated as not set (fail closed).

"Administrator" is the holder of ``settings:manage`` (the permission of the
role that opens the feature), so the decision stays on permissions (ADR-005).

The ``AppSettingsStore`` is given to the constructor; this component never
reads ``service_factory``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from ...config import config
from ...models.authz import Permission, Principal, permissions_of
from ...models.errors import CodedError, ErrorCode
from ...services.stores.app_settings_store import ADVANCED_GENERATION_SETTING_KEY
from .access_policy import AccessPolicy

if TYPE_CHECKING:
    from ...services.stores.app_settings_store import AppSettingsStore

logger = logging.getLogger(__name__)

#: ``audience_mode`` values (contract C6).
AUDIENCE_ADMINS_ONLY = "admins_only"
AUDIENCE_EVERYONE = "everyone"
AUDIENCE_DEPARTMENTS = "departments"
_GROUP_PREFIX = "group:"


class AdvancedGenerationPolicy:
    """The release decision of the advanced generation and its upload limit."""

    def __init__(self, settings_store: "AppSettingsStore") -> None:
        """
        Args:
            settings_store: Reads the administrator's setting (contract C6).
        """
        self._settings = settings_store

    def is_available(self, principal: Principal) -> bool:
        """Whether ``principal`` may use the advanced generation now (BR1.1)."""
        if not self._environment_allows():
            return False
        granted = permissions_of(principal.roles)
        if Permission.AI_EXECUTE not in granted:
            return False
        return self._audience_includes(principal, granted)

    def ensure_available(
        self, principal: Principal, error_type: type[CodedError] = CodedError
    ) -> None:
        """Stop unless ``principal`` may use the advanced generation (BR1.1).

        Args:
            principal: The user.
            error_type: The caller's Manager exception, so that its router's
                ``except`` clause catches the refusal.

        Raises:
            AccessDeniedError: The user lacks ``ai:execute`` (FORBIDDEN).
            CodedError: ``ADVANCED_GENERATION_UNAVAILABLE`` (CONFIG) as
                ``error_type`` when any other condition fails.
        """
        AccessPolicy.require(principal, Permission.AI_EXECUTE)
        if not self.is_available(principal):
            raise error_type(
                "advanced generation is not available to this user",
                code=ErrorCode.ADVANCED_GENERATION_UNAVAILABLE,
            )

    @staticmethod
    def upload_limit_bytes() -> int:
        """Largest table this environment accepts (BR2.2; 10MB without a sandbox is U7)."""
        return int(config.ADVANCED_UPLOAD_MAX_BYTES)

    # --- the conditions ---------------------------------------------------------

    @staticmethod
    def _environment_allows() -> bool:
        return bool(config.ENABLE_ADVANCED_GENERATION and config.CODE_INTERPRETER_ID)

    def _audience_includes(
        self, principal: Principal, granted: frozenset[Permission]
    ) -> bool:
        if Permission.SETTINGS_MANAGE in granted:
            return True
        setting = self._read_setting()
        mode = str((setting or {}).get("audience_mode") or AUDIENCE_ADMINS_ONLY)
        if mode == AUDIENCE_EVERYONE:
            return True
        if mode == AUDIENCE_DEPARTMENTS:
            groups = {
                str(g).removeprefix(_GROUP_PREFIX)
                for g in (setting or {}).get("department_groups") or []
            }
            return bool(groups & set(principal.group_ids))
        return False

    def _read_setting(self) -> Optional[dict[str, Any]]:
        try:
            return self._settings.get(ADVANCED_GENERATION_SETTING_KEY)
        except CodedError as e:
            # Fail closed: an unreadable setting opens it to administrators only.
            logger.warning(
                "advanced generation setting unreadable (%s)", type(e).__name__
            )
            return None
