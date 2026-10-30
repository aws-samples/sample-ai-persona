"""ペルソナ議論・インタビューで選択可能なAIモデルのレジストリ。

モデルによってStrandsのプロバイダクラス自体が異なる（BedrockModel経由のConverse呼び出しと、
OpenAIResponsesModel経由のBedrock Mantle呼び出し）ため、model_idだけでなく呼び出し経路の情報を
ここで一元管理する。標準ライブラリのみを使い他層に依存しない（architecture.md Models層規約）。

閉域対応 U5（BR4.1〜BR4.6）: 推論の範囲（global / jp）ごとに選べるモデルの表と既定のモデル ID を
持つ。範囲と既定のモデル ID は config を読まず、呼ぶ側（Manager・Service・Router）が
config.INFERENCE_SCOPE と config.AGENT_MODEL_ID を渡す。
"""

from dataclasses import dataclass
from enum import Enum


class ModelProvider(str, Enum):
    """モデルの呼び出し経路。"""

    BEDROCK = "bedrock"  # BedrockModel / Converse / SigV4
    OPENAI_RESPONSES = (
        "openai_responses"  # OpenAIResponsesModel / Mantle / bedrock_mantle_config
    )


class InferenceScope(str, Enum):
    """推論を送ってよい範囲（CDK の inferenceScope、環境変数 INFERENCE_SCOPE）。"""

    GLOBAL = "global"
    JP = "jp"


@dataclass(frozen=True)
class ModelSpec:
    """選択可能な1モデルの仕様。"""

    model_id: str
    display_name: str
    provider: ModelProvider
    requires_mantle: bool = False
    # 能力メタデータ。Manager層の入力サイズ検証で実際に使う（保持のみではない）。
    max_image_bytes: int | None = None
    max_request_bytes: int | None = None
    supports_pdf: bool = True
    # 属する範囲（1 つの ID は 1 つの範囲だけに属する）と、範囲をまたいで同じモデルを結ぶキー
    scope: InferenceScope = InferenceScope.GLOBAL
    base_model: str = ""
    # 範囲ごとにちょうど 1 つ（SCOPE_DEFAULTS の agent_model_id と同じ）
    is_default: bool = False


@dataclass(frozen=True)
class ScopeDefaults:
    """範囲ごとの既定のモデル ID 3 つ（cdk/lib/deployment-profile.ts の SCOPE_DEFAULTS と同じ値）。"""

    bedrock_model_id: str  # ペルソナの生成・インサイト・レポート（BEDROCK_MODEL_ID）
    agent_model_id: str  # 議論・インタビューの既定（AGENT_MODEL_ID）
    batch_inference_model_id: str  # アンケートのバッチ推論（BATCH_INFERENCE_MODEL_ID）


# BR1.6 と計画の「設計からの変更」（2026-10-27）。jp のバッチ推論は Haiku 4.5
# （Haiku 5.5 はバッチ推論の対応表にない）。値の一致は pytest と jest の両方で固定する（BR4.6）
SCOPE_DEFAULTS: dict[InferenceScope, ScopeDefaults] = {
    InferenceScope.GLOBAL: ScopeDefaults(
        bedrock_model_id="global.anthropic.claude-sonnet-5-5",
        agent_model_id="global.anthropic.claude-haiku-5-5",
        batch_inference_model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
    ),
    InferenceScope.JP: ScopeDefaults(
        bedrock_model_id="jp.anthropic.claude-haiku-5-5",
        agent_model_id="jp.anthropic.claude-haiku-5-5",
        batch_inference_model_id="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    ),
}

# Bedrock Mantle対応リージョン（GPT-5.6 Terra/Luna・Gemma 4 31B共通）。
# デプロイ先（config.AWS_REGION）がこれに含まれない場合はMANTLE_FALLBACK_REGIONへ丸める。
MANTLE_SUPPORTED_REGIONS: frozenset[str] = frozenset(
    {"us-east-1", "us-east-2", "us-west-2"}
)
MANTLE_FALLBACK_REGION = "us-east-1"

_GLOBAL = InferenceScope.GLOBAL
_JP = InferenceScope.JP

# 表の並び順がそのまま設定画面の並び順になる（範囲ごとに既定が先頭）
_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        model_id="global.anthropic.claude-haiku-5-5",
        display_name="Claude Haiku 5.5",
        provider=ModelProvider.BEDROCK,
        scope=_GLOBAL,
        base_model="claude-haiku-5-5",
        is_default=True,
    ),
    ModelSpec(
        model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        display_name="Claude Haiku 4.5",
        provider=ModelProvider.BEDROCK,
        scope=_GLOBAL,
        base_model="claude-haiku-4-5",
    ),
    ModelSpec(
        model_id="global.anthropic.claude-sonnet-5-5",
        display_name="Claude Sonnet 5.5",
        provider=ModelProvider.BEDROCK,
        scope=_GLOBAL,
        base_model="claude-sonnet-5-5",
    ),
    # 保存済みの議論・インタビューが選んだ Sonnet 5 を同じモデルで呼び続けるため残す
    ModelSpec(
        model_id="global.anthropic.claude-sonnet-5",
        display_name="Claude Sonnet 5",
        provider=ModelProvider.BEDROCK,
        scope=_GLOBAL,
        base_model="claude-sonnet-5",
    ),
    ModelSpec(
        model_id="openai.gpt-5.6-terra",
        display_name="GPT-5.6 Terra",
        provider=ModelProvider.OPENAI_RESPONSES,
        requires_mantle=True,
        scope=_GLOBAL,
        base_model="gpt-5.6-terra",
    ),
    ModelSpec(
        model_id="openai.gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        provider=ModelProvider.OPENAI_RESPONSES,
        requires_mantle=True,
        scope=_GLOBAL,
        base_model="gpt-5.6-luna",
    ),
    ModelSpec(
        model_id="google.gemma-4-31b",
        display_name="Google Gemma 4 31B",
        provider=ModelProvider.OPENAI_RESPONSES,
        requires_mantle=True,
        # AWS公式モデルカード実測: リクエストボディ合計（画像・動画含む）3.5MBが上限。
        # 現行アプリの画像5MB/枚・合計32MB運用より厳しいため、Manager層の合計サイズ検証で使う。
        max_request_bytes=int(3.5 * 1024 * 1024),
        scope=_GLOBAL,
        base_model="gemma-4-31b",
    ),
    # jp（国内の推論プロファイル。Mantle 系は置かない）
    ModelSpec(
        model_id="jp.anthropic.claude-haiku-5-5",
        display_name="Claude Haiku 5.5",
        provider=ModelProvider.BEDROCK,
        scope=_JP,
        base_model="claude-haiku-5-5",
        is_default=True,
    ),
    ModelSpec(
        model_id="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
        display_name="Claude Haiku 4.5",
        provider=ModelProvider.BEDROCK,
        scope=_JP,
        base_model="claude-haiku-4-5",
    ),
)

SUPPORTED_MODELS: dict[str, ModelSpec] = {spec.model_id: spec for spec in _MODELS}


def models_in_scope(scope: InferenceScope | str) -> list[ModelSpec]:
    """The models of one scope in display order (BR4.1).

    Raises:
        ValueError: ``scope`` is not ``global`` or ``jp``.
    """
    target = InferenceScope(scope)
    return [spec for spec in _MODELS if spec.scope == target]


def list_selectable_models(
    enable_additional_models: bool, scope: InferenceScope | str
) -> list[ModelSpec]:
    """UI用。範囲に属するモデルだけを返し、無効時はrequires_mantleのモデルを除外する（BR4.2）。

    configをimportしないため、呼び出し側（Router）がconfig.ENABLE_ADDITIONAL_PERSONA_MODELSと
    config.INFERENCE_SCOPEを渡す。
    """
    return [
        spec
        for spec in models_in_scope(scope)
        if enable_additional_models or not spec.requires_mantle
    ]


def default_selectable_model_id(
    models: list[ModelSpec], preferred_model_id: str
) -> str:
    """The model the setup screen selects first (BR5.1).

    ``preferred_model_id`` (``config.AGENT_MODEL_ID``) when it is in ``models``, otherwise the
    list's default (or its first model).
    """
    if any(spec.model_id == preferred_model_id for spec in models):
        return preferred_model_id
    defaults = [spec for spec in models if spec.is_default]
    return (defaults or models)[0].model_id


def resolve_model_spec(
    model_id: str | None, scope: InferenceScope | str, default_model_id: str
) -> ModelSpec:
    """The spec of the model that is actually called for ``model_id`` (BR4.4, BR4.5).

    1. ``None`` or empty means ``default_model_id``.
    2. A model of the current scope is itself.
    3. ``default_model_id`` is used as is, even from another scope or outside the table.
    4. A model of another scope is read as the same model (``base_model``) of this scope.
    5. Anything else (no same model in this scope, unknown IDs) is ``default_model_id``.

    Raises:
        ValueError: ``scope`` is not ``global`` or ``jp``.
    """
    target_scope = InferenceScope(scope)
    requested = model_id or default_model_id
    spec = SUPPORTED_MODELS.get(requested)
    if spec is not None and spec.scope == target_scope:
        return spec
    if requested == default_model_id:
        return _default_spec(default_model_id, target_scope)
    if spec is not None:
        counterpart = _same_model_in_scope(spec.base_model, target_scope)
        if counterpart is not None:
            return counterpart
    return _default_spec(default_model_id, target_scope)


def _same_model_in_scope(base_model: str, scope: InferenceScope) -> ModelSpec | None:
    for spec in _MODELS:
        if spec.scope == scope and spec.base_model == base_model:
            return spec
    return None


def _default_spec(default_model_id: str, scope: InferenceScope) -> ModelSpec:
    """The environment's default model: its table spec, or a plain Bedrock spec (BR4.5)."""
    spec = SUPPORTED_MODELS.get(default_model_id)
    if spec is not None:
        return spec
    return ModelSpec(
        model_id=default_model_id,
        display_name=default_model_id,
        provider=ModelProvider.BEDROCK,
        scope=scope,
        base_model=default_model_id,
    )


def display_name_for(model_id: str | None) -> str:
    """表示用。未知/Noneでも例外を投げず生の値へフォールバックする（BR5.2）。"""
    if model_id is None:
        model_id = SCOPE_DEFAULTS[InferenceScope.GLOBAL].agent_model_id
    spec = SUPPORTED_MODELS.get(model_id)
    return spec.display_name if spec is not None else model_id


def resolve_call_region(spec: ModelSpec, deploy_region: str) -> str:
    """モデル呼び出しに使うリージョンを決定する。

    BEDROCKモデル（Claude系）はクロスリージョン推論プロファイル経由なのでデプロイ先に
    そのまま追従する。Mantle系（requires_mantle=True）はMantle対応リージョンが限られるため、
    デプロイ先が非対応（例: 東京）ならMANTLE_FALLBACK_REGIONへ丸める。
    """
    if not spec.requires_mantle:
        return deploy_region
    if deploy_region in MANTLE_SUPPORTED_REGIONS:
        return deploy_region
    return MANTLE_FALLBACK_REGION
