"""古い読み手が残っていないこと（U5・U6）。

U5（infrastructure-specification.md「古いテーブルの読み手の付け替え」）: 古い
PersonaDatasetBindings・PersonaKBBindings の権限と環境変数を外すため、それらを読む
コードが 1 つでも残ると、新しいタスクで紐付けの読み出しが失敗する。

U6（BR8.2、NFR2.15）: Discussions の型固定の GSI（CreatedAtIndex・TopicIndex・
ModeIndex）を読むコードを残さない（CDK から外すのは U9）。判定を通らずにペルソナを
読む ``get_persona_unchecked`` もなくす（BR2.8、NFR1.46）。

U5（BR8.2）: Personas・UploadedFiles の型固定の GSI（NameIndex・OccupationIndex・
UploadedAtIndex）を読むコードを残さない。
"""

from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent.parent
_LEGACY_NAMES = (
    "PersonaDatasetBindings",
    "PersonaKBBindings",
    "PERSONA_DATASET_BINDINGS_TABLE_NAME",
    "PERSONA_KB_BINDINGS_TABLE_NAME",
    "get_bindings_by_persona",
    "get_kb_binding_by_persona",
    # U5 BR8.2: Personas・UploadedFiles の type 固定の GSI
    "NameIndex",
    "OccupationIndex",
    "UploadedAtIndex",
    # U6
    "CreatedAtIndex",
    "TopicIndex",
    "ModeIndex",
    "get_persona_unchecked",
)


def _sources() -> list[Path]:
    files: list[Path] = []
    for top in ("src", "web"):
        files += [
            p
            for p in (_ROOT / top).rglob("*")
            if p.suffix in {".py", ".html", ".js"} and "__pycache__" not in p.parts
        ]
    return files


@pytest.mark.parametrize("name", _LEGACY_NAMES)
def test_no_source_names_the_legacy_readers(name):
    offenders = [
        str(p.relative_to(_ROOT))
        for p in _sources()
        if name in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == [], f"{name} is still used in {offenders}"


def test_the_check_sees_the_sources():
    """空振りしないこと（src と web の両方を読んでいる）。"""
    names = {p.relative_to(_ROOT).parts[0] for p in _sources()}
    assert names == {"src", "web"}
