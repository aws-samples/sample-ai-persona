"""Which kinds of resources a run migrates (``--include`` / ``--exclude``)."""

from __future__ import annotations

from typing import Iterable

#: Every kind, in the order they are migrated (a kind only depends on earlier ones).
KINDS: tuple[str, ...] = (
    "users",
    "datasets",
    "knowledge_bases",
    "custom_persona_data",
    "personas",
    "bindings",
    "survey_templates",
    "surveys",
    "discussions",
)


class SelectionError(ValueError):
    """The kinds given on the command line are not usable."""


def select_kinds(
    include: Iterable[str] | None, exclude: Iterable[str] | None
) -> tuple[str, ...]:
    """The kinds to migrate, in :data:`KINDS` order.

    Args:
        include: Kinds to migrate; every kind when ``None``.
        exclude: Kinds to leave out of ``include``.

    Raises:
        SelectionError: An unknown kind, or nothing left to migrate.
    """
    wanted = set(include) if include is not None else set(KINDS)
    dropped = set(exclude or ())
    unknown = sorted((wanted | dropped) - set(KINDS))
    if unknown:
        raise SelectionError(
            f"unknown kind(s): {', '.join(unknown)} (choose from {', '.join(KINDS)})"
        )
    selected = tuple(kind for kind in KINDS if kind in wanted - dropped)
    if not selected:
        raise SelectionError("nothing to migrate after --include / --exclude")
    return selected


def parse_list(value: str | None) -> list[str] | None:
    """Split a comma-separated option (``None`` stays ``None``)."""
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]
