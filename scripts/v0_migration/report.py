"""Counts and messages of one migration run, printed at the end."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

#: Labels of the report (the people running the script read Japanese).
_LABELS = {
    "users": "ユーザー",
    "datasets": "データセット",
    "knowledge_bases": "ナレッジベース",
    "custom_persona_data": "カスタムペルソナデータ",
    "personas": "ペルソナ",
    "bindings": "紐付け",
    "survey_templates": "アンケートテンプレート",
    "surveys": "アンケート",
    "discussions": "議論・インタビュー",
}


@dataclass
class KindReport:
    """What happened to one kind of resource.

    Attributes:
        source: Items found in v0.
        created: Items written to v1 (would be written, in a dry run).
        existing: Items already in v1 (left as they are).
        skipped: Items not migrated, by reason.
        failed: Items whose migration raised an error.
        shared: Resources whose departments were (would be) set.
        messages: Warnings and failures, one line each.
        excluded: The kind was left out with ``--include`` / ``--exclude``.
    """

    source: int = 0
    created: int = 0
    existing: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    failed: int = 0
    shared: int = 0
    messages: list[str] = field(default_factory=list)
    excluded: bool = False

    def skip(self, reason: str, detail: str | None = None) -> None:
        self.skipped[reason] += 1
        if detail:
            self.messages.append(f"スキップ（{reason}）: {detail}")

    def warn(self, detail: str) -> None:
        self.messages.append(f"注意: {detail}")

    def fail(self, detail: str) -> None:
        self.failed += 1
        self.messages.append(f"失敗: {detail}")


class Report:
    """The report of every kind, in the order they ran."""

    def __init__(self, kinds: tuple[str, ...], selected: tuple[str, ...]) -> None:
        self.kinds = {kind: KindReport(excluded=kind not in selected) for kind in kinds}

    def __getitem__(self, kind: str) -> KindReport:
        return self.kinds[kind]

    @property
    def failed(self) -> int:
        return sum(k.failed for k in self.kinds.values())

    def render(self, *, apply: bool) -> str:
        """The report as text."""
        verb = "作成" if apply else "作成予定"
        lines = [
            "=" * 60,
            "移行の結果" if apply else "移行の予定（dry-run。何も書き込んでいません）",
            "=" * 60,
        ]
        for kind, rep in self.kinds.items():
            label = _LABELS.get(kind, kind)
            if rep.excluded:
                lines.append(f"- {label}: 除外（--include / --exclude）")
                continue
            parts = [
                f"v0 {rep.source}",
                f"{verb} {rep.created}",
                f"移行済み {rep.existing}",
            ]
            if rep.shared:
                parts.append(f"共有の設定 {rep.shared}")
            if rep.skipped:
                detail = ", ".join(f"{r} {n}" for r, n in sorted(rep.skipped.items()))
                parts.append(f"スキップ {sum(rep.skipped.values())}（{detail}）")
            if rep.failed:
                parts.append(f"失敗 {rep.failed}")
            lines.append(f"- {label}: " + " / ".join(parts))
            lines.extend(f"    {m}" for m in rep.messages)
        lines.append("=" * 60)
        if self.failed:
            lines.append(
                f"失敗が {self.failed} 件あります。原因を直してから再実行してください。"
            )
        elif not apply:
            lines.append("内容を確認してから --apply を付けて本実行してください。")
        return "\n".join(lines)
