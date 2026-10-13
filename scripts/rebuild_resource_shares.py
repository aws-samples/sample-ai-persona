#!/usr/bin/env python3
"""
Rebuild the ResourceShares index from the bodies (U4, NFR1.20, reliability-design.md §6).

The bodies' ``shared_group_ids`` are the truth. The script builds the index
items that should exist and compares them with the ones that do:

- missing items (a department in the body without an index item),
- extra items (an index item whose department is not in the body),
- ``created_at`` mismatches (the item's share key has another ``created_at``),
- ``owner_sub`` mismatches.

By default it only prints the differences. ``--apply`` fixes them. Bodies
without ``owner_sub`` (made before v1.0.0) are counted and skipped. The types
come from ``ResourceSharing``'s registration (``default_sharing_tables``), so
types added by U6 / U7 are rebuilt by the same script.

Run it with an admin's own credentials, not the app's task role (the task role
has no ``Scan`` on ResourceShares)::

    uv run python scripts/rebuild_resource_shares.py --shares-table <name> [--apply]

The table names default to the app's settings (``RESOURCE_SHARES_TABLE_NAME``,
``PERSONAS_TABLE_NAME``, ...; see ``src/config.py``).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import config  # noqa: E402
from src.managers.components.resource_sharing import default_sharing_tables  # noqa: E402
from src.models.authz import ResourceType  # noqa: E402
from src.models.sharing import ResourceShareEntry, SharingTable  # noqa: E402
from src.services.resource_share_store import ResourceShareStore  # noqa: E402

_BODY_COLUMNS = ["id", "owner_sub", "shared_group_ids", "created_at"]


class Store(Protocol):
    """What the script needs from ``ResourceShareStore``."""

    def scan_items(
        self, table: str, attributes: Sequence[str]
    ) -> list[dict[str, Any]]: ...

    def scan_shares(self) -> list[ResourceShareEntry]: ...

    def put_entries(self, entries: Iterable[ResourceShareEntry]) -> None: ...

    def delete_entries(self, entries: Iterable[ResourceShareEntry]) -> None: ...


@dataclass
class RebuildPlan:
    """The differences between the index and the bodies.

    Attributes:
        missing: Items to add.
        extra: Items to delete.
        created_at_mismatch: Items to add whose resource has an item with an
            old ``created_at`` (in ``stale``).
        owner_mismatch: Items to write again with the body's ``owner_sub``.
        stale: The old items of ``created_at_mismatch`` (to delete).
        skipped_without_owner: Bodies without ``owner_sub``.
    """

    missing: list[ResourceShareEntry] = field(default_factory=list)
    extra: list[ResourceShareEntry] = field(default_factory=list)
    created_at_mismatch: list[ResourceShareEntry] = field(default_factory=list)
    owner_mismatch: list[ResourceShareEntry] = field(default_factory=list)
    stale: list[ResourceShareEntry] = field(default_factory=list)
    skipped_without_owner: int = 0

    @property
    def is_clean(self) -> bool:
        """Whether the index already matches the bodies."""
        return not (
            self.missing
            or self.extra
            or self.created_at_mismatch
            or self.owner_mismatch
        )


def _resource_key(entry: ResourceShareEntry) -> tuple[str, ResourceType, str]:
    return entry.group_id, entry.resource_type, entry.resource_id


def _expected(
    store: Store, tables: Sequence[SharingTable]
) -> tuple[
    dict[tuple[str, ResourceType, str], ResourceShareEntry],
    set[tuple[ResourceType, str]],
    int,
]:
    """The items the bodies call for, the ownerless bodies and their count."""
    expected: dict[tuple[str, ResourceType, str], ResourceShareEntry] = {}
    ownerless: set[tuple[ResourceType, str]] = set()
    for table in tables:
        for body in store.scan_items(table.table, _BODY_COLUMNS):
            resource_id = str(body["id"])
            owner = body.get("owner_sub")
            if not owner:
                ownerless.add((table.resource_type, resource_id))
                continue
            for group_id in sorted(body.get("shared_group_ids") or ()):
                entry = ResourceShareEntry(
                    group_id,
                    table.resource_type,
                    resource_id,
                    str(body.get("created_at", "")),
                    str(owner),
                )
                expected[_resource_key(entry)] = entry
    return expected, ownerless, len(ownerless)


def plan_rebuild(store: Store, tables: Sequence[SharingTable]) -> RebuildPlan:
    """Compare the index with the bodies of ``tables`` (nothing is written)."""
    expected, ownerless, skipped = _expected(store, tables)
    types = {t.resource_type for t in tables}
    actual = [
        e
        for e in store.scan_shares()
        if e.resource_type in types
        and (e.resource_type, e.resource_id) not in ownerless
    ]
    plan = RebuildPlan(skipped_without_owner=skipped)
    found: set[tuple[str, ResourceType, str]] = set()
    for entry in sorted(actual, key=lambda e: (e.group_id, e.share_key)):
        key = _resource_key(entry)
        want = expected.get(key)
        if want is None:
            plan.extra.append(entry)
            continue
        found.add(key)
        if want.share_key != entry.share_key:
            plan.created_at_mismatch.append(want)
            plan.stale.append(entry)
        elif want.owner_sub != entry.owner_sub:
            plan.owner_mismatch.append(want)
    plan.missing = [e for k, e in sorted(expected.items()) if k not in found]
    return plan


def apply_plan(store: Store, plan: RebuildPlan) -> None:
    """Fix the index (``--apply``)."""
    store.delete_entries([*plan.extra, *plan.stale])
    store.put_entries([*plan.missing, *plan.created_at_mismatch, *plan.owner_mismatch])


def _report(plan: RebuildPlan) -> list[str]:
    def rows(title: str, entries: list[ResourceShareEntry]) -> list[str]:
        lines = [f"{title}: {len(entries)}"]
        lines += [
            f"  {e.group_id}\t{e.resource_type.value}\t{e.resource_id}\t{e.created_at}"
            for e in entries
        ]
        return lines

    return [
        *rows("足りない項目", plan.missing),
        *rows("余分な項目", plan.extra),
        *rows("created_at の食い違い", plan.created_at_mismatch),
        *rows("owner_sub の食い違い", plan.owner_mismatch),
        f"owner_sub のない本体（飛ばした）: {plan.skipped_without_owner}",
    ]


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild ResourceShares from the bodies' shared_group_ids."
    )
    parser.add_argument("--apply", action="store_true", help="fix the differences")
    parser.add_argument("--region", default=config.DYNAMODB_REGION)
    parser.add_argument(
        "--shares-table",
        default=config.RESOURCE_SHARES_TABLE_NAME,
        help="physical name of ResourceShares",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Store | None = None,
    tables: Sequence[SharingTable] | None = None,
) -> int:
    """Print the differences, and fix them with ``--apply``."""
    args = _parse(argv)
    store = store or ResourceShareStore(
        region=args.region, shares_table=args.shares_table
    )
    tables = list(tables if tables is not None else default_sharing_tables())
    plan = plan_rebuild(store, tables)
    for line in _report(plan):
        print(line)
    if plan.is_clean:
        print("ずれはありません")
        return 0
    if not args.apply:
        print(
            "差分を出しただけで、何も書いていません。直すときは --apply を付けて流してください"
        )
        return 0
    apply_plan(store, plan)
    print("直しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
