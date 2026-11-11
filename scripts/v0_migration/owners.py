"""The mapping file and owner resolution (user-authz-design.md §3.6.1).

v0 recorded no creator, so every migrated resource gets the default owner
unless the mapping file names another one. The mapping file is a CSV::

    resource_type,resource_id,owner_email,group_ids,action
    persona,<id>,user@example.com,sales;planning,
    discussion,<id>,,,skip
    dataset,<id>,,sales,

- ``resource_type``: ``persona``, ``discussion``, ``survey_template``,
  ``survey`` (owned, shareable), ``dataset``, ``knowledge_base``,
  ``custom_persona_data`` (organization assets: usage scope only, no owner).
  A custom persona data row is keyed by its v0 name.
- ``group_ids``: departments separated by ``;``, with or without ``group:``.
- ``action``: ``migrate`` (default) or ``skip``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .users import normalize_email

#: Types that have an owner and can be shared with departments.
OWNED_TYPES = frozenset({"persona", "discussion", "survey_template", "survey"})
#: Organization assets: a usage scope but no owner.
ASSET_TYPES = frozenset({"dataset", "knowledge_base", "custom_persona_data"})
ACTIONS = ("migrate", "skip")
_GROUP_PREFIX = "group:"


class MappingError(ValueError):
    """The mapping file or an owner cannot be used."""


@dataclass(frozen=True)
class MappingRow:
    """One row of the mapping file."""

    resource_type: str
    resource_id: str
    owner_email: str
    group_ids: frozenset[str]
    action: str


class OwnerMapping:
    """Rows of the mapping file, looked up by type and ID."""

    def __init__(self, rows: Mapping[tuple[str, str], MappingRow]) -> None:
        self._rows = dict(rows)

    def get(self, resource_type: str, resource_id: str) -> MappingRow | None:
        """The row of a resource, or ``None``."""
        return self._rows.get((resource_type, resource_id))

    def skipped(self, resource_type: str, resource_id: str) -> bool:
        """Whether the row says ``skip``."""
        row = self.get(resource_type, resource_id)
        return row is not None and row.action == "skip"

    def groups(self, resource_type: str, resource_id: str) -> frozenset[str]:
        """Departments of a resource (empty when not mapped)."""
        row = self.get(resource_type, resource_id)
        return row.group_ids if row else frozenset()

    def group_ids(self) -> frozenset[str]:
        """Every department named in the file."""
        return frozenset(g for row in self._rows.values() for g in row.group_ids)

    def owner_emails(self) -> frozenset[str]:
        """Every owner email named in the file."""
        return frozenset(r.owner_email for r in self._rows.values() if r.owner_email)


def _groups(value: str) -> frozenset[str]:
    groups = set()
    for part in value.split(";"):
        name = part.strip()
        if name.startswith(_GROUP_PREFIX):
            name = name[len(_GROUP_PREFIX) :]
        if name:
            groups.add(name)
    return frozenset(groups)


def load_mapping(path: Path | str | None) -> OwnerMapping:
    """Read the mapping file (an empty mapping when ``path`` is ``None``).

    Raises:
        MappingError: A missing column, an unknown type or action, an owner
            on an organization asset, or the same resource twice.
    """
    if path is None:
        return OwnerMapping({})
    rows: dict[tuple[str, str], MappingRow] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        columns = set(reader.fieldnames or ())
        if not {"resource_type", "resource_id"} <= columns:
            raise MappingError(
                "the mapping needs resource_type and resource_id columns"
            )
        for line, raw in enumerate(reader, start=2):
            row = _row(raw, line)
            key = (row.resource_type, row.resource_id)
            if key in rows:
                raise MappingError(f"line {line}: duplicate row for {key[0]} {key[1]}")
            rows[key] = row
    return OwnerMapping(rows)


def _row(raw: Mapping[str, str | None], line: int) -> MappingRow:
    def cell(name: str) -> str:
        return (raw.get(name) or "").strip()

    resource_type = cell("resource_type")
    if resource_type not in OWNED_TYPES | ASSET_TYPES:
        raise MappingError(f"line {line}: unknown resource_type {resource_type!r}")
    resource_id = cell("resource_id")
    if not resource_id:
        raise MappingError(f"line {line}: resource_id is empty")
    action = cell("action") or "migrate"
    if action not in ACTIONS:
        raise MappingError(f"line {line}: unknown action {action!r} (migrate or skip)")
    owner = normalize_email(cell("owner_email"))
    if owner and resource_type in ASSET_TYPES:
        raise MappingError(
            f"line {line}: {resource_type} has no owner (leave owner_email empty)"
        )
    return MappingRow(
        resource_type, resource_id, owner, _groups(cell("group_ids")), action
    )


class OwnerResolver:
    """``owner_sub`` of each migrated resource.

    Args:
        subs: Normalized email -> ``sub`` of the v1 pool.
        default_email: The default owner (must be in the v1 pool).
        mapping: The mapping file.
        planned_emails: Users the run would create (dry-run only); they
            resolve to a placeholder because they have no ``sub`` yet.

    Raises:
        MappingError: The default owner or a mapped owner is not a v1 user.
    """

    def __init__(
        self,
        subs: Mapping[str, str],
        *,
        default_email: str,
        mapping: OwnerMapping,
        planned_emails: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self._subs = dict(subs)
        self._mapping = mapping
        self._planned = set(planned_emails)
        default = normalize_email(default_email)
        if default not in self._subs:
            raise MappingError(f"default owner {default} is not a user of the v1 pool")
        self.default_sub = self._subs[default]
        missing = sorted(
            e
            for e in mapping.owner_emails()
            if e not in self._subs and e not in self._planned
        )
        if missing:
            raise MappingError(f"owners not in the v1 pool: {', '.join(missing)}")

    def owner(self, resource_type: str, resource_id: str) -> str:
        """The owner of a resource: the mapped one, else the default."""
        row = self._mapping.get(resource_type, resource_id)
        if row is None or not row.owner_email:
            return self.default_sub
        if row.owner_email in self._subs:
            return self._subs[row.owner_email]
        return f"(new:{row.owner_email})"
