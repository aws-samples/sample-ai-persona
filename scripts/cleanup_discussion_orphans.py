#!/usr/bin/env python3
"""
Find what a discussion delete left behind (U6, BR1.6, NFR1.58).

Deleting a discussion removes its body and shares in one transaction, then
removes the messages, the reports and the attached files, retrying three
times (``DiscussionRecords.delete``). Whatever is still left after that is
found here:

- messages (``DiscussionMessages``) whose discussion body no longer exists,
- reports (``DiscussionReports``) whose discussion body no longer exists,
- uploads attached to a discussion (``parent_type=discussion``) that no
  longer exists.

By default it only prints the counts. ``--apply`` deletes them (the file's
object on S3 too, when S3 is configured). Nothing reachable from an existing
discussion is touched.

A save writes the attachments and the messages before the body, so a
discussion being saved looks orphaned for a moment. Discussions with anything
written within the last hour (a message's ``written_at``, a report's
``created_at``, a file's ``attached_at``) are left alone, and ``--apply``
checks again that the body is absent just before it deletes a discussion's
leftovers.

Run it with an admin's own credentials, not the app's task role (the task role
has no ``Scan`` on these tables)::

    uv run python scripts/cleanup_discussion_orphans.py [--apply]

The table names default to the app's settings (``DISCUSSIONS_TABLE_NAME``,
``DISCUSSION_MESSAGES_TABLE_NAME``, ``DISCUSSION_REPORTS_TABLE_NAME``,
``UPLOADED_FILES_TABLE_NAME``; see ``src/config.py``).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import config  # noqa: E402
from src.models.uploaded_file import UploadedFile  # noqa: E402

PARENT_TYPE = "discussion"
#: Anything written within this many seconds may belong to a save in progress.
GRACE_SECONDS = 3600


class Partition(Protocol):
    """``DiscussionStore.PartitionSummary``."""

    @property
    def count(self) -> int: ...

    @property
    def newest(self) -> float | None: ...


class Store(Protocol):
    """What the script needs from ``DiscussionStore``."""

    def message_partitions(self) -> Mapping[str, Partition]: ...

    def report_partitions(self) -> Mapping[str, Partition]: ...

    def existing(self, discussion_ids: Sequence[str]) -> set[str]: ...

    def delete_messages(self, discussion_id: str) -> int: ...

    def delete_reports(self, discussion_id: str) -> int: ...


class Files(Protocol):
    """What the script needs from ``UploadedFileStore``."""

    def scan_attached(self, parent_type: str) -> list[UploadedFile]: ...

    def delete(self, file_id: str, parent_type: str, parent_id: str) -> None: ...


class Objects(Protocol):
    """What the script needs from ``S3Service``."""

    def delete_file(self, s3_path: str) -> Any: ...


@dataclass
class CleanupPlan:
    """What is left without its discussion.

    Attributes:
        messages: Discussion ID -> number of its messages.
        reports: Discussion ID -> number of its reports.
        files: Uploads attached to a discussion that no longer exists.
        recent: Body-less discussions left alone because something of theirs
            was written within the last hour (a save may be in progress).
    """

    messages: dict[str, int] = field(default_factory=dict)
    reports: dict[str, int] = field(default_factory=dict)
    files: list[UploadedFile] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Whether nothing is left behind (the recent ones are not counted)."""
        return not (self.messages or self.reports or self.files)


def _file_time(uploaded: UploadedFile) -> float:
    if uploaded.attached_at is not None:
        return float(uploaded.attached_at)
    return uploaded.uploaded_at.timestamp()


def plan_cleanup(
    store: Store, files: Files, *, now: float, grace_seconds: int = GRACE_SECONDS
) -> CleanupPlan:
    """Find the orphans written before ``now - grace_seconds`` (nothing is written)."""
    messages = store.message_partitions()
    reports = store.report_partitions()
    attached = files.scan_attached(PARENT_TYPE)
    candidates = sorted(
        set(messages) | set(reports) | {f.parent_id for f in attached if f.parent_id}
    )
    alive = store.existing(candidates) if candidates else set()

    newest: dict[str, float] = {}
    for partitions in (messages, reports):
        for discussion_id, summary in partitions.items():
            if summary.newest is not None:
                newest[discussion_id] = max(
                    summary.newest, newest.get(discussion_id, summary.newest)
                )
    for uploaded in attached:
        if uploaded.parent_id:
            when = _file_time(uploaded)
            newest[uploaded.parent_id] = max(when, newest.get(uploaded.parent_id, when))
    limit = now - grace_seconds
    recent = {d for d in candidates if d not in alive and newest.get(d, 0.0) > limit}
    skip = alive | recent
    return CleanupPlan(
        messages={k: v.count for k, v in sorted(messages.items()) if k not in skip},
        reports={k: v.count for k, v in sorted(reports.items()) if k not in skip},
        files=sorted(
            (f for f in attached if f.parent_id not in skip), key=lambda f: f.id
        ),
        recent=sorted(recent),
    )


def apply_plan(
    store: Store, files: Files, objects: Objects | None, plan: CleanupPlan
) -> list[str]:
    """Delete the orphans (``--apply``).

    Each discussion's body is checked again just before its leftovers are
    deleted; one that now exists (its save finished) is kept.

    Returns:
        The discussion IDs kept because their body appeared.
    """
    by_discussion: dict[str, list[UploadedFile]] = {}
    for uploaded in plan.files:
        by_discussion.setdefault(uploaded.parent_id or "", []).append(uploaded)
    targets = sorted(set(plan.messages) | set(plan.reports) | set(by_discussion))
    kept: list[str] = []
    for discussion_id in targets:
        if store.existing([discussion_id]):
            kept.append(discussion_id)
            continue
        if discussion_id in plan.messages:
            store.delete_messages(discussion_id)
        if discussion_id in plan.reports:
            store.delete_reports(discussion_id)
        for uploaded in by_discussion.get(discussion_id, []):
            if objects is not None and uploaded.file_path.startswith("s3://"):
                objects.delete_file(uploaded.file_path)
            files.delete(uploaded.id, PARENT_TYPE, discussion_id)
    return kept


def _report(plan: CleanupPlan) -> list[str]:
    def counted(title: str, counts: dict[str, int]) -> list[str]:
        lines = [f"{title}: {len(counts)} 件の議論（{sum(counts.values())} 件）"]
        lines += [f"  {discussion_id}\t{n}" for discussion_id, n in counts.items()]
        return lines

    return [
        *counted("本体のない発言", plan.messages),
        *counted("本体のないレポート", plan.reports),
        f"親の議論がないファイル: {len(plan.files)} 件",
        *[f"  {f.id}\t{f.parent_id}" for f in plan.files],
        # 保存の途中かもしれないため、今回は対象にしない
        f"書かれてから 1 時間たっていない議論: {len(plan.recent)} 件（今回は対象外）",
        *[f"  {discussion_id}" for discussion_id in plan.recent],
    ]


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find messages, reports and files left without their discussion."
    )
    parser.add_argument("--apply", action="store_true", help="delete them")
    parser.add_argument("--region", default=config.DYNAMODB_REGION)
    return parser.parse_args(argv)


def _default_stores(region: str) -> tuple[Store, Files, Objects | None]:
    from src.services.discussion_store import DiscussionStore
    from src.services.service_factory import service_factory
    from src.services.uploaded_file_store import UploadedFileStore

    store = DiscussionStore(
        region=region,
        discussions_table=config.table_name("Discussions"),
        messages_table=config.DISCUSSION_MESSAGES_TABLE_NAME,
        reports_table=config.DISCUSSION_REPORTS_TABLE_NAME,
    )
    files = UploadedFileStore(
        region=region, table_name=config.table_name("UploadedFiles")
    )
    return store, files, service_factory.get_optional_s3_service()


_UNSET: Any = object()


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Store | None = None,
    files: Files | None = None,
    objects: Objects | None = _UNSET,
    now: float | None = None,
) -> int:
    """Print the orphans, and delete them with ``--apply``.

    Args:
        now: Epoch seconds the one-hour grace is measured from (tests).
    """
    args = _parse(argv)
    if store is None or files is None or objects is _UNSET:
        default_store, default_files, default_objects = _default_stores(args.region)
        store = store or default_store
        files = files or default_files
        objects = default_objects if objects is _UNSET else objects
    plan = plan_cleanup(store, files, now=time.time() if now is None else now)
    for line in _report(plan):
        print(line)
    if plan.is_clean:
        print("残っているものはありません")
        return 0
    if not args.apply:
        print(
            "件数を出しただけで、何も消していません。消すときは --apply を付けて流してください"
        )
        return 0
    kept = apply_plan(store, files, objects, plan)
    for discussion_id in kept:
        print(f"本体ができたため消さなかった議論: {discussion_id}")
    print("消しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
