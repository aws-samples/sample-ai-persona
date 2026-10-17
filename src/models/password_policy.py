"""
PasswordPolicy value object (v1.0.0 U8, ``closed-network-groundwork/entities.md``).

The password rules of the closed-network User Pool, received through five
environment variables (BR4.3) and checked by the app before Cognito is called
(BR2.5, NFR9.41). Besides the pool's own rules, Cognito always refuses a
password longer than 256 characters or one that starts or ends with a space,
so those are checked too.

Standard library only (Models layer). Wording for each rule lives in
``web/error_messages.py`` (:func:`password_rule_label`).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

_MIN_LENGTH_SETTING = "PASSWORD_MIN_LENGTH"
#: Boolean field of :class:`PasswordPolicy` -> the environment variable it comes from.
_FLAG_FIELDS: dict[str, str] = {
    "require_uppercase": "PASSWORD_REQUIRE_UPPERCASE",
    "require_lowercase": "PASSWORD_REQUIRE_LOWERCASE",
    "require_digits": "PASSWORD_REQUIRE_DIGITS",
    "require_symbols": "PASSWORD_REQUIRE_SYMBOLS",
}
#: The five environment variables, in the order they are documented (BR4.3).
PASSWORD_POLICY_SETTINGS: tuple[str, ...] = (
    _MIN_LENGTH_SETTING,
    *_FLAG_FIELDS.values(),
)
#: Range Cognito allows for the minimum length.
MIN_LENGTH_RANGE = (6, 99)
#: Cognito refuses longer passwords whatever the pool policy says.
MAX_PASSWORD_LENGTH = 256
#: Characters Cognito counts as special characters. A space that is neither
#: leading nor trailing also counts (see :meth:`PasswordPolicy.violations`).
COGNITO_SYMBOLS: frozenset[str] = frozenset("^$*.[]{}()?-\"!@#%&/\\,><':;|_~`+=")

_TRUE = frozenset({"true", "1", "yes"})
_FALSE = frozenset({"false", "0", "no"})


class PasswordRule(StrEnum):
    """Stable keys of the password rules, in display order.

    The key is what ``PASSWORD_POLICY_NOT_MET`` carries in its context and what
    the M3 rule list is built from.
    """

    MIN_LENGTH = "min_length"
    UPPERCASE = "uppercase"
    LOWERCASE = "lowercase"
    DIGITS = "digits"
    SYMBOLS = "symbols"
    MAX_LENGTH = "max_length"
    NO_SURROUNDING_SPACE = "no_surrounding_space"


class PasswordPolicyConfigError(ValueError):
    """A password policy setting is missing or malformed (startup must stop)."""


def _is_upper(ch: str) -> bool:
    return "A" <= ch <= "Z"


def _is_lower(ch: str) -> bool:
    return "a" <= ch <= "z"


def _is_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


def _parse_flag(name: str, value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise PasswordPolicyConfigError(f"{name} must be true or false")


def _parse_min_length(name: str, value: str | None) -> int:
    low, high = MIN_LENGTH_RANGE
    try:
        length = int((value or "").strip())
    except ValueError:
        raise PasswordPolicyConfigError(f"{name} must be an integer") from None
    if not low <= length <= high:
        raise PasswordPolicyConfigError(f"{name} must be between {low} and {high}")
    return length


@dataclass(frozen=True)
class PasswordPolicy:
    """Password rules of the User Pool (value object)."""

    min_length: int
    require_uppercase: bool
    require_lowercase: bool
    require_digits: bool
    require_symbols: bool

    @classmethod
    def from_settings(cls, values: Mapping[str, str | None]) -> "PasswordPolicy":
        """Build from the five settings keyed by :data:`PASSWORD_POLICY_SETTINGS`.

        Raises:
            PasswordPolicyConfigError: A setting is missing, not a number, out
                of range, or not a boolean. The message names the setting but
                never echoes its value.
        """
        missing = [
            name for name in PASSWORD_POLICY_SETTINGS if values.get(name) is None
        ]
        if missing:
            raise PasswordPolicyConfigError(
                f"password policy settings are missing: {', '.join(missing)}"
            )
        flags = {
            field: _parse_flag(setting, values[setting])
            for field, setting in _FLAG_FIELDS.items()
        }
        return cls(
            min_length=_parse_min_length(
                _MIN_LENGTH_SETTING, values[_MIN_LENGTH_SETTING]
            ),
            **flags,
        )

    def rules(self) -> tuple[PasswordRule, ...]:
        """The rules that apply under this policy, in display order."""
        enabled = {
            PasswordRule.MIN_LENGTH: True,
            PasswordRule.UPPERCASE: self.require_uppercase,
            PasswordRule.LOWERCASE: self.require_lowercase,
            PasswordRule.DIGITS: self.require_digits,
            PasswordRule.SYMBOLS: self.require_symbols,
            PasswordRule.MAX_LENGTH: True,
            PasswordRule.NO_SURROUNDING_SPACE: True,
        }
        return tuple(rule for rule in PasswordRule if enabled[rule])

    def violations(self, password: str) -> tuple[PasswordRule, ...]:
        """The applicable rules ``password`` does not meet, in display order."""
        inner = password[1:-1]
        met = {
            PasswordRule.MIN_LENGTH: len(password) >= self.min_length,
            PasswordRule.UPPERCASE: any(_is_upper(ch) for ch in password),
            PasswordRule.LOWERCASE: any(_is_lower(ch) for ch in password),
            PasswordRule.DIGITS: any(_is_digit(ch) for ch in password),
            PasswordRule.SYMBOLS: (
                any(ch in COGNITO_SYMBOLS for ch in password) or " " in inner
            ),
            PasswordRule.MAX_LENGTH: len(password) <= MAX_PASSWORD_LENGTH,
            PasswordRule.NO_SURROUNDING_SPACE: not (
                password[:1].isspace() or password[-1:].isspace()
            ),
        }
        return tuple(rule for rule in self.rules() if not met[rule])
