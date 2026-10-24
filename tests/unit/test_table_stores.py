"""テーブルごとのストアに関する静的な検査。

- DatabaseService は消した。読み書きはテーブルごとのストア（``PersonaStore``、
  ``SurveyStore``、``DiscussionStore`` など）が持ち、Manager はそれを受け取る。
- Personas・UploadedFiles の type 固定の GSI を読むコードがない（U5 BR8.2）。
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_PERSONA_FILE_GSIS = (
    "CreatedAtIndex",
    "NameIndex",
    "OccupationIndex",
    "UploadedAtIndex",
)


class TestDatabaseServiceRemoved:
    def test_module_is_gone(self):
        assert not (ROOT / "src/services/database_service.py").exists()

    def test_factory_has_no_getter(self):
        from src.services.service_factory import ServiceFactory

        assert not hasattr(ServiceFactory, "get_database_service")

    @pytest.mark.parametrize(
        "module, cls",
        [
            ("src.managers.discussion_manager", "DiscussionManager"),
            ("src.managers.file_manager", "FileManager"),
            ("src.managers.persona_generation_manager", "PersonaGenerationManager"),
            ("src.managers.persona_memory_manager", "PersonaMemoryManager"),
            ("src.managers.report_manager", "ReportManager"),
        ],
    )
    def test_managers_take_no_database_service(self, module, cls):
        manager = getattr(importlib.import_module(module), cls)
        parameters = inspect.signature(manager.__init__).parameters

        assert "database_service" not in parameters
        assert "db_service" not in parameters

    def test_nothing_names_it(self):
        for path in [*ROOT.glob("src/**/*.py"), *ROOT.glob("web/**/*.py")]:
            source = path.read_text(encoding="utf-8")
            assert "DatabaseService" not in source, path
            assert "get_database_service" not in source, path


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
