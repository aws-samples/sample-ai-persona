"""Who may use the advanced generation (beta), and its setting (U1 BR1, U6 BR34).

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

U6 (BR34, BR35.1): :meth:`AdvancedGenerationPolicy.availability` tells, with
one read, whether the feature is usable and whether the AI detailed analysis
is offered (allowed by the administrator and not the in-process sandbox).
The setting is saved on the version read (optimistic lock, BR34.4); the
setting is read with a strong read every time (BR34.5).

The ``AppSettingsStore`` is given to the constructor; this component never
reads ``service_factory``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from ...config import config
from ...models.authz import Department, Permission, Principal, permissions_of
from ...models.errors import CodedError, ErrorCode
from ...services.stores.app_settings_store import (
    ADVANCED_GENERATION_SETTING_KEY,
    SettingsVersionConflict,
)
from .access_policy import AccessPolicy

if TYPE_CHECKING:
    from ...services.stores.app_settings_store import AppSettingsStore

logger = logging.getLogger(__name__)

#: ``audience_mode`` values (contract C6).
AUDIENCE_ADMINS_ONLY = "admins_only"
AUDIENCE_EVERYONE = "everyone"
AUDIENCE_DEPARTMENTS = "departments"
AUDIENCE_MODES = (AUDIENCE_ADMINS_ONLY, AUDIENCE_EVERYONE, AUDIENCE_DEPARTMENTS)
_GROUP_PREFIX = "group:"
#: Departments one setting may name (BR34.2).
MAX_DEPARTMENTS = 100
#: Version of the stored item (contract C6).
SETTING_SCHEMA_VERSION = 1
#: ``SettingSectionView.notice`` values.
NOTICE_SAVED = "saved"
NOTICE_CONFLICT = "conflict"


@dataclass(frozen=True)
class Availability:
    """What one read of the setting tells (U6 BR34.6, BR35.1, BR35.4).

    Attributes:
        usable: The user may use the advanced generation (BR1.1).
        detailed: The AI detailed analysis is offered to the user.
    """

    usable: bool
    detailed: bool


@dataclass(frozen=True)
class AdvancedGenerationSetting:
    """The administrator's setting (entities.md AdvancedGenerationSetting, C6).

    ``department_groups`` are group names without the ``group:`` prefix.
    """

    audience_mode: str = AUDIENCE_ADMINS_ONLY
    department_groups: tuple[str, ...] = ()
    allow_detailed_analysis: bool = False
    version: int = 0
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_item(
        cls, item: Optional[Mapping[str, Any]]
    ) -> "AdvancedGenerationSetting":
        """The stored item, or the defaults when there is none (BR35.1)."""
        if not item:
            return cls()
        mode = str(item.get("audience_mode") or AUDIENCE_ADMINS_ONLY)
        return cls(
            audience_mode=mode if mode in AUDIENCE_MODES else AUDIENCE_ADMINS_ONLY,
            department_groups=tuple(
                str(g).removeprefix(_GROUP_PREFIX)
                for g in item.get("department_groups") or []
            ),
            allow_detailed_analysis=item.get("allow_detailed_analysis") is True,
            version=int(item.get("version") or 0),
            updated_by=str(item["updated_by"]) if item.get("updated_by") else None,
            updated_at=str(item["updated_at"]) if item.get("updated_at") else None,
        )


@dataclass(frozen=True)
class SettingSectionView:
    """What the settings screen section shows (entities.md SettingSectionView).

    Attributes:
        setting: The current values and version.
        departments: Every department that exists now (the choices).
        missing_groups: Saved departments that no longer exist (BR34.3).
        updated_by_name: Display name of who saved last (filled by the Manager).
        notice: ``saved`` or ``conflict``.
    """

    setting: AdvancedGenerationSetting
    departments: tuple[Department, ...]
    missing_groups: tuple[str, ...] = ()
    updated_by_name: Optional[str] = None
    notice: Optional[str] = None

    def update(self, **changes: Any) -> "SettingSectionView":
        return replace(self, **changes)


class AdvancedGenerationPolicy:
    """The release decision of the advanced generation and its setting."""

    def __init__(self, settings_store: "AppSettingsStore") -> None:
        """
        Args:
            settings_store: Reads and writes the administrator's setting (C6).
        """
        self._settings = settings_store

    def availability(
        self, principal: Principal, *, in_process: bool = False
    ) -> Availability:
        """Whether ``principal`` may use it and is offered the detailed analysis.

        One strong read of the setting (BR34.5). An unreadable setting is
        administrators only without the detailed analysis (NFR5.56).

        Args:
            principal: The user.
            in_process: The sandbox runs in this process (no AI-written code,
                ADR-004).
        """
        if not self._environment_allows():
            return Availability(usable=False, detailed=False)
        granted = permissions_of(principal.roles)
        if Permission.AI_EXECUTE not in granted:
            return Availability(usable=False, detailed=False)
        setting = self._read_setting()
        usable = self._audience_includes(principal, granted, setting)
        detailed = usable and setting.allow_detailed_analysis and not in_process
        return Availability(usable=usable, detailed=detailed)

    def is_available(self, principal: Principal) -> bool:
        """Whether ``principal`` may use the advanced generation now (BR1.1)."""
        return self.availability(principal).usable

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

    # --- U6: the settings screen section (BR34) ---------------------------------

    @staticmethod
    def feature_enabled() -> bool:
        """Whether this environment has the feature at all (BR1.3, BR1.4).

        ``ENABLE_ADVANCED_GENERATION`` only: the closed network Stage passes
        false. The settings section follows this so that an administrator can
        prepare the audience before the sandbox is configured.
        """
        return bool(config.ENABLE_ADVANCED_GENERATION)

    def section_view(self, departments: Sequence[Department]) -> SettingSectionView:
        """The current values, version, choices and missing departments (BR34.3).

        Raises:
            AppSettingsStoreError: The setting could not be read.
        """
        setting = AdvancedGenerationSetting.from_item(
            self._settings.get(ADVANCED_GENERATION_SETTING_KEY)
        )
        return self._view(setting, departments)

    def save_setting(
        self,
        principal: Principal,
        form: Mapping[str, Any],
        departments: Sequence[Department],
        *,
        now: datetime,
        error_type: type[CodedError] = CodedError,
    ) -> SettingSectionView:
        """Check and save the section on the version read (BR34.2〜BR34.4).

        Departments with none chosen are saved as ``admins_only``; other modes
        keep no departments. The caller checked ``settings:manage``.

        Raises:
            CodedError: As ``error_type``: ``REQUEST_INVALID`` (an unknown
                mode, a department that does not exist, more than 100, a bad
                version) or ``SETTINGS_VERSION_CONFLICT`` with the latest
                section in ``context["section"]``.
            AppSettingsStoreError: DynamoDB failed.
        """
        mode, groups, allow, version = self._checked(form, departments, error_type)
        item: dict[str, Any] = {
            "schema_version": SETTING_SCHEMA_VERSION,
            "audience_mode": mode,
            "allow_detailed_analysis": allow,
            "updated_by": principal.sub,
            "updated_at": now.isoformat(),
        }
        if groups:
            item["department_groups"] = [_GROUP_PREFIX + g for g in groups]
        try:
            new_version = self._settings.put_if_version(
                ADVANCED_GENERATION_SETTING_KEY, item, version
            )
        except SettingsVersionConflict as e:
            latest = self.section_view(departments).update(notice=NOTICE_CONFLICT)
            raise error_type(
                "the setting changed after it was read",
                code=ErrorCode.SETTINGS_VERSION_CONFLICT,
                context={"section": latest},
            ) from e
        saved = AdvancedGenerationSetting(
            audience_mode=mode,
            department_groups=groups,
            allow_detailed_analysis=allow,
            version=new_version,
            updated_by=principal.sub,
            updated_at=now.isoformat(),
        )
        return self._view(saved, departments).update(notice=NOTICE_SAVED)

    @staticmethod
    def _checked(
        form: Mapping[str, Any],
        departments: Sequence[Department],
        error_type: type[CodedError],
    ) -> tuple[str, tuple[str, ...], bool, int]:
        def invalid(field: str) -> CodedError:
            return error_type(
                "advanced generation setting refused",
                code=ErrorCode.REQUEST_INVALID,
                context={"field": field},
            )

        mode = str(form.get("audience_mode") or "")
        if mode not in AUDIENCE_MODES:
            raise invalid("audience_mode")
        try:
            version = int(form.get("version"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise invalid("version") from None
        if version < 0:
            raise invalid("version")
        groups: tuple[str, ...] = ()
        if mode == AUDIENCE_DEPARTMENTS:
            groups = tuple(
                dict.fromkeys(
                    str(g).removeprefix(_GROUP_PREFIX)
                    for g in form.get("department_groups") or []
                    if str(g).strip()
                )
            )
            if len(groups) > MAX_DEPARTMENTS:
                raise invalid("department_groups")
            existing = {d.group_id for d in departments}
            vanished = sum(1 for g in groups if g not in existing)
            if vanished:
                # BR34.3: the screen tells the cause; only the count is shown
                # (the group ids are not, NFR3.38)
                raise error_type(
                    "advanced generation setting names a missing department",
                    code=ErrorCode.REQUEST_INVALID,
                    context={
                        "field": "department_groups",
                        "reason": "vanished_departments",
                        "count": vanished,
                    },
                )
            if not groups:
                # BR34.2 (FD6-Q2=A): no department chosen is administrators only
                mode = AUDIENCE_ADMINS_ONLY
        allow = form.get("allow_detailed_analysis") in (True, "true", "on", "1")
        return mode, groups, allow, version

    @staticmethod
    def _view(
        setting: AdvancedGenerationSetting, departments: Sequence[Department]
    ) -> SettingSectionView:
        existing = {d.group_id for d in departments}
        return SettingSectionView(
            setting=setting,
            departments=tuple(departments),
            missing_groups=tuple(
                g for g in setting.department_groups if g not in existing
            ),
        )

    # --- the conditions ---------------------------------------------------------

    @staticmethod
    def _environment_allows() -> bool:
        return bool(config.ENABLE_ADVANCED_GENERATION and config.CODE_INTERPRETER_ID)

    @staticmethod
    def _audience_includes(
        principal: Principal,
        granted: frozenset[Permission],
        setting: AdvancedGenerationSetting,
    ) -> bool:
        if Permission.SETTINGS_MANAGE in granted:
            return True
        if setting.audience_mode == AUDIENCE_EVERYONE:
            return True
        if setting.audience_mode == AUDIENCE_DEPARTMENTS:
            return bool(set(setting.department_groups) & set(principal.group_ids))
        return False

    def _read_setting(self) -> AdvancedGenerationSetting:
        try:
            return AdvancedGenerationSetting.from_item(
                self._settings.get(ADVANCED_GENERATION_SETTING_KEY)
            )
        except CodedError as e:
            # Fail closed: an unreadable setting opens it to administrators
            # only, without the detailed analysis (BR1.2, BR35.1, NFR5.56).
            logger.warning(
                "advanced generation setting unreadable (%s)", type(e).__name__
            )
            return AdvancedGenerationSetting()
