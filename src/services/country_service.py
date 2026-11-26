"""
CountryService
Service that validates ISO 3166-1 alpha-2 country codes and looks up display names.

Wraps pycountry (official ISO 3166 data) and supports all country codes worldwide.
Names are returned in English (common_name is preferred when present, giving names such as South Korea / Taiwan;
otherwise the official ISO name).

Cities are out of scope because pycountry does not cover them (they stay free-form strings).
Gender is handled by our own fixed three-value enum (src/models/demographics.py).
"""

from __future__ import annotations

from functools import lru_cache

import pycountry


def is_valid_country(code: str | None) -> bool:
    """Return whether the code is a real ISO 3166-1 alpha-2 country code.

    Returns False for made-up codes (XX), alpha-3 (JPN), non-ASCII, etc.
    """
    if not code:
        return False
    return pycountry.countries.get(alpha_2=code) is not None


def country_name(code: str | None) -> str:
    """Convert a country code to an English display name.

    Prefers common_name when present, otherwise the official ISO name.
    Examples: KR -> "South Korea", TW -> "Taiwan", JP -> "Japan".
    Unknown codes are returned as is; None/empty string returns an empty string.
    """
    if not code:
        return ""
    country = pycountry.countries.get(alpha_2=code)
    if country is None:
        return code
    return getattr(country, "common_name", None) or country.name


@lru_cache(maxsize=1)
def country_choices() -> tuple[tuple[str, str], ...]:
    """Return (alpha-2 code, display name) pairs for filter UIs and edit forms.

    Display names use the same common_name-first logic as country_name, sorted by display name.
    The country list never changes, so lru_cache avoids rebuilding it on every request.
    An immutable tuple is returned to prevent cache pollution.
    """
    choices = [
        (country.alpha_2, getattr(country, "common_name", None) or country.name)
        for country in pycountry.countries
    ]
    return tuple(sorted(choices, key=lambda item: item[1]))


def sanitize_country(code: str | None) -> str | None:
    """Normalize an AI-generated or user-entered country code; return None if it is not real.

    Trims and uppercases, then returns None if it fails is_valid_country.
    Even if the LLM returns something non-standard such as "Japan" / "日本" / "JPN", this
    keeps one bad value from failing the whole batch generation.
    """
    if not code or not code.strip():
        return None
    normalized = code.strip().upper()
    return normalized if is_valid_country(normalized) else None
