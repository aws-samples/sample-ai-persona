"""Personas・UploadedFiles の type 固定の GSI を読むコードがないことの静的な検査（U5 BR8.2）。"""

import ast
import inspect
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

_PERSONA_FILE_GSIS = (
    "CreatedAtIndex",
    "NameIndex",
    "OccupationIndex",
    "UploadedAtIndex",
)


class TestNoTypeFixedIndexReads:
    """Personas・UploadedFiles の type 固定の GSI を読むコードがない（BR8.2）。

    Discussions の CreatedAtIndex・ModeIndex は U6 の担当のため対象外。
    """

    def _functions(self) -> dict[str, str]:
        import src.services.persona_store as module

        source = inspect.getsource(module)
        lines = source.splitlines()
        return {
            node.name: "\n".join(lines[node.lineno - 1 : node.end_lineno])
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
        }

    def test_persona_reads_do_not_use_type_fixed_indexes(self):
        for name, body in self._functions().items():
            for index in _PERSONA_FILE_GSIS:
                assert index not in body, f"{name} reads {index}"

    def test_persona_only_indexes_are_not_named_anywhere(self):
        for path in ROOT.glob("src/services/*.py"):
            source = path.read_text(encoding="utf-8")
            for index in ("NameIndex", "OccupationIndex", "UploadedAtIndex"):
                assert index not in source, f"{path.name} names {index}"
