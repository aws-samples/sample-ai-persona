"""
ペルソナアイコン画像URLの表示ヘルパー

テンプレートのグローバル関数として登録する純粋な表示ヘルパー（ビジネスロジックなし）。
"""

from pathlib import PurePosixPath
from typing import Any


def persona_avatar_url(persona: Any) -> str:
    """Return the app URL of the persona's uploaded avatar, or "" if none is set.

    The object key's file stem (a UUID regenerated on every upload) is appended
    as a version query, so browsers never show a cached image after a change.
    """
    avatar_path = getattr(persona, "avatar_path", None)
    persona_id = getattr(persona, "id", None)
    if not avatar_path or not persona_id:
        return ""
    version = PurePosixPath(avatar_path).stem
    return f"/persona/{persona_id}/avatar?v={version}"
