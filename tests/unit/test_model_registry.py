"""model_registry の単体テスト。

推論の範囲（閉域対応 U5、BR4.1・BR4.2・BR4.4〜BR4.6）ごとの表と一覧、呼ぶモデルを決める
resolve_model_spec（読み替え）、display_name_for（例外を投げない）、resolve_call_region を検証する。
"""

import pytest

from src.config import Config
from src.models.model_registry import (
    MANTLE_FALLBACK_REGION,
    SCOPE_DEFAULTS,
    SUPPORTED_MODELS,
    InferenceScope,
    ModelProvider,
    default_selectable_model_id,
    display_name_for,
    list_selectable_models,
    models_in_scope,
    resolve_call_region,
    resolve_model_spec,
)

# 範囲ごとの既定（BR1.6 と計画の「設計からの変更」）。cdk/test/deployment-profile.test.ts にも
# 同じ値を書く（BR4.6）。どちらかを変えたら両方のテストが落ちる
GLOBAL_HAIKU_5_5 = "global.anthropic.claude-haiku-5-5"
GLOBAL_HAIKU_4_5 = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
GLOBAL_SONNET_5 = "global.anthropic.claude-sonnet-5"
GLOBAL_SONNET_5_5 = "global.anthropic.claude-sonnet-5-5"
JP_HAIKU_5_5 = "jp.anthropic.claude-haiku-5-5"
JP_HAIKU_4_5 = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
GEMMA = "google.gemma-4-31b"
TERRA = "openai.gpt-5.6-terra"
LUNA = "openai.gpt-5.6-luna"
MANTLE_MODEL_IDS = {TERRA, LUNA, GEMMA}


class TestScopeDefaults:
    """BR1.6・BR4.6: 範囲ごとの既定のモデル ID 3 つ（CDK の ScopeDefaults と同じ値）。"""

    def test_global_defaults(self):
        defaults = SCOPE_DEFAULTS[InferenceScope.GLOBAL]
        assert defaults.bedrock_model_id == GLOBAL_SONNET_5_5
        assert defaults.agent_model_id == GLOBAL_HAIKU_5_5
        assert defaults.batch_inference_model_id == GLOBAL_HAIKU_4_5

    def test_jp_defaults(self):
        defaults = SCOPE_DEFAULTS[InferenceScope.JP]
        assert defaults.bedrock_model_id == JP_HAIKU_5_5
        assert defaults.agent_model_id == JP_HAIKU_5_5
        assert defaults.batch_inference_model_id == JP_HAIKU_4_5

    def test_only_two_scopes(self):
        assert set(SCOPE_DEFAULTS) == {InferenceScope.GLOBAL, InferenceScope.JP}
        assert [scope.value for scope in InferenceScope] == ["global", "jp"]

    def test_config_class_default_is_the_global_agent_model(self):
        """環境変数のないクラスの属性の既定は SCOPE_DEFAULTS の global（BR3.4）。"""
        defaults = SCOPE_DEFAULTS[InferenceScope.GLOBAL]
        assert Config.AGENT_MODEL_ID == defaults.agent_model_id
        assert Config.BEDROCK_MODEL_ID == defaults.bedrock_model_id
        assert Config.BATCH_INFERENCE_MODEL_ID == defaults.batch_inference_model_id


class TestScopeTables:
    """BR4.1: 範囲と baseModel を持つ表。"""

    def test_global_has_seven_models(self):
        ids = [spec.model_id for spec in models_in_scope(InferenceScope.GLOBAL)]
        assert ids == [
            GLOBAL_HAIKU_5_5,
            GLOBAL_HAIKU_4_5,
            GLOBAL_SONNET_5_5,
            GLOBAL_SONNET_5,
            TERRA,
            LUNA,
            GEMMA,
        ]

    def test_jp_has_two_models_only_jp_prefixed_without_mantle(self):
        specs = models_in_scope(InferenceScope.JP)
        assert [spec.model_id for spec in specs] == [JP_HAIKU_5_5, JP_HAIKU_4_5]
        assert all(spec.model_id.startswith("jp.") for spec in specs)
        assert not any(spec.requires_mantle for spec in specs)

    @pytest.mark.parametrize("scope", list(InferenceScope))
    def test_exactly_one_default_per_scope_matching_scope_defaults(self, scope):
        defaults = [spec for spec in models_in_scope(scope) if spec.is_default]
        assert len(defaults) == 1
        assert defaults[0].model_id == SCOPE_DEFAULTS[scope].agent_model_id

    def test_same_model_has_same_base_model_and_display_name(self):
        pairs = [(GLOBAL_HAIKU_5_5, JP_HAIKU_5_5), (GLOBAL_HAIKU_4_5, JP_HAIKU_4_5)]
        for global_id, jp_id in pairs:
            global_spec = SUPPORTED_MODELS[global_id]
            jp_spec = SUPPORTED_MODELS[jp_id]
            assert global_spec.base_model == jp_spec.base_model
            assert global_spec.display_name == jp_spec.display_name
        assert SUPPORTED_MODELS[GLOBAL_HAIKU_5_5].display_name == "Claude Haiku 5.5"

    def test_mantle_ids_match_the_cdk_additional_model_ids(self):
        """BR4.6: CDK の追加のモデルの ID（deployment-profile.ts）と同じ 3 つ。"""
        mantle = {
            spec.model_id for spec in SUPPORTED_MODELS.values() if spec.requires_mantle
        }
        assert mantle == MANTLE_MODEL_IDS

    def test_every_spec_belongs_to_its_scope_by_prefix(self):
        for spec in SUPPORTED_MODELS.values():
            assert spec.scope == (
                InferenceScope.JP
                if spec.model_id.startswith("jp.")
                else InferenceScope.GLOBAL
            )


class TestListSelectableModels:
    """BR4.2: 範囲に属するモデルだけ。Mantle 系は有効なときだけ。"""

    def test_global_disabled_lists_haiku_5_5_first_without_mantle(self):
        ids = [
            spec.model_id
            for spec in list_selectable_models(False, InferenceScope.GLOBAL)
        ]
        assert ids == [
            GLOBAL_HAIKU_5_5,
            GLOBAL_HAIKU_4_5,
            GLOBAL_SONNET_5_5,
            GLOBAL_SONNET_5,
        ]

    def test_global_enabled_includes_mantle(self):
        ids = [spec.model_id for spec in list_selectable_models(True, "global")]
        assert ids == [
            GLOBAL_HAIKU_5_5,
            GLOBAL_HAIKU_4_5,
            GLOBAL_SONNET_5_5,
            GLOBAL_SONNET_5,
            TERRA,
            LUNA,
            GEMMA,
        ]

    @pytest.mark.parametrize("enabled", [True, False])
    def test_jp_lists_two_models(self, enabled):
        ids = [spec.model_id for spec in list_selectable_models(enabled, "jp")]
        assert ids == [JP_HAIKU_5_5, JP_HAIKU_4_5]

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError):
            list_selectable_models(False, "us")


class TestDefaultSelectableModelId:
    """BR5.1: 設定画面の既定の選択。"""

    def test_agent_model_in_list_is_selected(self):
        models = list_selectable_models(False, "global")
        assert default_selectable_model_id(models, GLOBAL_SONNET_5) == GLOBAL_SONNET_5

    def test_agent_model_not_in_list_falls_back_to_list_default(self):
        models = list_selectable_models(False, "jp")
        assert default_selectable_model_id(models, "custom.model") == JP_HAIKU_5_5

    def test_mantle_agent_model_hidden_from_list_falls_back(self):
        models = list_selectable_models(False, "global")
        assert default_selectable_model_id(models, GEMMA) == GLOBAL_HAIKU_5_5


class TestResolveModelSpec:
    """BR4.4・BR4.5: 呼ぶモデルの仕様を 1 つの関数で決める（functional-spec 4.6）。"""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (GLOBAL_HAIKU_4_5, JP_HAIKU_4_5),  # 同じモデルに読み替え
            (GLOBAL_SONNET_5, JP_HAIKU_5_5),  # 国内に同じモデルがないので既定
            (GEMMA, JP_HAIKU_5_5),  # Mantle 系は既定
            ("unknown.model-id", JP_HAIKU_5_5),  # 表にない ID は既定
        ],
    )
    def test_functional_spec_examples_in_jp(self, stored, expected):
        spec = resolve_model_spec(stored, "jp", JP_HAIKU_5_5)
        assert spec.model_id == expected

    def test_gemma_in_jp_has_no_request_limit(self):
        """読み替え後の仕様でサイズを判定する（Gemma の 3.5 MB を使わない）。"""
        assert resolve_model_spec(GEMMA, "jp", JP_HAIKU_5_5).max_request_bytes is None

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (JP_HAIKU_4_5, GLOBAL_HAIKU_4_5),
            (JP_HAIKU_5_5, GLOBAL_HAIKU_5_5),
        ],
    )
    def test_back_to_global_reads_jp_ids_as_global(self, stored, expected):
        assert (
            resolve_model_spec(stored, "global", GLOBAL_HAIKU_5_5).model_id == expected
        )

    def test_in_scope_id_is_itself(self):
        spec = resolve_model_spec(TERRA, InferenceScope.GLOBAL, GLOBAL_HAIKU_5_5)
        assert spec.model_id == TERRA
        assert spec.provider == ModelProvider.OPENAI_RESPONSES

    @pytest.mark.parametrize("empty", [None, ""])
    def test_none_or_empty_is_the_default(self, empty):
        assert resolve_model_spec(empty, "jp", JP_HAIKU_5_5).model_id == JP_HAIKU_5_5

    def test_unknown_id_in_global_goes_to_agent_model_id(self):
        """global でも、表にない ID の行き先は AGENT_MODEL_ID（BR4.5）。"""
        spec = resolve_model_spec("unknown.model-id", "global", GLOBAL_HAIKU_5_5)
        assert spec.model_id == GLOBAL_HAIKU_5_5

    def test_unknown_id_follows_a_mantle_default_in_global(self):
        spec = resolve_model_spec("unknown.model-id", "global", GEMMA)
        assert spec.model_id == GEMMA
        assert spec.requires_mantle is True

    def test_default_from_another_scope_is_used_as_is(self):
        """AGENT_MODEL_ID が別の範囲の ID でも、その仕様で呼ぶ（BR4.5）。"""
        spec = resolve_model_spec(None, "jp", GLOBAL_SONNET_5)
        assert spec.model_id == GLOBAL_SONNET_5
        assert spec.scope == InferenceScope.GLOBAL

    def test_explicit_default_from_another_scope_is_not_read_through(self):
        spec = resolve_model_spec(GLOBAL_HAIKU_4_5, "jp", GLOBAL_HAIKU_4_5)
        assert spec.model_id == GLOBAL_HAIKU_4_5

    def test_unlisted_default_gets_a_plain_bedrock_spec(self):
        spec = resolve_model_spec("unknown.model-id", "jp", "custom.model-v1")
        assert spec.model_id == "custom.model-v1"
        assert spec.display_name == "custom.model-v1"
        assert spec.provider == ModelProvider.BEDROCK
        assert spec.requires_mantle is False
        assert spec.max_request_bytes is None
        assert spec.scope == InferenceScope.JP

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError):
            resolve_model_spec(None, "world", GLOBAL_HAIKU_5_5)


class TestDisplayNameFor:
    """BR5.2: 表示名は保存された ID で出す（今のまま）。"""

    def test_none_falls_back_to_default_display_name(self):
        assert display_name_for(None) == "Claude Haiku 5.5"

    def test_unknown_id_falls_back_to_raw_value(self):
        assert display_name_for("unknown.model-id") == "unknown.model-id"

    def test_jp_id_has_the_same_name_as_global(self):
        assert display_name_for(JP_HAIKU_4_5) == "Claude Haiku 4.5"
        assert display_name_for(GLOBAL_HAIKU_4_5) == "Claude Haiku 4.5"


class TestGemma4RequestSizeLimit:
    def test_gemma4_has_request_byte_limit(self):
        spec = SUPPORTED_MODELS[GEMMA]
        assert spec.max_request_bytes == int(3.5 * 1024 * 1024)

    def test_other_models_have_no_request_byte_limit(self):
        for model_id, spec in SUPPORTED_MODELS.items():
            if model_id == GEMMA:
                continue
            assert spec.max_request_bytes is None


class TestResolveCallRegion:
    def test_bedrock_model_follows_deploy_region(self):
        """Claude系(BEDROCK)はMantle制約がないためデプロイ先にそのまま追従する。"""
        spec = SUPPORTED_MODELS[GLOBAL_HAIKU_4_5]
        assert resolve_call_region(spec, "ap-northeast-1") == "ap-northeast-1"

    def test_mantle_model_follows_deploy_region_when_supported(self):
        """デプロイ先がMantle対応リージョン(us-west-2)ならそのまま使う。"""
        spec = SUPPORTED_MODELS[TERRA]
        assert resolve_call_region(spec, "us-west-2") == "us-west-2"

    def test_mantle_model_falls_back_when_deploy_region_unsupported(self):
        """デプロイ先がMantle非対応リージョン(東京)ならフォールバックする。"""
        spec = SUPPORTED_MODELS[GEMMA]
        assert resolve_call_region(spec, "ap-northeast-1") == MANTLE_FALLBACK_REGION

    def test_mantle_fallback_region_is_itself_supported(self):
        """フォールバック先がMantle対応リージョンでなければ無限に非対応になる。"""
        from src.models.model_registry import MANTLE_SUPPORTED_REGIONS

        assert MANTLE_FALLBACK_REGION in MANTLE_SUPPORTED_REGIONS
