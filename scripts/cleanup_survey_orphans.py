#!/usr/bin/env python3
"""
Find the S3 objects a survey or custom persona data delete left behind (U7, NFR1.84, NFR1.85).

Deleting a survey removes its body and shares first, then its result CSV,
retrying three times. Deleting custom persona data removes its record (and
the name reservation) first, then its Parquet file, retrying three times.
Whatever is still left after that is found here:

- objects under ``survey-results/{id}/`` whose survey no longer exists,
- ``persona-dataset/custom/{id}.parquet`` whose record no longer exists.

Only keys named by an ID (a UUID) are looked at; files named by their data
set's name (before v1.0.0) and temporary uploads are left as they are.

By default it only prints the counts. ``--apply`` deletes them.

A survey writes its CSV before the body records it, and an import writes the
Parquet before its record. Objects written within the last hour are left
alone, and ``--apply`` checks again that the body (the record) is absent just
before it deletes.

Run it with an admin's own credentials, not the app's task role (the task role
has no ``ListBucket`` on these prefixes and no ``Scan``)::

    uv run python scripts/cleanup_survey_orphans.py [--apply]

The bucket and table names default to the app's settings (``S3_BUCKET_NAME``,
``SURVEYS_TABLE_NAME`` or ``DYNAMODB_TABLE_PREFIX``,
``CUSTOM_PERSONA_DATA_TABLE_NAME``; see ``src/config.py``).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import config  # noqa: E402

RESULTS_PREFIX = "survey-results/"
CUSTOM_PREFIX = "persona-dataset/custom/"
#: Anything written within this many seconds may belong to a save in progress.
GRACE_SECONDS = 3600

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RESULT_KEY = re.compile(rf"^{RESULTS_PREFIX}({_UUID})/.+$")
_CUSTOM_KEY = re.compile(rf"^{CUSTOM_PREFIX}({_UUID})\.parquet$")


class Surveys(Protocol):
    """What the script needs from ``SurveyStore``."""

    def get_many(self, survey_ids: Sequence[str]) -> Mapping[str, Any]: ...


class CustomData(Protocol):
    """What the script needs from ``CustomPersonaDataStore``."""

    def get_many(self, data_ids: Sequence[str]) -> Mapping[str, Any]: ...


class Objects(Protocol):
    """What the script needs from ``S3Service``."""

    bucket_name: str
    s3_client: Any


@dataclass(frozen=True)
class StoredObject:
    """One S3 object: its key and when it was written (epoch seconds)."""

    key: str
    written_at: float


@dataclass
class CleanupPlan:
    """What is left without its survey or record.

    Attributes:
        results: Survey ID -> number of its objects under ``survey-results/{id}/``.
        result_keys: Survey ID -> those keys.
        custom_files: Keys of Parquet files without a record.
        recent: Prefixes / keys left alone because they were written within
            the last hour (a save may be in progress).
        skipped: Keys under the two prefixes not named by an ID (not touched).
    """

    results: dict[str, int] = field(default_factory=dict)
    result_keys: dict[str, list[str]] = field(default_factory=dict)
    custom_files: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def is_clean(self) -> bool:
        """Whether nothing is left behind (the recent ones are not counted)."""
        return not (self.results or self.custom_files)


def _list(objects: Objects, prefix: str) -> Iterator[StoredObject]:
    paginator = objects.s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=objects.bucket_name, Prefix=prefix):
        for item in page.get("Contents", []):
            yield StoredObject(item["Key"], item["LastModified"].timestamp())


def _existing(reader: Any, method: str, ids: Sequence[str]) -> set[str]:
    if not ids:
        return set()
    return set(getattr(reader, method)(list(ids)))


def plan_cleanup(
    surveys: Surveys,
    custom_data: CustomData,
    objects: Objects,
    *,
    now: float,
    grace_seconds: int = GRACE_SECONDS,
) -> CleanupPlan:
    """Find the orphans written before ``now - grace_seconds`` (nothing is deleted)."""
    limit = now - grace_seconds
    plan = CleanupPlan()

    by_survey: dict[str, list[StoredObject]] = {}
    for stored in _list(objects, RESULTS_PREFIX):
        match = _RESULT_KEY.match(stored.key)
        if match is None:
            plan.skipped += 1
            continue
        by_survey.setdefault(match.group(1), []).append(stored)
    alive = _existing(surveys, "get_many", sorted(by_survey))
    for survey_id, results in sorted(by_survey.items()):
        if survey_id in alive:
            continue
        if max(s.written_at for s in results) > limit:
            plan.recent.append(f"{RESULTS_PREFIX}{survey_id}/")
            continue
        plan.results[survey_id] = len(results)
        plan.result_keys[survey_id] = sorted(s.key for s in results)

    files: dict[str, StoredObject] = {}
    for stored in _list(objects, CUSTOM_PREFIX):
        match = _CUSTOM_KEY.match(stored.key)
        if match is None:
            plan.skipped += 1
            continue
        files[match.group(1)] = stored
    registered = _existing(custom_data, "get_many", sorted(files))
    for data_id, stored in sorted(files.items()):
        if data_id in registered:
            continue
        if stored.written_at > limit:
            plan.recent.append(stored.key)
            continue
        plan.custom_files.append(stored.key)

    plan.recent.sort()
    return plan


def _delete_keys(objects: Objects, keys: Sequence[str]) -> None:
    for start in range(0, len(keys), 1000):
        chunk = keys[start : start + 1000]
        objects.s3_client.delete_objects(
            Bucket=objects.bucket_name,
            Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
        )


def apply_plan(
    surveys: Surveys, custom_data: CustomData, objects: Objects, plan: CleanupPlan
) -> list[str]:
    """Delete the orphans (``--apply``).

    Each survey and record is checked again just before its objects are
    deleted; one that now exists (its save finished) is kept.

    Returns:
        The survey and custom data IDs kept because they appeared.
    """
    kept: list[str] = []
    for survey_id, keys in plan.result_keys.items():
        if _existing(surveys, "get_many", [survey_id]):
            kept.append(survey_id)
            continue
        _delete_keys(objects, keys)
    for key in plan.custom_files:
        match = _CUSTOM_KEY.match(key)
        data_id = match.group(1) if match else ""
        if _existing(custom_data, "get_many", [data_id]):
            kept.append(data_id)
            continue
        _delete_keys(objects, [key])
    return sorted(kept)


def _report(plan: CleanupPlan) -> list[str]:
    return [
        f"本体のない結果: {len(plan.results)} 件（{sum(plan.results.values())} オブジェクト）",
        *[f"  {RESULTS_PREFIX}{sid}/\t{n}" for sid, n in plan.results.items()],
        f"登録情報のないカスタムデータ: {len(plan.custom_files)} 件",
        *[f"  {key}" for key in plan.custom_files],
        # 保存の途中かもしれないため、今回は対象にしない
        f"書かれてから 1 時間たっていないもの: {len(plan.recent)} 件（今回は対象外）",
        *[f"  {key}" for key in plan.recent],
        f"ID の名前でないもの: {plan.skipped} 件（対象外）",
    ]


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find survey results and custom persona data files left behind."
    )
    parser.add_argument("--apply", action="store_true", help="delete them")
    parser.add_argument("--region", default=config.DYNAMODB_REGION)
    return parser.parse_args(argv)


def _default_services(region: str) -> tuple[Surveys, CustomData, Objects]:
    from src.services.custom_persona_data_store import CustomPersonaDataStore
    from src.services.s3_service import S3Service
    from src.services.survey_store import SurveyStore

    if not config.S3_BUCKET_NAME:
        raise SystemExit("S3_BUCKET_NAME が設定されていません")
    return (
        SurveyStore(region=region, table_name=config.table_name("Surveys")),
        CustomPersonaDataStore(
            region=region, table_name=config.CUSTOM_PERSONA_DATA_TABLE_NAME
        ),
        S3Service(bucket_name=config.S3_BUCKET_NAME, region_name=config.AWS_REGION),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    surveys: Surveys | None = None,
    custom_data: CustomData | None = None,
    objects: Objects | None = None,
    now: float | None = None,
) -> int:
    """Print the orphans, and delete them with ``--apply``.

    Args:
        now: Epoch seconds the one-hour grace is measured from (tests).
    """
    args = _parse(argv)
    if surveys is None or custom_data is None or objects is None:
        default_surveys, default_custom, default_objects = _default_services(
            args.region
        )
        surveys = surveys or default_surveys
        custom_data = custom_data or default_custom
        objects = objects or default_objects
    plan = plan_cleanup(
        surveys, custom_data, objects, now=time.time() if now is None else now
    )
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
    kept = apply_plan(surveys, custom_data, objects, plan)
    for item_id in kept:
        print(f"本体ができたため消さなかったもの: {item_id}")
    print("消しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
