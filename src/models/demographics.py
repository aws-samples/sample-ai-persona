"""
Allowed gender values and their display-name mapping.

gender is a custom enum with a fixed set of choices (three values), shared by
validation, template display and generation prompts.

Country is ISO 3166 based and worldwide, so it is handled by
src/services/country_service.py, which wraps the pycountry library (Models
stay standard-library only).
"""

from __future__ import annotations

# Mapping from allowed gender values (as stored) to Japanese display names
GENDER_LABELS: dict[str, str] = {
    "male": "男性",
    "female": "女性",
    "other": "その他",
}

# Set of allowed gender values
VALID_GENDERS: frozenset[str] = frozenset(GENDER_LABELS.keys())


def gender_label(value: str | None) -> str:
    """Convert a gender code to its Japanese display name. Unknown values are returned as is."""
    if not value:
        return ""
    return GENDER_LABELS.get(value, value)


def sanitize_gender(value: str | None) -> str | None:
    """Validate an AI-generated gender value and return None if it is not allowed.

    The generation prompt asks for fixed values, but the LLM may return
    something off-spec (e.g. "M"), so the value is normalized before saving.
    This keeps one invalid value from failing a whole batch generation.
    """
    if value in VALID_GENDERS:
        return value
    return None
