"""
モデル選択バリデーションの共通ユーティリティ

AgentDiscussionManager, InterviewManager から共通利用される
「選択されたmodel_idが利用可能か検証する」「未選択ペルソナに環境既定モデルを
補完する」「ドキュメント合計サイズが選択モデルの上限を超えないか検証する」ロジック。
"""

from typing import Callable, Dict, Iterable, Mapping, Optional, Type

from ...config import config
from ...models.errors import CodedError, ErrorCode
from ...models.model_registry import get_model_spec, is_supported


def validate_model_selection(
    model_ids: Optional[Mapping[str, Optional[str]]],
    error_cls: Type[CodedError],
    unsupported_code: ErrorCode,
    disabled_code: ErrorCode,
    unsupported_message: Callable[[str, str], str] = (
        lambda identifier,
        model_id: f"unsupported model_id {model_id!r} for {identifier!r}"
    ),
    disabled_message: Callable[[str], str] = (
        lambda model_id: f"model {model_id!r} requires Mantle but "
        "ENABLE_ADDITIONAL_PERSONA_MODELS is disabled"
    ),
) -> None:
    """選択されたmodel_idが利用可能か検証する。

    未対応のmodel_idは unsupported_code、追加ペルソナベースモデルだが
    ENABLE_ADDITIONAL_PERSONA_MODELS無効時は disabled_code で例外を送出する。

    Args:
        model_ids: 検証対象の {識別子: model_id} マップ（Noneは既定モデルとしてスキップ）
        error_cls: 送出する例外クラス（呼び出し元のドメイン例外）
        unsupported_code: 未対応model_id時のErrorCode
        disabled_code: 追加ペルソナベースモデル無効時のErrorCode
        unsupported_message: (identifier, model_id) -> ログ用メッセージ
        disabled_message: model_id -> ログ用メッセージ
    """
    if not model_ids:
        return
    for identifier, model_id in model_ids.items():
        if model_id is None:
            continue
        if not is_supported(model_id):
            raise error_cls(
                unsupported_message(identifier, model_id),
                code=unsupported_code,
            )
        spec = get_model_spec(model_id)
        if spec.requires_mantle and not config.ENABLE_ADDITIONAL_PERSONA_MODELS:
            raise error_cls(
                disabled_message(model_id),
                code=disabled_code,
            )


def resolve_effective_persona_models(
    persona_ids: Iterable[str],
    persona_models: Optional[Mapping[str, Optional[str]]],
) -> Dict[str, str]:
    """未選択のペルソナにはconfig.AGENT_MODEL_ID（環境既定モデル）を補完する。

    環境既定モデル（config.AGENT_MODEL_ID）はGemma4等のMantle系モデルに設定されうるため、
    添付種別・サイズ検証は「明示的に選ばれたモデル」だけでなく実際に呼び出されるモデルを
    対象にする必要がある（persona_models=Noneのまま検証をスキップすると、環境既定を
    Mantle系にした運用でサイズ・種別制限を回避できてしまう）。

    Args:
        persona_ids: 対象ペルソナID群（discussionはpersona_agent、interviewは
            session.participantsから呼び出し元がpersona_idのリストに変換して渡す）
        persona_models: persona_id -> model_id のマップ（省略時は既定モデル）

    Returns:
        persona_modelsの内容を保持しつつ、persona_idsに含まれる未選択分を補完したマップ
        （persona_modelsに残る他のキーは変更しない。呼び出し元が明示的に選んだ値を
        誤って落とさないため、persona_idsだけから作り直すのではなくコピーに補完する）
    """
    resolved: Dict[str, str] = dict(persona_models or {})  # type: ignore[arg-type]
    for persona_id in persona_ids:
        if not resolved.get(persona_id):
            resolved[persona_id] = config.AGENT_MODEL_ID
    return resolved


def validate_document_size_for_models(
    total_size: int,
    persona_models: Optional[Mapping[str, Optional[str]]],
    error_cls: Type[CodedError],
    too_large_code: ErrorCode,
    message: Callable[[str, int, int], str] = (
        lambda model_id, total, effective_max: (
            f"document total size {total} exceeds model {model_id!r} "
            f"limit (effective max {effective_max})"
        )
    ),
) -> None:
    """選択モデルのmax_request_bytes（Gemma4等）に対しドキュメント合計サイズを検証する。

    base64化によるオーバーヘッド（概算4/3倍）を見込んだ実効上限で判定する。

    Args:
        total_size: 検証対象のドキュメント合計サイズ（バイト。合計方法は呼び出し元に委ねる。
            discussionは新規添付のみ、interviewはセッション既存分も合算する）
        persona_models: 検証対象の {persona_id: model_id} マップ
        error_cls: 送出する例外クラス（呼び出し元のドメイン例外）
        too_large_code: サイズ超過時のErrorCode
        message: (model_id, total_size, effective_max) -> ログ用メッセージ
    """
    if not persona_models or total_size == 0:
        return

    for model_id in set(persona_models.values()):
        if model_id is None:
            continue
        spec = get_model_spec(model_id)
        if spec.max_request_bytes is None:
            continue
        effective_max = int(spec.max_request_bytes * 3 / 4)
        if total_size > effective_max:
            raise error_cls(
                message(model_id, total_size, effective_max),
                code=too_large_code,
                context={"max_size_mb": spec.max_request_bytes / (1024 * 1024)},
            )
