"""Shared fakes for the U6 discussion tests (DiscussionRecords and the Managers).

``FakeStore`` stands in for ``DiscussionStore`` (with failure injection);
``FakeShareStore`` for ``ResourceShareStore`` (it reads the same bodies);
``PersonaReader`` / ``DiscussionReader`` are ``ResourceLookup`` readers.
``AccessPolicy``, ``ResourceSharing``, ``ResourceLookup`` and
``ParticipantReader`` are the real ones (decisions are never mocked).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence
from unittest.mock import Mock

from src.managers.components.discussion_records import DiscussionRecords
from src.managers.components.participant_reader import ParticipantReader
from src.managers.components.resource_lookup import (
    ResourceLookup,
    build_resource_lookup,
)
from src.managers.components.resource_sharing import (
    ResourceSharing,
    default_sharing_tables,
)
from src.models.authz import AccessTarget, Principal, ResourceType, Role
from src.models.discussion_report import DiscussionReport
from src.models.persona import Persona
from src.models.sharing import ResourceShareEntry
from src.models.uploaded_file import AttachResult
from src.services.discussion_store import DiscussionConditionFailed, DiscussionStore

DISCUSSION = ResourceType.DISCUSSION
PERSONA = ResourceType.PERSONA
NOW = datetime(2026, 10, 15, 9, 0)

#: member A and B in ``sales``, member C in ``plan``, an admin, no role.
OWNER = Principal(
    sub="a", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
PEER = Principal(
    sub="b", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
STRANGER = Principal(
    sub="c", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"plan"})
)
ADMIN = Principal(sub="admin", roles=frozenset({Role.ADMIN}))
NO_ROLE = Principal(sub="x", group_ids=frozenset({"sales"}))


class FakeStore:
    """In-memory stand-in for DiscussionStore with failure injection."""

    def __init__(self) -> None:
        self.bodies: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[int, Any]] = {}
        self.reports: dict[str, dict[str, DiscussionReport]] = {}
        self.fail: dict[str, list[Exception]] = {}
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        queue = self.fail.get(name)
        if queue:
            raise queue.pop(0)

    item_size = staticmethod(DiscussionStore.item_size)

    def put_new(self, body, extra_items=()):
        self._maybe_fail("put_new")
        if body["id"] in self.bodies:
            raise DiscussionConditionFailed("exists")
        self.bodies[body["id"]] = dict(body)

    def get(self, discussion_id):
        self._maybe_fail("get")
        body = self.bodies.get(discussion_id)
        return dict(body) if body else None

    def get_many(self, ids, attributes):
        self._maybe_fail("get_many")
        return {
            i: {k: v for k, v in self.bodies[i].items() if k in attributes}
            for i in ids
            if i in self.bodies
        }

    def write_messages(self, messages):
        self._maybe_fail("write_messages")
        for m in messages:
            self.messages.setdefault(m.discussion_id, {})[m.seq] = m

    def query_messages(self, discussion_id, *, up_to=None):
        self._maybe_fail("query_messages")
        rows = self.messages.get(discussion_id, {})
        return [rows[s] for s in sorted(rows) if up_to is None or s <= up_to]

    def delete_messages(self, discussion_id):
        self._maybe_fail("delete_messages")
        return len(self.messages.pop(discussion_id, {}))

    def query_reports(self, discussion_id):
        self._maybe_fail("query_reports")
        return sorted(
            self.reports.get(discussion_id, {}).values(), key=lambda r: r.created_at
        )

    def delete_reports(self, discussion_id):
        self._maybe_fail("delete_reports")
        return len(self.reports.pop(discussion_id, {}))

    def update_insights(self, discussion_id, insights, updated_at):
        self._maybe_fail("update_insights")
        if discussion_id not in self.bodies:
            raise DiscussionConditionFailed("gone")
        self.bodies[discussion_id]["insights"] = insights
        self.bodies[discussion_id]["updated_at"] = updated_at

    def advance_message_count(
        self, discussion_id, *, expected, new, updated_at, documents=None
    ):
        self._maybe_fail("advance_message_count")
        body = self.bodies.get(discussion_id)
        if body is None or body.get("message_count") != expected:
            raise DiscussionConditionFailed("moved")
        body["message_count"] = new
        body["updated_at"] = updated_at
        if documents is not None:
            body["documents"] = documents

    def put_report(self, report, *, max_reports, updated_at):
        self._maybe_fail("put_report")
        body = self.bodies.get(report.discussion_id)
        if body is None or body.get("report_count", 0) >= max_reports:
            raise DiscussionConditionFailed("limit", (1,))
        body["report_count"] = body.get("report_count", 0) + 1
        self.reports.setdefault(report.discussion_id, {})[report.report_id] = report

    def update_report(self, report, *, updated_at):
        self._maybe_fail("update_report")
        rows = self.reports.get(report.discussion_id, {})
        if report.report_id not in rows or report.discussion_id not in self.bodies:
            raise DiscussionConditionFailed("gone", (0,))
        rows[report.report_id] = report

    def delete_report(self, discussion_id, report_id, *, updated_at):
        self._maybe_fail("delete_report")
        rows = self.reports.get(discussion_id, {})
        if report_id not in rows:
            raise DiscussionConditionFailed("gone", (0,))
        del rows[report_id]
        self.bodies[discussion_id]["report_count"] -= 1


class FakeShareStore:
    """What ResourceSharing needs, reading the same bodies as FakeStore."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.calls: list[str] = []

    def _bodies(self) -> list[dict[str, Any]]:
        return list(self.store.bodies.values())

    def get_item(self, table: str, rid: str, attributes: Sequence[str]):
        body = self.store.bodies.get(rid)
        return (
            None if body is None else {k: v for k, v in body.items() if k in attributes}
        )

    def delete_with_shares(self, table, rtype, rid, *, expected, created_at, cleanup):
        self.calls.append("delete_with_shares")
        del self.store.bodies[rid]

    def write_shares(self, table, rtype, rid, *, expected, new, created_at, owner_sub):
        self.store.bodies[rid]["shared_group_ids"] = sorted(new)

    def _owned(self, owner: str) -> list[dict[str, Any]]:
        mine = [b for b in self._bodies() if b.get("owner_sub") == owner]
        return sorted(mine, key=lambda b: (b["created_at"], b["id"]), reverse=True)

    def query_owner(self, table, owner_sub, *, limit, start_key=None):
        self.calls.append("query_owner")
        rows = self._owned(owner_sub)
        if start_key:
            pos = next(i for i, b in enumerate(rows) if b["id"] == start_key["id"]["S"])
            rows = rows[pos + 1 :]
        page = rows[:limit]
        last = None
        if page and len(page) == limit:
            b = page[-1]
            last = {
                "id": {"S": b["id"]},
                "owner_sub": {"S": owner_sub},
                "created_at": {"S": b["created_at"]},
            }
        return [b["id"] for b in page], last

    def count_owner(self, table, owner_sub):
        return len(self._owned(owner_sub))

    def query_group(self, group_id, rtype):
        self.calls.append("query_group")
        entries = [
            ResourceShareEntry(
                group_id, rtype, b["id"], b["created_at"], b.get("owner_sub", "")
            )
            for b in self._bodies()
            if group_id in (b.get("shared_group_ids") or ())
        ]
        return sorted(entries, key=lambda e: e.share_key, reverse=True)

    def scan_page(self, table, *, limit, attributes, start_key=None):
        self.calls.append("scan_page")
        rows = sorted(self._bodies(), key=lambda b: b["id"])
        if start_key:
            rows = [b for b in rows if b["id"] > start_key["id"]["S"]]
        page = rows[:limit]
        last = {"id": {"S": page[-1]["id"]}} if page and len(page) == limit else None
        return [{k: v for k, v in b.items() if k in attributes} for b in page], last

    def scan_count(self, table):
        return len(self.store.bodies)


class PersonaReader:
    """Persona decision columns; tests change ``targets`` to unshare or delete."""

    def __init__(self) -> None:
        self.targets: dict[str, AccessTarget] = {
            "p1": AccessTarget(PERSONA, "p1", owner_sub="a"),
            "p2": AccessTarget(
                PERSONA, "p2", owner_sub="b", shared_group_ids=frozenset({"sales"})
            ),
            "p3": AccessTarget(PERSONA, "p3", owner_sub="c"),
        }
        self.calls = 0

    def read(self, ids, deadline):
        self.calls += 1
        return {i: self.targets[i] for i in ids if i in self.targets}


class DiscussionReader:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def read(self, ids, deadline):
        found = {}
        for i in ids:
            body = self.store.bodies.get(i)
            if body:
                found[i] = AccessTarget(
                    DISCUSSION,
                    i,
                    owner_sub=body.get("owner_sub"),
                    shared_group_ids=frozenset(body.get("shared_group_ids") or ()),
                )
        return found


class AssetReader:
    """Organization assets without a usage scope (admin only until U7)."""

    def read(self, ids, deadline):
        return {i: AccessTarget(ResourceType.DATASET, i) for i in ids}


def persona(pid: str, owner: str, name: str | None = None, **kwargs: Any) -> Persona:
    return Persona(
        id=pid,
        name=name or f"名前{pid}",
        age=30,
        occupation="会社員",
        background="背景",
        values=["価値"],
        pain_points=["課題"],
        goals=["目標"],
        created_at=NOW,
        updated_at=NOW,
        owner_sub=owner,
        **kwargs,
    )


def make_files() -> Mock:
    files = Mock()
    files.attach_all.side_effect = lambda p, ids, pt, pid: AttachResult(
        parent_type=pt, parent_id=pid, original_expires_at={i: 1 for i in ids}
    )
    files.delete_attached.return_value = []
    return files


@dataclass
class World:
    """Everything a U6 Manager test wires together."""

    store: FakeStore = field(default_factory=FakeStore)
    personas: PersonaReader = field(default_factory=PersonaReader)
    files: Mock = field(default_factory=make_files)
    bodies: Mock = field(default_factory=Mock)

    def __post_init__(self) -> None:
        self.share_store = FakeShareStore(self.store)
        departments = Mock()
        departments.list_all.return_value = []
        departments.get_many.return_value = {}
        self.sharing = ResourceSharing(
            self.share_store, departments, sleep=lambda _s: None
        )
        for table in default_sharing_tables():
            self.sharing.register(table)
        self.lookup: ResourceLookup = build_resource_lookup(
            [
                (PERSONA, self.personas),
                (DISCUSSION, DiscussionReader(self.store)),
                (ResourceType.DATASET, AssetReader()),
                (ResourceType.KNOWLEDGE_BASE, AssetReader()),
            ],
            sample_rate=0.0,
        )
        self.persona_bodies = {
            "p1": persona("p1", "a", "佐藤"),
            "p2": persona("p2", "b", "鈴木", shared_group_ids=frozenset({"sales"})),
            "p3": persona("p3", "c", "田中"),
        }
        self.bodies.get_personas_by_ids.side_effect = lambda ids: {
            i: self.persona_bodies[i] for i in ids if i in self.persona_bodies
        }
        self.participants = ParticipantReader(self.lookup, self.bodies)
        self.records = DiscussionRecords(
            self.store,
            self.sharing,
            self.files,
            self.participants,
            clock=lambda: NOW,
            sleep=lambda _s: None,
        )
        self.owners = Mock()
        self.owners.labels.side_effect = lambda subs: {s: f"利用者{s}" for s in subs}

    def unshare(self, persona_id: str) -> None:
        """Make a persona unreadable for its non-owners (shares removed)."""
        target = self.personas.targets[persona_id]
        self.personas.targets[persona_id] = AccessTarget(
            PERSONA, persona_id, owner_sub=target.owner_sub
        )

    def share(self, discussion_id: str, *groups: str) -> None:
        self.store.bodies[discussion_id]["shared_group_ids"] = list(groups)
