"""The retention period of uploaded files is the same in the app and in CDK.

The app sets the expiry with ``ADVANCED_SOURCE_RETENTION_DAYS`` (7 days) in
``src/config.py``; CDK deletes the current S3 version after ``SOURCE_RETENTION_DAYS`` +
``SOURCE_LIFECYCLE_MARGIN_DAYS`` (7 + 2 = 9 days) in ``cdk/lib/constructs/upload-bucket.ts``.
If only one side changes, S3 would delete the contents of files the app still treats
as usable. TypeScript is not executed; the constant lines are read as strings.
"""

import re
from pathlib import Path

from src.config import config

ROOT = Path(__file__).parent.parent.parent
UPLOAD_BUCKET = ROOT / "cdk" / "lib" / "constructs" / "upload-bucket.ts"


def _constant(name: str) -> int:
    text = UPLOAD_BUCKET.read_text(encoding="utf-8")
    match = re.search(rf"export const {name}\s*=\s*(\d+)\s*;", text)
    assert match is not None, f"{name} is not found in upload-bucket.ts"
    return int(match.group(1))


def test_the_app_and_the_bucket_keep_files_for_the_same_days():
    assert _constant("SOURCE_RETENTION_DAYS") == config.ADVANCED_SOURCE_RETENTION_DAYS


def test_the_bucket_keeps_a_margin_of_two_days():
    assert _constant("SOURCE_LIFECYCLE_MARGIN_DAYS") == 2


def test_the_sources_rule_uses_both_constants():
    text = UPLOAD_BUCKET.read_text(encoding="utf-8")

    assert re.search(
        r"expiration:\s*Duration\.days\(\s*SOURCE_RETENTION_DAYS\s*\+\s*"
        r"SOURCE_LIFECYCLE_MARGIN_DAYS\s*\)",
        text,
    )
