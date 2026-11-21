"""上げたファイルの保存の期間がアプリと CDK で同じこと（高度な生成 U2、NFR5.25、BR13.6）。

アプリは ``src/config.py`` の ``ADVANCED_SOURCE_RETENTION_DAYS``（7 日）で期限を決め、CDK は
``cdk/lib/constructs/upload-bucket.ts`` の ``SOURCE_RETENTION_DAYS`` + ``SOURCE_LIFECYCLE_MARGIN_DAYS``
（7 + 2 = 9 日）で S3 の今の版を消す。片方だけ変えると、アプリが使えるとみなすファイルの
中身が S3 から先に消える。TypeScript は実行せず、定数の行を文字列として読む。
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
