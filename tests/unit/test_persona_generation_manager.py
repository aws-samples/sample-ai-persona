"""Tests for PersonaGenerationManager dataset analysis integration (source path)."""

import os
from unittest.mock import Mock

import pytest

# Reference through the module so the import style stays uniform (``from ... import``);
# monkeypatching config / save_temp_csv etc. also goes through this handle.
from src.managers import persona_generation_manager as mod
from tests.conftest import MEMBER_A

pytestmark = pytest.mark.unit

#: Generation requires ai:execute (generate takes the user)
MEMBER = MEMBER_A.principal()


def _valid_result():
    return mod._PersonaListOutput(
        personas=[
            mod._PersonaOutput(
                name="田中太郎",
                age=35,
                gender="male",
                country="JP",
                city="東京",
                occupation="会社員",
                background="背景",
                values=["v"],
                pain_points=["p"],
                goals=["g"],
            )
        ]
    )


def _manager(dataset_service=None, run_side_effect=None):
    agent_service = Mock()
    agent_service.create_generation_agent.return_value = Mock()
    if run_side_effect is not None:
        agent_service.run_persona_generation.side_effect = run_side_effect
    else:
        agent_service.run_persona_generation.return_value = (_valid_result(), [])
    ds = dataset_service or Mock()
    if dataset_service is None:
        ds.build_source_tools.return_value = [Mock()]
    return mod.PersonaGenerationManager(
        agent_service=agent_service,
        dataset_analysis_service=ds,
        candidate_store=Mock(),
        s3_service=None,
    )


class TestExtractFileTexts:
    def test_csv_descriptor_uses_alias_not_filename(self):
        mgr = _manager()
        content = "region,amount\neast,10\n".encode("utf-8")
        combined, descriptors = mgr._extract_file_texts([(content, "購買履歴.csv")])
        try:
            assert len(descriptors) == 1
            d = descriptors[0]
            assert d["alias"] == "source_1"
            assert d["columns"] == ["region", "amount"]
            assert os.path.exists(d["path"])
            # The combined text uses an alias and does not expose the original file name
            assert "source_1" in combined
            assert "購買履歴" not in combined
        finally:
            from src.managers.shared.file_utils import cleanup_temp_files

            cleanup_temp_files([d["path"] for d in descriptors])

    def test_shift_jis_csv_columns_inferred(self):
        mgr = _manager()
        content = "地域,金額\n東京,10\n".encode("shift_jis")
        combined, descriptors = mgr._extract_file_texts([(content, "sjis.csv")])
        try:
            assert descriptors[0]["columns"] == ["地域", "金額"]
        finally:
            from src.managers.shared.file_utils import cleanup_temp_files

            cleanup_temp_files([d["path"] for d in descriptors])

    def test_source_descriptor_has_column_types(self):
        mgr = _manager()
        content = b"region,amount\neast,10\n"
        combined, descriptors = mgr._extract_file_texts([(content, "d.csv")])
        try:
            detail = descriptors[0]["columns_detail"]
            names = [c["name"] for c in detail]
            assert names == ["region", "amount"]
            # The type is inferred and appears in the prompt
            prompt = mgr._build_user_prompt("data", 1, descriptors)
            assert "(integer)" in prompt or "(string)" in prompt
        finally:
            from src.managers.shared.file_utils import cleanup_temp_files

            cleanup_temp_files([d["path"] for d in descriptors])

    def test_multiple_csv_get_distinct_aliases(self):
        mgr = _manager()
        c1 = b"a\n1\n"
        c2 = b"b\n2\n"
        combined, descriptors = mgr._extract_file_texts(
            [(c1, "one.csv"), (c2, "two.csv")]
        )
        try:
            assert [d["alias"] for d in descriptors] == ["source_1", "source_2"]
        finally:
            from src.managers.shared.file_utils import cleanup_temp_files

            cleanup_temp_files([d["path"] for d in descriptors])


class TestTempCleanup:
    def _capture_paths(self, mgr, monkeypatch):
        created: list[str] = []
        orig = mod.save_temp_csv

        def spy(content):
            path = orig(content)
            created.append(path)
            return path

        monkeypatch.setattr(mod, "save_temp_csv", spy)
        return created

    def test_temp_removed_on_success(self, monkeypatch):
        mgr = _manager()
        created = self._capture_paths(mgr, monkeypatch)
        mgr.generate(
            MEMBER,
            file_contents=[(b"region,amount\neast,10\n", "d.csv")],
            data_type="purchase",
            persona_count=1,
        )
        assert created and all(not os.path.exists(p) for p in created)

    def test_temp_removed_on_schema_parse_error(self, monkeypatch):
        # Even if analyze_csv_schema raises a non-Manager exception (e.g. _csv.Error),
        # the temp CSV is not left behind (the except in _extract_file_texts is broad).
        mgr = _manager()
        created = self._capture_paths(mgr, monkeypatch)

        def boom(_content):
            raise ValueError("field larger than field limit")

        monkeypatch.setattr(mod, "analyze_csv_schema", boom)

        with pytest.raises(Exception):  # noqa: B017 - not limited to Manager exceptions
            mgr._extract_file_texts([(b"region,amount\neast,10\n", "d.csv")])
        assert created and all(not os.path.exists(p) for p in created)

    def test_preview_header_omits_tool_claim(self):
        # The preview heading does not claim analyze_dataset availability
        # (it is generated before the enabled check, so avoid misleading when disabled).
        mgr = _manager()
        combined, descriptors = mgr._extract_file_texts(
            [(b"region,amount\neast,10\n", "d.csv")]
        )
        try:
            assert "analyze_dataset" not in combined
            assert "先頭20行プレビュー" in combined
        finally:
            from src.managers.shared.file_utils import cleanup_temp_files

            cleanup_temp_files([d["path"] for d in descriptors])

    def test_temp_removed_on_tool_build_exception(self, monkeypatch):
        # The temp CSV is not left behind even if build_source_tools fails (finally right after extraction).
        ds = Mock()
        ds.build_source_tools.side_effect = RuntimeError("tool build boom")
        mgr = _manager(dataset_service=ds)
        created = self._capture_paths(mgr, monkeypatch)

        with pytest.raises(mod.PersonaGenerationManagerError):
            mgr.generate(
                MEMBER,
                file_contents=[(b"region,amount\neast,10\n", "d.csv")],
                data_type="purchase",
                persona_count=1,
            )
        assert created and all(not os.path.exists(p) for p in created)


class TestEffectiveFlag:
    def test_disabled_global_flag_skips_dataset_tools(self, monkeypatch):
        monkeypatch.setattr(mod.config, "ENABLE_DATASET_ANALYSIS", False)
        ds = Mock()
        mgr = _manager(dataset_service=ds)
        mgr.generate(
            MEMBER,
            file_contents=[(b"region,amount\neast,10\n", "d.csv")],
            data_type="purchase",
            persona_count=1,
        )
        ds.build_source_tools.assert_not_called()

    def test_enabled_flag_builds_source_tools(self, monkeypatch):
        monkeypatch.setattr(mod.config, "ENABLE_DATASET_ANALYSIS", True)
        ds = Mock()
        ds.build_source_tools.return_value = [Mock()]
        mgr = _manager(dataset_service=ds)
        mgr.generate(
            MEMBER,
            file_contents=[(b"region,amount\neast,10\n", "d.csv")],
            data_type="purchase",
            persona_count=1,
        )
        ds.build_source_tools.assert_called_once()
        # Called with the assigned alias
        descriptors = ds.build_source_tools.call_args[0][0]
        assert descriptors[0]["alias"] == "source_1"


class TestDataAgentNotAllowed:
    """data_type=dwh generation is stopped."""

    @pytest.fixture
    def not_allowed(self, monkeypatch):
        monkeypatch.setattr(mod.config, "DATA_AGENT_ALLOWED", False)
        monkeypatch.setattr(mod.config, "DATA_AGENT_RUNTIME_ARN", "arn:agent")

    def test_dwh_generation_stops_before_any_tool_is_built(
        self, monkeypatch, not_allowed
    ):
        from src.models.errors import ErrorCode
        from tests.error_helpers import raises_code

        create_tool = Mock()
        monkeypatch.setattr(
            "src.services.data_agent_service.create_data_agent_tool", create_tool
        )
        mgr = _manager()
        with raises_code(
            mod.PersonaGenerationManagerError, ErrorCode.DATA_AGENT_NOT_ALLOWED
        ):
            mgr.generate(
                MEMBER,
                file_contents=[],
                data_type="dwh",
                persona_count=1,
                data_description="購買",
            )
        create_tool.assert_not_called()
        mgr.agent_service.run_persona_generation.assert_not_called()

    def test_pre_check_stops_too(self, not_allowed):
        from src.models.errors import ErrorCode
        from tests.error_helpers import raises_code

        with raises_code(
            mod.PersonaGenerationManagerError, ErrorCode.DATA_AGENT_NOT_ALLOWED
        ):
            _manager().ensure_data_agent_allowed(MEMBER)

    def test_pre_check_needs_ai_execute_first(self, not_allowed):
        from src.models.errors import AccessDeniedError
        from tests.conftest import NO_ROLE

        with pytest.raises(AccessDeniedError):
            _manager().ensure_data_agent_allowed(NO_ROLE.principal())

    def test_pre_check_passes_when_allowed(self, monkeypatch):
        monkeypatch.setattr(mod.config, "DATA_AGENT_ALLOWED", True)
        _manager().ensure_data_agent_allowed(MEMBER)

    def test_file_generation_is_not_affected(self, not_allowed):
        mgr = _manager()
        result = mgr.generate(
            MEMBER,
            file_contents=[
                ("顧客インタビューの十分に長い本文です。".encode("utf-8"), "a.txt")
            ],
            data_type="interview",
            persona_count=1,
        )
        assert len(result.personas) == 1


class TestConstructorDI:
    def test_accepts_dataset_analysis_service(self):
        ds = Mock()
        mgr = mod.PersonaGenerationManager(
            agent_service=Mock(),
            dataset_analysis_service=ds,
        )
        assert mgr.dataset_analysis_service is ds
