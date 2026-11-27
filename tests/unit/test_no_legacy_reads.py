"""No old readers remain.

The permissions and environment variables for the old PersonaDatasetBindings and
PersonaKBBindings tables are being removed, so any code left that reads them would make
binding reads fail in new tasks.

No code may read the Discussions type-fixed GSIs (CreatedAtIndex, TopicIndex,
ModeIndex), and ``get_persona_unchecked``, which reads a persona without going through
the authorization decision, is gone.

No code may read the Personas and UploadedFiles type-fixed GSIs (NameIndex,
OccupationIndex, UploadedAtIndex) either.
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
    # Personas and UploadedFiles type-fixed GSIs
    "NameIndex",
    "OccupationIndex",
    "UploadedAtIndex",
    # Discussions type-fixed GSIs
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
    """Does not pass vacuously (reads both src and web)."""
    names = {p.relative_to(_ROOT).parts[0] for p in _sources()}
    assert names == {"src", "web"}
