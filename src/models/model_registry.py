"""ペルソナ議論・インタビューで選択可能なAIモデルのレジストリ。

モデルによってStrandsのプロバイダクラス自体が異なる（BedrockModel経由のConverse呼び出しと、
OpenAIResponsesModel経由のBedrock Mantle呼び出し）ため、model_idだけでなく呼び出し経路の情報を
ここで一元管理する。標準ライブラリのみを使い他層に依存しない（architecture.md Models層規約）。
"""

from dataclasses import dataclass
from enum import Enum


class ModelProvider(str, Enum):
    """モデルの呼び出し経路。"""

    BEDROCK = "bedrock"  # BedrockModel / Converse / SigV4
    OPENAI_RESPONSES = (
        "openai_responses"  # OpenAIResponsesModel / Mantle / bedrock_mantle_config
    )


@dataclass(frozen=True)
class ModelSpec:
    """選択可能な1モデルの仕様。"""

    model_id: str
    display_name: str
    provider: ModelProvider
    region: str
    requires_mantle: bool = False
    # 能力メタデータ。Manager層の入力サイズ検証で実際に使う（保持のみではない）。
    max_image_bytes: int | None = None
    max_request_bytes: int | None = None
    supports_pdf: bool = True


# config への依存（循環）を避けるため文字列で持つ。config.AGENT_MODEL_ID と一致することを
# tests/unit/test_model_registry.py で担保する。
DEFAULT_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

_DEFAULT_REGION = "us-east-1"

SUPPORTED_MODELS: dict[str, ModelSpec] = {
    DEFAULT_MODEL_ID: ModelSpec(
        model_id=DEFAULT_MODEL_ID,
        display_name="Claude Haiku 4.5（既定）",
        provider=ModelProvider.BEDROCK,
        region=_DEFAULT_REGION,
    ),
    "global.anthropic.claude-sonnet-5": ModelSpec(
        model_id="global.anthropic.claude-sonnet-5",
        display_name="Claude Sonnet 5",
        provider=ModelProvider.BEDROCK,
        region=_DEFAULT_REGION,
    ),
    "openai.gpt-5.6-terra": ModelSpec(
        model_id="openai.gpt-5.6-terra",
        display_name="GPT-5.6 Terra",
        provider=ModelProvider.OPENAI_RESPONSES,
        region=_DEFAULT_REGION,
        requires_mantle=True,
    ),
    "openai.gpt-5.6-luna": ModelSpec(
        model_id="openai.gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        provider=ModelProvider.OPENAI_RESPONSES,
        region=_DEFAULT_REGION,
        requires_mantle=True,
    ),
    "google.gemma-4-31b": ModelSpec(
        model_id="google.gemma-4-31b",
        display_name="Google Gemma 4 31B",
        provider=ModelProvider.OPENAI_RESPONSES,
        region=_DEFAULT_REGION,
        requires_mantle=True,
        # AWS公式モデルカード実測: リクエストボディ合計（画像・動画含む）3.5MBが上限。
        # 現行アプリの画像5MB/枚・合計32MB運用より厳しいため、Manager層の合計サイズ検証で使う。
        max_request_bytes=int(3.5 * 1024 * 1024),
    ),
}


def get_model_spec(model_id: str | None) -> ModelSpec:
    """呼び出し経路の解決用。None/未知IDは既定モデルへ丸める。"""
    if model_id is None:
        return SUPPORTED_MODELS[DEFAULT_MODEL_ID]
    return SUPPORTED_MODELS.get(model_id, SUPPORTED_MODELS[DEFAULT_MODEL_ID])


def is_supported(model_id: str) -> bool:
    """バリデーション用の厳密判定。未知IDはFalse。"""
    return model_id in SUPPORTED_MODELS


def list_selectable_models(enable_mantle: bool) -> list[ModelSpec]:
    """UI用。enable_mantle=Falseのときrequires_mantleのモデルを除外する。

    configをimportしないため、呼び出し側（Manager/Router）がconfig.ENABLE_MANTLE_MODELSを渡す。
    """
    return [
        spec
        for spec in SUPPORTED_MODELS.values()
        if enable_mantle or not spec.requires_mantle
    ]


def display_name_for(model_id: str | None) -> str:
    """表示用。未知/Noneでも例外を投げず生の値へフォールバックする。"""
    if model_id is None:
        return SUPPORTED_MODELS[DEFAULT_MODEL_ID].display_name
    spec = SUPPORTED_MODELS.get(model_id)
    return spec.display_name if spec is not None else model_id
