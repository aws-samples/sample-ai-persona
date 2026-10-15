"""Rendering a persona as a Markdown file (U5, BR7.2, BR7.3).

A pure helper with no business decisions: ``PersonaManager.export_persona``
checks who may read the persona and calls these.

Values never change the document structure: Markdown control characters are
escaped (so a value cannot become a heading, a list or a code fence), and the
generation log is written line by line as quotes in its original order.
"""

import re
import unicodedata
from typing import Any, Iterable

from ...models.demographics import gender_label
from ...models.persona import Persona

#: Characters that start or shape Markdown syntax.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")
#: Characters that cannot appear in a file name on common systems.
_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')

_LOG_LABELS = {
    "thinking": "思考",
    "tool_call": "ツール呼び出し",
    "tool_result": "ツール結果",
}
_CONTEXT_LABELS = {
    "data_type": "データの種類",
    "data_description": "説明",
    "custom_prompt": "追加の指示",
    "source_files": "元ファイル",
    "persona_count": "人数",
    "generated_at": "生成日時",
    "auto_link_behavior": "行動データの自動紐付け",
}


def escape_markdown(value: Any) -> str:
    """Escape Markdown syntax in one value; line breaks become spaces."""
    text = " ".join(str(value).splitlines())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def export_filename(name: str) -> str:
    """``{name}.md`` with characters unusable in file names replaced by ``_``."""
    cleaned = _FILENAME_UNSAFE.sub(
        "_", unicodedata.normalize("NFC", name or "")
    ).strip()
    return f"{cleaned or 'persona'}.md"


def _list(items: Iterable[Any]) -> list[str]:
    return [f"- {escape_markdown(item)}" for item in items] or ["- （なし）"]


def render_persona_markdown(persona: Persona) -> str:
    """The Markdown document of a persona (BR7.2: profile and generation record).

    Memory, bindings, the owner's ``sub`` and the shares are not part of the
    :class:`Persona` fields used here.
    """
    lines: list[str] = [f"# {escape_markdown(persona.name)}", "", "## 基本情報", ""]
    profile = [
        ("名前", persona.name),
        ("年齢", persona.age),
        ("性別", gender_label(persona.gender) if persona.gender else None),
        ("職業", persona.occupation),
        ("国", persona.country),
        ("都市", persona.city),
        ("タグ", "、".join(persona.tags) if persona.tags else None),
    ]
    lines += [
        f"- {label}: {escape_markdown(v)}"
        for label, v in profile
        if v not in (None, "")
    ]
    lines += ["", "## 背景", ""]
    lines += [
        escape_markdown(line)
        for line in persona.background.splitlines()
        if line.strip()
    ]
    for title, items in (
        ("価値観", persona.values),
        ("課題", persona.pain_points),
        ("目標", persona.goals),
    ):
        lines += ["", f"## {title}", ""] + _list(items)

    if persona.generation_context:
        lines += ["", "## 生成の条件", ""]
        for key, value in persona.generation_context.items():
            if value in (None, "", []):
                continue
            label = _CONTEXT_LABELS.get(key, key)
            shown = "、".join(map(str, value)) if isinstance(value, list) else value
            lines.append(f"- {escape_markdown(label)}: {escape_markdown(shown)}")

    if persona.generation_log:
        lines += ["", "## 生成ログ", ""]
        for entry in persona.generation_log:
            kind = _LOG_LABELS.get(
                str(entry.get("type", "")), str(entry.get("type", ""))
            )
            lines.append(f"> **{escape_markdown(kind)}**")
            for raw in str(entry.get("content", "")).splitlines() or [""]:
                lines.append(f"> {escape_markdown(raw)}" if raw.strip() else ">")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
