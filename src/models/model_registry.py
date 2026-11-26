"""Registry of AI models selectable for persona discussions and interviews.

The Strands provider class itself differs per model (a Converse call through
BedrockModel, or a Bedrock Mantle call through OpenAIResponsesModel), so the
call-path information is managed here in one place together with model_id.
Uses the standard library only and depends on no other layer (Models layer rule
in architecture.md).

For each inference scope (global / jp) it holds the table of selectable models
and the default model IDs. It does not read config: callers (Manager, Service,
Router) pass config.INFERENCE_SCOPE and config.AGENT_MODEL_ID.
"""

from dataclasses import dataclass
from enum import Enum


class ModelProvider(str, Enum):
    """How a model is called."""

    BEDROCK = "bedrock"  # BedrockModel / Converse / SigV4
    OPENAI_RESPONSES = (
        "openai_responses"  # OpenAIResponsesModel / Mantle / bedrock_mantle_config
    )


class InferenceScope(str, Enum):
    """Scope inference may be sent to (CDK inferenceScope, env var INFERENCE_SCOPE)."""

    GLOBAL = "global"
    JP = "jp"


@dataclass(frozen=True)
class ModelSpec:
    """Spec of one selectable model."""

    model_id: str
    display_name: str
    provider: ModelProvider
    requires_mantle: bool = False
    # Capability metadata. Actually used by the Manager layer's input size validation (not just stored).
    max_image_bytes: int | None = None
    max_request_bytes: int | None = None
    supports_pdf: bool = True
    # The scope it belongs to (one ID belongs to exactly one scope) and the key that links the same model across scopes
    scope: InferenceScope = InferenceScope.GLOBAL
    base_model: str = ""
    # Exactly one per scope (same as agent_model_id in SCOPE_DEFAULTS)
    is_default: bool = False


@dataclass(frozen=True)
class ScopeDefaults:
    """The three default model IDs per scope (same values as SCOPE_DEFAULTS in cdk/lib/deployment-profile.ts)."""

    bedrock_model_id: str  # Persona generation, insights, reports (BEDROCK_MODEL_ID)
    agent_model_id: str  # Default for discussions and interviews (AGENT_MODEL_ID)
    survey_model_id: str  # Survey answer generation (SURVEY_MODEL_ID)


# Surveys use Haiku 5.5 through Step Functions on-demand inference. The values
# are pinned by both pytest and jest so they stay identical.
SCOPE_DEFAULTS: dict[InferenceScope, ScopeDefaults] = {
    InferenceScope.GLOBAL: ScopeDefaults(
        bedrock_model_id="global.anthropic.claude-sonnet-5-5",
        agent_model_id="global.anthropic.claude-haiku-5-5",
        survey_model_id="global.anthropic.claude-haiku-5-5",
    ),
    InferenceScope.JP: ScopeDefaults(
        bedrock_model_id="jp.anthropic.claude-haiku-5-5",
        agent_model_id="jp.anthropic.claude-haiku-5-5",
        survey_model_id="jp.anthropic.claude-haiku-5-5",
    ),
}

# Regions supported by Bedrock Mantle (shared by GPT-5.6 Terra/Luna and Gemma 4 31B).
# If the deployment region (config.AWS_REGION) is not in this set, it is rounded to MANTLE_FALLBACK_REGION.
MANTLE_SUPPORTED_REGIONS: frozenset[str] = frozenset(
    {"us-east-1", "us-east-2", "us-west-2"}
)
MANTLE_FALLBACK_REGION = "us-east-1"

_GLOBAL = InferenceScope.GLOBAL
_JP = InferenceScope.JP

# The table order is the settings screen order (the default comes first in each scope)
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
    # Kept so saved discussions and interviews that chose Sonnet 5 keep calling the same model
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
        # Measured from the AWS official model card: the total request body (images and video
        # included) is capped at 3.5MB. Stricter than the app's 5MB per image / 32MB total,
        # so the Manager layer's total size validation uses it.
        max_request_bytes=int(3.5 * 1024 * 1024),
        scope=_GLOBAL,
        base_model="gemma-4-31b",
    ),
    # jp (domestic inference profiles; no Mantle models)
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
    """The models of one scope in display order.

    Raises:
        ValueError: ``scope`` is not ``global`` or ``jp``.
    """
    target = InferenceScope(scope)
    return [spec for spec in _MODELS if spec.scope == target]


def list_selectable_models(
    enable_additional_models: bool, scope: InferenceScope | str
) -> list[ModelSpec]:
    """For the UI: return only the models of the scope, excluding requires_mantle models when disabled.

    Does not import config, so the caller (Router) passes
    config.ENABLE_ADDITIONAL_PERSONA_MODELS and config.INFERENCE_SCOPE.
    """
    return [
        spec
        for spec in models_in_scope(scope)
        if enable_additional_models or not spec.requires_mantle
    ]


def default_selectable_model_id(
    models: list[ModelSpec], preferred_model_id: str
) -> str:
    """The model the setup screen selects first.

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
    """The spec of the model that is actually called for ``model_id``.

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
    """The environment's default model: its table spec, or a plain Bedrock spec."""
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
    """For display. Never raises for unknown/None; falls back to the raw value."""
    if model_id is None:
        model_id = SCOPE_DEFAULTS[InferenceScope.GLOBAL].agent_model_id
    spec = SUPPORTED_MODELS.get(model_id)
    return spec.display_name if spec is not None else model_id


def resolve_call_region(spec: ModelSpec, deploy_region: str) -> str:
    """Decide the region used for model calls.

    BEDROCK models (Claude family) go through cross-region inference profiles,
    so they simply follow the deployment region. Mantle models
    (requires_mantle=True) are available in few regions, so if the deployment
    region is unsupported (e.g. Tokyo) it is rounded to MANTLE_FALLBACK_REGION.
    """
    if not spec.requires_mantle:
        return deploy_region
    if deploy_region in MANTLE_SUPPORTED_REGIONS:
        return deploy_region
    return MANTLE_FALLBACK_REGION
