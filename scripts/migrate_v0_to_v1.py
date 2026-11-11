#!/usr/bin/env python3
"""
Copy the data of a v0 environment (v0.19.x) into a v1.x environment.

v0 keeps running and is only read; v1 is written only with ``--apply``. The
procedure (backups, departments, the mapping file, checks after the copy) is
in ``docs/migration_v0_to_v1.md``.

What is copied: users (re-created with an invitation email and
``role:member``; a v1 user with the same email is left alone), datasets,
knowledge bases, custom persona data, personas (with avatars), persona
bindings, survey templates (with images), surveys (with result CSVs) and
discussions / interviews (with messages, reports and attached documents).
Not copied: the Nemotron persona data, AgentCore Memory, jobs, sessions and
other temporary data.

Run it with an admin's own credentials (it reads v0 with Scan, which the app's
task role cannot do)::

    uv run python scripts/migrate_v0_to_v1.py --default-owner admin@example.com
    uv run python scripts/migrate_v0_to_v1.py --default-owner admin@example.com --apply

Every write is conditional: items already in v1 are counted as migrated and
left as they are, so a stopped run is simply run again.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.v0_migration.discover import (  # noqa: E402
    DiscoveryError,
    Environment,
    discover_v0,
    discover_v1,
)
from scripts.v0_migration.owners import MappingError, load_mapping  # noqa: E402
from scripts.v0_migration.runner import Clients, Migration  # noqa: E402
from scripts.v0_migration.selection import (  # noqa: E402
    KINDS,
    SelectionError,
    parse_list,
    select_kinds,
)

#: Builds a boto3 client: ``factory(service, region)``.
ClientFactory = Callable[[str, str], Any]


def _boto3_client(service: str, region: str) -> Any:
    import boto3

    return boto3.client(service, region_name=region)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="v0（v0.19.x）のデータを v1.x 系の環境へコピーします（既定は dry-run）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="移せる種別: " + ", ".join(KINDS),
    )
    parser.add_argument(
        "--default-owner",
        required=True,
        help="対応表に載らないリソースの所有者（v1 のユーザープールにいる admin のメールアドレス）",
    )
    parser.add_argument("--mapping", type=Path, help="対応表 CSV（任意）")
    parser.add_argument("--include", help="移す種別（カンマ区切り。既定はすべて）")
    parser.add_argument("--exclude", help="移さない種別（カンマ区切り）")
    parser.add_argument(
        "--apply", action="store_true", help="v1 へ書き込む（付けなければ dry-run）"
    )
    parser.add_argument(
        "--source-env", default="dev", help="v0 の deploy.sh --env の値（既定: dev）"
    )
    parser.add_argument(
        "--source-region",
        default="us-east-1",
        help="v0 のリージョン（既定: us-east-1）",
    )
    parser.add_argument(
        "--target-stage",
        default=None,
        help="v1 の CDK の Stage 名（既定: AIPersonaDev、--closed なら AIPersonaDevClosed）",
    )
    parser.add_argument(
        "--closed",
        action="store_true",
        help="v1 が閉域版のとき（利用者は招待メールを送らずに作る）",
    )
    parser.add_argument(
        "--target-region",
        default="ap-northeast-1",
        help="v1 のリージョン（既定: ap-northeast-1）",
    )
    return parser


def _describe(env: Environment, label: str) -> str:
    return f"{label}: リージョン {env.region} / バケット {env.bucket} / ユーザープール {env.user_pool_id}"


def main(
    argv: Sequence[str] | None = None, *, client_factory: ClientFactory = _boto3_client
) -> int:
    """Run the migration; returns the exit status (1 on any failure)."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    stage = args.target_stage or (
        "AIPersonaDevClosed" if args.closed else "AIPersonaDev"
    )
    try:
        kinds = select_kinds(parse_list(args.include), parse_list(args.exclude))
        mapping = load_mapping(args.mapping)
        v0 = discover_v0(
            client_factory("cloudformation", args.source_region),
            env=args.source_env,
            region=args.source_region,
        )
        v1 = discover_v1(
            client_factory("cloudformation", args.target_region),
            client_factory("ecs", args.target_region),
            stage=stage,
            region=args.target_region,
        )
    except (SelectionError, MappingError, DiscoveryError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    if v0.bucket == v1.bucket or v0.tables["personas"] == v1.tables["personas"]:
        print("エラー: v0 と v1 が同じリソースを指しています", file=sys.stderr)
        return 2
    print(_describe(v0, "v0（読み取りのみ）"))
    print(
        _describe(
            v1, "v1（書き込み先）" if args.apply else "v1（dry-run のため書き込まない）"
        )
    )
    print("移す種別: " + ", ".join(kinds))
    clients = Clients(
        v0_dynamodb=client_factory("dynamodb", v0.region),
        v0_s3=client_factory("s3", v0.region),
        v0_cognito=client_factory("cognito-idp", v0.region),
        v1_dynamodb=client_factory("dynamodb", v1.region),
        v1_s3=client_factory("s3", v1.region),
        v1_cognito=client_factory("cognito-idp", v1.region),
    )
    migration = Migration(
        v0=v0,
        v1=v1,
        clients=clients,
        kinds=kinds,
        mapping=mapping,
        default_owner=args.default_owner,
        apply=args.apply,
        invite=not args.closed,
    )
    try:
        report = migration.run()
    except MappingError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    print(report.render(apply=args.apply))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
