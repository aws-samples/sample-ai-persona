"""PersonaGenerationManager のデータセット分析連携（source 経路）テスト。"""

import os
from unittest.mock import Mock

import pytest

# モジュール経由で参照し import 形式を from ... import に統一する
# （同一モジュールの import / import from 混在を避ける。config / save_temp_csv 等の
#  monkeypatch もこのハンドル経由で行う）。
from src.managers import persona_generation_manager as mod

pytestmark = pytest.mark.unit


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
        database_service=Mock(),
        dataset_analysis_service=ds,
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
            # combined text は別名を使い、元ファイル名を出さない
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
            # 型が推定され、プロンプトにも型が現れる
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
        mgr.generate_and_cache(
            file_contents=[(b"region,amount\neast,10\n", "d.csv")],
            data_type="purchase",
            persona_count=1,
        )
        assert created and all(not os.path.exists(p) for p in created)

    def test_temp_removed_on_schema_parse_error(self, monkeypatch):
        # analyze_csv_schema が Manager 例外以外（_csv.Error 等）を投げても
        # temp CSV は残らない（_extract_file_texts の except が広いこと）。
        mgr = _manager()
        created = self._capture_paths(mgr, monkeypatch)

        def boom(_content):
            raise ValueError("field larger than field limit")

        monkeypatch.setattr(mod, "analyze_csv_schema", boom)

        with pytest.raises(Exception):  # noqa: B017 - Manager 例外に限定されない
            mgr._extract_file_texts([(b"region,amount\neast,10\n", "d.csv")])
        assert created and all(not os.path.exists(p) for p in created)

    def test_preview_header_omits_tool_claim(self):
        # プレビュー見出しは analyze_dataset 可用性を主張しない
        # （有効判定前に生成されるため、無効時の誤誘導を避ける）。
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
        # build_source_tools が失敗しても temp CSV は残らない（抽出直後から finally）。
        ds = Mock()
        ds.build_source_tools.side_effect = RuntimeError("tool build boom")
        mgr = _manager(dataset_service=ds)
        created = self._capture_paths(mgr, monkeypatch)

        with pytest.raises(mod.PersonaGenerationManagerError):
            mgr.generate_and_cache(
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
        mgr.generate_and_cache(
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
        mgr.generate_and_cache(
            file_contents=[(b"region,amount\neast,10\n", "d.csv")],
            data_type="purchase",
            persona_count=1,
        )
        ds.build_source_tools.assert_called_once()
        # 採番済み別名で呼ばれる
        descriptors = ds.build_source_tools.call_args[0][0]
        assert descriptors[0]["alias"] == "source_1"


class TestConstructorDI:
    def test_accepts_dataset_analysis_service(self):
        ds = Mock()
        mgr = mod.PersonaGenerationManager(
            agent_service=Mock(),
            database_service=Mock(),
            dataset_analysis_service=ds,
        )
        assert mgr.dataset_analysis_service is ds
