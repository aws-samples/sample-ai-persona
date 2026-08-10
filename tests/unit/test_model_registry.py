"""model_registry の単体テスト。

get_model_spec（丸める）/ is_supported（厳密）/ display_name_for（例外を投げない）/
list_selectable_models（Mantle出し分け）の関心分離を検証する。
"""

from src.config import config
from src.models.model_registry import (
    DEFAULT_MODEL_ID,
    MANTLE_FALLBACK_REGION,
    SUPPORTED_MODELS,
    ModelProvider,
    display_name_for,
    get_model_spec,
    is_supported,
    list_selectable_models,
    resolve_call_region,
)


class TestDefaultModelConsistency:
    def test_default_model_id_matches_config_agent_model_id(self):
        """未指定時の挙動を不変に保つための一致検査。"""
        assert DEFAULT_MODEL_ID == config.AGENT_MODEL_ID

    def test_default_model_id_is_registered(self):
        assert DEFAULT_MODEL_ID in SUPPORTED_MODELS


class TestGetModelSpec:
    def test_none_resolves_to_default(self):
        assert get_model_spec(None).model_id == DEFAULT_MODEL_ID

    def test_unknown_id_resolves_to_default(self):
        assert get_model_spec("unknown.model-id").model_id == DEFAULT_MODEL_ID

    def test_known_id_resolves_to_itself(self):
        spec = get_model_spec("openai.gpt-5.6-terra")
        assert spec.model_id == "openai.gpt-5.6-terra"
        assert spec.provider == ModelProvider.OPENAI_RESPONSES


class TestIsSupported:
    def test_known_id_is_supported(self):
        assert is_supported(DEFAULT_MODEL_ID) is True

    def test_unknown_id_is_not_supported(self):
        assert is_supported("unknown.model-id") is False


class TestDisplayNameFor:
    def test_none_falls_back_to_default_display_name(self):
        assert display_name_for(None) == SUPPORTED_MODELS[DEFAULT_MODEL_ID].display_name

    def test_unknown_id_falls_back_to_raw_value(self):
        assert display_name_for("unknown.model-id") == "unknown.model-id"

    def test_known_id_returns_display_name(self):
        assert (
            display_name_for(DEFAULT_MODEL_ID)
            == SUPPORTED_MODELS[DEFAULT_MODEL_ID].display_name
        )


class TestListSelectableModels:
    def test_disabled_excludes_requires_mantle_models(self):
        models = list_selectable_models(enable_additional_models=False)
        assert all(not spec.requires_mantle for spec in models)
        assert any(spec.model_id == DEFAULT_MODEL_ID for spec in models)

    def test_enabled_includes_all_models(self):
        models = list_selectable_models(enable_additional_models=True)
        model_ids = {spec.model_id for spec in models}
        assert model_ids == set(SUPPORTED_MODELS.keys())


class TestGemma4RequestSizeLimit:
    def test_gemma4_has_request_byte_limit(self):
        spec = SUPPORTED_MODELS["google.gemma-4-31b"]
        assert spec.max_request_bytes == int(3.5 * 1024 * 1024)

    def test_other_models_have_no_request_byte_limit(self):
        for model_id, spec in SUPPORTED_MODELS.items():
            if model_id == "google.gemma-4-31b":
                continue
            assert spec.max_request_bytes is None


class TestResolveCallRegion:
    def test_bedrock_model_follows_deploy_region(self):
        """Claude系(BEDROCK)はMantle制約がないためデプロイ先にそのまま追従する。"""
        spec = SUPPORTED_MODELS[DEFAULT_MODEL_ID]
        assert resolve_call_region(spec, "ap-northeast-1") == "ap-northeast-1"

    def test_mantle_model_follows_deploy_region_when_supported(self):
        """デプロイ先がMantle対応リージョン(us-west-2)ならそのまま使う。"""
        spec = SUPPORTED_MODELS["openai.gpt-5.6-terra"]
        assert resolve_call_region(spec, "us-west-2") == "us-west-2"

    def test_mantle_model_falls_back_when_deploy_region_unsupported(self):
        """デプロイ先がMantle非対応リージョン(東京)ならフォールバックする。"""
        spec = SUPPORTED_MODELS["google.gemma-4-31b"]
        assert resolve_call_region(spec, "ap-northeast-1") == MANTLE_FALLBACK_REGION

    def test_mantle_fallback_region_is_itself_supported(self):
        """フォールバック先がMantle対応リージョンでなければ無限に非対応になる。"""
        from src.models.model_registry import MANTLE_SUPPORTED_REGIONS

        assert MANTLE_FALLBACK_REGION in MANTLE_SUPPORTED_REGIONS
