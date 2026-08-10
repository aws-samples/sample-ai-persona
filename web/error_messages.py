"""
User-facing error wording catalog (presentation layer).

Routers must resolve wording through :func:`user_message_for` and must never
write ``str(e)`` into a response. That keeps diagnostic detail in the logs and
makes ``py/stack-trace-exposure`` structurally impossible; the rule is enforced
mechanically by ``tests/api/test_error_exposure.py``.

The catalog is module-private on purpose: keeping every lookup behind
:func:`user_message_for` means adding a locale dimension later stays confined to
this file, with no change at the call sites.

:func:`toast_response` renders ``ErrorKind.TRANSIENT`` errors as a toast instead
of a body swap, so that the user's input survives a retryable failure
(Issue #117).
"""

import json
import logging
from typing import TypeVar

from fastapi import Response

from src.models.errors import CodedError, ErrorCode, ErrorKind

logger = logging.getLogger(__name__)

#: ``mark_renderable`` は受け取った応答型をそのまま返す（TemplateResponse を
#: 渡した呼び出し元が Response に狭められないようにするため）。
_ResponseT = TypeVar("_ResponseT", bound=Response)

# Field key -> display label. Managers put the stable key in
# ``context["field"]`` so that only this file holds Japanese labels.
_FIELD_LABELS: dict[str, str] = {
    "name": "ペルソナ名",
    "occupation": "職業",
    "background": "背景",
    "city": "居住都市",
    "values": "価値観",
    "pain_points": "課題・悩み",
    "goals": "目標・願望",
    "tags": "タグ",
    "topic_name": "トピック名",
    "topic_content": "内容",
}

# ErrorCode -> user-facing wording. Values are ``str.format`` templates whose
# placeholders are filled from ``CodedError.context``.
_CATALOG: dict[ErrorCode, str] = {
    ErrorCode.NETWORK_ERROR: (
        "ネットワーク接続エラーが発生しました。接続を確認してください。"
    ),
    ErrorCode.JOB_NOT_FOUND: "ジョブが見つかりません",
    ErrorCode.GENERATION_CAPACITY_EXCEEDED: (
        "生成するデータ量が大きすぎて処理しきれませんでした。"
        "ペルソナ生成数を減らすか、アップロードするファイルを小さくして再度お試しください。"
    ),
    ErrorCode.REPORT_CAPACITY_EXCEEDED: (
        "分析対象のデータ量が大きすぎてレポートを生成しきれませんでした。"
        "対象を絞るか、議論ログを短くして再度お試しください。"
    ),
    ErrorCode.GENERATION_PERSONA_COUNT_INVALID: (
        "生成するペルソナ数は1〜10の範囲で指定してください"
    ),
    ErrorCode.GENERATION_DATA_DESCRIPTION_REQUIRED: "分析の切り口を入力してください",
    ErrorCode.GENERATION_FILES_REQUIRED: "ファイルが選択されていません",
    ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED: (
        "生成されたペルソナの一時データが期限切れです。お手数ですが再度生成してください。"
    ),
    ErrorCode.GENERATION_OPERATION_FAILED: (
        "ペルソナ生成の処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.FILE_TOO_LARGE: (
        "ファイルサイズが制限を超えています。最大サイズ: {max_size_mb:.1f}MB"
    ),
    ErrorCode.FILE_FORMAT_NOT_ALLOWED: (
        "許可されていないファイル形式です。対応形式: {allowed_formats}"
    ),
    ErrorCode.FILE_MIME_UNSUPPORTED: "サポートされていないファイル種別です。",
    ErrorCode.FILE_EMPTY: "ファイルが空です。",
    ErrorCode.INTERVIEW_FILE_CONTENT_TOO_SHORT: (
        "ファイル内容が短すぎます。"
        "インタビューなどの内容を含むテキストファイルをアップロードしてください。"
    ),
    ErrorCode.FILE_NAME_INVALID: "ファイル名に不正な文字が含まれています。",
    ErrorCode.FILE_NAME_TOO_LONG: "ファイル名が長すぎます。",
    ErrorCode.FILE_HIDDEN_NOT_ALLOWED: "隠しファイルはアップロードできません。",
    ErrorCode.FILE_BINARY_NOT_ALLOWED: "バイナリファイルはアップロードできません。",
    ErrorCode.FILE_OPERATION_FAILED: (
        "ファイルの処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    # --- アンケート ---
    # ID は画面に出さずログにのみ残す（診断情報とユーザー向け案内の分離）。
    ErrorCode.SURVEY_NOT_FOUND: "アンケートが見つかりません",
    ErrorCode.SURVEY_RESULT_NOT_READY: "アンケート結果がまだ生成されていません",
    ErrorCode.SURVEY_TEMPLATE_NOT_FOUND: "テンプレートが見つかりません",
    ErrorCode.SURVEY_TEMPLATE_NAME_BLANK: (
        "テンプレート名は空白のみでは登録できません"
    ),
    ErrorCode.SURVEY_TEMPLATE_NO_QUESTIONS: "質問が1つも含まれていません",
    ErrorCode.SURVEY_TEMPLATE_QUESTIONS_INVALID: "質問データの形式が不正です",
    ErrorCode.SURVEY_TEMPLATE_TOO_FEW_OPTIONS: (
        "選択式質問「{question_text}」には2つ以上の選択肢が必要です"
    ),
    ErrorCode.SURVEY_TEMPLATE_TOO_MANY_IMAGES: "画像は1枚まで添付できます",
    ErrorCode.SURVEY_TEMPLATE_IMAGE_NAME_MISSING: "画像には名前を設定してください",
    ErrorCode.SURVEY_PERSONA_COUNT_INVALID: "ペルソナ数は整数で入力してください",
    ErrorCode.SURVEY_TARGET_COUNT_TOO_LOW: (
        "対象ペルソナ数は{min_count}以上で指定してください"
    ),
    ErrorCode.SURVEY_TARGET_COUNT_TOO_HIGH: "対象ペルソナ数は{max_count}人までです",
    ErrorCode.SURVEY_TARGET_COUNT_TOO_HIGH_WITH_IMAGES: (
        "画像付きアンケートの場合、対象ペルソナ数は{max_count}人までです"
    ),
    ErrorCode.SURVEY_AVAILABLE_PERSONAS_TOO_FEW: (
        "条件に合致するペルソナが{available_count}人しかいません。"
        "アンケートの実行には{min_count}人以上が必要です。フィルタ条件を緩めてください。"
    ),
    ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS: (
        "データ件数が{row_count}件しかありません。"
        "アンケートの実行には{min_rows}件以上のデータが必要です。"
        "{min_rows}件以上のデータセットを選択するか、データを追加してください。"
    ),
    ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED: (
        "Nemotronデータセットがまだダウンロードされていません。"
        "アンケート調査 > ペルソナデータ設定からデータセットをダウンロードしてください。"
    ),
    ErrorCode.SURVEY_DATASET_CSV_UNREADABLE: (
        "CSVファイルを読み取れませんでした。"
        "UTF-8のカンマ区切り、ヘッダー行ありの形式で保存してください。"
    ),
    ErrorCode.SURVEY_DATASET_CSV_EMPTY: "CSVにデータ行がありません。",
    ErrorCode.SURVEY_DATASET_NO_TEXT_COLUMN: (
        "テキスト型のカラムが見つかりません。"
        "「ペルソナ概要」にマッピングするカラムを指定してください。"
    ),
    ErrorCode.SURVEY_BATCH_ROLE_NOT_CONFIGURED: (
        "バッチ推論の実行設定が完了していません。管理者に連絡してください。"
    ),
    ErrorCode.SURVEY_BATCH_JOB_FAILED: (
        "バッチ推論の実行に失敗しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.SURVEY_BATCH_JOB_TIMED_OUT: (
        "バッチ推論が制限時間内に完了しませんでした。"
        "対象ペルソナ数を減らして再度お試しください。"
    ),
    ErrorCode.SURVEY_AI_UNAVAILABLE: (
        "AIによる生成機能が利用できません。設定を確認してください。"
    ),
    ErrorCode.SURVEY_AI_NO_QUESTIONS: "AIが有効な設問を生成できませんでした",
    ErrorCode.SURVEY_AI_CONVERSATION_INVALID: "会話履歴の形式が正しくありません",
    ErrorCode.SURVEY_AI_CONVERSATION_TOO_LONG: (
        "会話履歴が長すぎます（最大 {max_messages} 件）"
    ),
    ErrorCode.SURVEY_AI_MESSAGE_TOO_LONG: (
        "1メッセージは{max_length}文字以内にしてください"
    ),
    ErrorCode.SURVEY_OPERATION_FAILED: (
        "アンケートの処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.SURVEY_EXECUTION_FAILED: (
        "アンケートの実行中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.SURVEY_REPORT_GENERATION_FAILED: (
        "レポート生成に失敗しました。再試行してください。"
    ),
    # --- 議論 ---
    ErrorCode.DISCUSSION_INTERVIEW_MODE_UNSUPPORTED: (
        "インタビューモードは別のエンドポイントで処理されます"
    ),
    ErrorCode.DISCUSSION_PERSONAS_REQUIRED: "議論参加ペルソナが指定されていません",
    ErrorCode.DISCUSSION_TOO_FEW_PERSONAS: (
        "議論には最低{min_personas}つのペルソナが必要です"
    ),
    ErrorCode.DISCUSSION_TOO_MANY_PERSONAS: (
        "議論参加ペルソナは最大{max_personas}つまでです"
    ),
    ErrorCode.DISCUSSION_PERSONA_INVALID: "選択したペルソナの情報が正しくありません",
    ErrorCode.DISCUSSION_PERSONA_DUPLICATED: "重複したペルソナが含まれています",
    ErrorCode.DISCUSSION_TOPIC_REQUIRED: "議論トピックが空です",
    ErrorCode.DISCUSSION_TOPIC_TOO_SHORT: (
        "議論トピックが短すぎます。{min_length}文字以上で入力してください"
    ),
    ErrorCode.DISCUSSION_TOPIC_TOO_LONG: (
        "議論トピックが長すぎます。{max_length}文字以内で入力してください"
    ),
    ErrorCode.DISCUSSION_DOCUMENTS_TOO_LARGE: (
        "ドキュメントの合計サイズが制限を超えています（最大{max_size_mb:.0f}MB）"
    ),
    ErrorCode.DISCUSSION_NOT_FOUND: "議論が見つかりません",
    ErrorCode.DISCUSSION_ROUNDS_TOO_FEW: (
        "ラウンド数は{min_rounds}以上で指定してください"
    ),
    ErrorCode.DISCUSSION_ROUNDS_TOO_MANY: (
        "ラウンド数は{max_rounds}以下で指定してください"
    ),
    ErrorCode.DISCUSSION_AGENT_SETUP_FAILED: (
        "議論エージェントの準備に失敗しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.DISCUSSION_OPERATION_FAILED: (
        "議論の処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.DISCUSSION_INVALID: "議論の内容が正しくありません",
    ErrorCode.DISCUSSION_ID_INVALID: "議論IDが無効です",
    ErrorCode.DISCUSSION_DOCUMENT_NOT_FOUND: "ドキュメントが見つかりません",
    ErrorCode.DISCUSSION_RESULT_INVALID: (
        "議論結果の生成に失敗しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.DISCUSSION_INSIGHT_GENERATION_FAILED: (
        "インサイトの生成に失敗しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.DISCUSSION_MEMORY_MODE_INVALID: (
        "無効な記憶モードです。議論の設定を確認してください"
    ),
    ErrorCode.DISCUSSION_MODEL_UNSUPPORTED: (
        "選択されたモデルは利用できません。設定を確認してください"
    ),
    ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED: (
        "選択されたモデルは現在無効化されています。管理者に設定の有効化を依頼してください"
    ),
    ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE: (
        "選択したモデルの入力サイズ上限（最大{max_size_mb:.1f}MB）を超えています。"
        "モデルを変更するか、添付ドキュメントを減らしてください"
    ),
    ErrorCode.DISCUSSION_MODEL_DOCUMENT_UNSUPPORTED: (
        "選択したモデルは現在PDF等のドキュメント添付に対応していません（画像は対応済み）。"
        "Claude系モデルに変更するか、添付を外してください"
    ),
    # --- 議論レポート ---
    ErrorCode.REPORT_NOT_FOUND: "レポートが見つかりません",
    ErrorCode.REPORT_LIMIT_REACHED: (
        "レポートは最大{max_reports}件まで保存できます。"
        "不要なレポートを削除してください。"
    ),
    ErrorCode.REPORT_OPERATION_FAILED: (
        "レポートの処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    # --- ペルソナデータのセグメント抽出（DWH） ---
    ErrorCode.SEGMENT_CONDITION_REQUIRED: "抽出条件を入力してください",
    ErrorCode.SEGMENT_ROW_COUNT_TOO_LOW: (
        "抽出件数が少なすぎます（{row_count}件）。最低{min_rows}件のデータが必要です。"
    ),
    ErrorCode.SEGMENT_ROW_COUNT_TOO_HIGH: (
        "抽出件数が多すぎます（{row_count}件）。最大{max_rows}件までです。"
    ),
    ErrorCode.SEGMENT_CSV_URL_MISSING: "CSVエクスポートURLを取得できませんでした。",
    ErrorCode.DATA_AGENT_NOT_CONFIGURED: (
        "データ分析エージェントの接続設定がされていません。"
        "設定画面から Runtime ARN を設定してください"
    ),
    ErrorCode.DATA_AGENT_CONNECTION_FAILED: (
        "データ分析エージェントへの接続テストに失敗しました。"
        "時間をおいて再度お試しください。"
    ),
    ErrorCode.DATA_AGENT_DOWNLOAD_URL_REJECTED: (
        "データのダウンロードに失敗しました。時間をおいて再度お試しください。"
    ),
    # --- インタビュー ---
    ErrorCode.INTERVIEW_PERSONAS_REQUIRED: (
        "インタビューには最低1つのペルソナが必要です"
    ),
    ErrorCode.INTERVIEW_TOO_MANY_PERSONAS: (
        "インタビューには最大{max_personas}つのペルソナまで参加できます"
    ),
    ErrorCode.INTERVIEW_PERSONA_INVALID: (
        "選択したペルソナの情報が不完全です。ペルソナを選び直してください"
    ),
    ErrorCode.INTERVIEW_USER_ID_INVALID: "有効なユーザーIDが必要です",
    ErrorCode.INTERVIEW_MEMORY_MODE_INVALID: (
        "無効な記憶モードです。設定を確認してください"
    ),
    ErrorCode.INTERVIEW_MESSAGE_REQUIRED: "メッセージを入力してください",
    ErrorCode.INTERVIEW_MESSAGE_TOO_LONG: (
        "メッセージが長すぎます（最大{max_length}文字）"
    ),
    ErrorCode.INTERVIEW_SESSION_NAME_REQUIRED: "セッション名を入力してください",
    ErrorCode.INTERVIEW_SESSION_NAME_TOO_LONG: (
        "セッション名が長すぎます（最大{max_length}文字）"
    ),
    ErrorCode.INTERVIEW_SESSION_NOT_FOUND: "インタビューセッションが見つかりません",
    ErrorCode.INTERVIEW_SESSION_ALREADY_SAVED: ("このセッションは既に保存されています"),
    ErrorCode.INTERVIEW_SESSION_AGENTS_MISSING: (
        "セッションが見つかりません。ページを再読み込みしてください"
    ),
    ErrorCode.INTERVIEW_SAVE_PRECONDITION_NOT_MET: (
        "保存するにはユーザーとペルソナ双方のメッセージが必要です"
    ),
    ErrorCode.INTERVIEW_NO_MESSAGES: "保存するメッセージがありません",
    ErrorCode.INTERVIEW_NO_USER_MESSAGES: "ユーザーメッセージが含まれていません",
    ErrorCode.INTERVIEW_NO_PERSONA_RESPONSES: "ペルソナの応答が含まれていません",
    ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED: (
        "インタビューの処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.INTERVIEW_AGENT_SETUP_FAILED: (
        "AIエージェントの初期化に失敗しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.INTERVIEW_AGENT_UNAVAILABLE: (
        "AIエージェントとの通信に失敗しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.INTERVIEW_SAVE_FAILED: (
        "セッションの保存に失敗しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.INTERVIEW_MODEL_UNSUPPORTED: (
        "選択されたモデルはインタビューで利用できません。設定を確認してください"
    ),
    ErrorCode.INTERVIEW_MODEL_ADDITIONAL_MODELS_DISABLED: (
        "選択されたモデルは現在無効化されています。"
        "管理者にインタビュー用モデル設定の有効化を依頼してください"
    ),
    ErrorCode.INTERVIEW_MODEL_DOCUMENT_UNSUPPORTED: (
        "選択したモデルは現在インタビューでのPDF等のドキュメント添付に対応していません"
        "（画像は対応済み）。Claude系モデルに変更するか、添付を外してください"
    ),
    # --- ペルソナ ---
    # {field} は _FIELD_LABELS で表示名に解決される。
    ErrorCode.PERSONA_FIELD_REQUIRED: "{field}が設定されていません",
    ErrorCode.PERSONA_FIELD_TOO_LONG: "{field}は{max_length}文字以内で設定してください",
    ErrorCode.PERSONA_LIST_EMPTY: "{field}が1つも設定されていません",
    ErrorCode.PERSONA_SELECTION_REQUIRED: "保存するペルソナを選択してください",
    ErrorCode.PERSONA_LIST_HAS_EMPTY_ITEM: "{field}に空の項目があります",
    ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS: "{field}は{max_items}項目以内で設定してください",
    ErrorCode.PERSONA_LIST_ITEM_TOO_LONG: (
        "{field}は1個あたり{max_length}文字以内で設定してください"
    ),
    ErrorCode.PERSONA_AGE_OUT_OF_RANGE: (
        "年齢は{min_age}から{max_age}の範囲で設定してください"
    ),
    ErrorCode.PERSONA_GENDER_INVALID: (
        "性別は {allowed_genders} のいずれかで設定してください"
    ),
    ErrorCode.PERSONA_COUNTRY_INVALID: (
        "国はISO 3166-1 alpha-2の実在する国コードで設定してください"
    ),
    ErrorCode.PERSONA_TAG_COMMA_NOT_ALLOWED: "タグにカンマ（,）は使用できません",
    ErrorCode.PERSONA_INVALID: "ペルソナの内容が正しくありません",
    ErrorCode.PERSONA_ID_INVALID: "ペルソナIDが無効です",
    ErrorCode.PERSONA_NOT_FOUND: "ペルソナが見つかりません",
    ErrorCode.PERSONA_UPDATE_FAILED: "ペルソナの更新に失敗しました",
    ErrorCode.PERSONA_OPERATION_FAILED: (
        "ペルソナの処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    ErrorCode.DATASET_COLUMN_NOT_FOUND: (
        "カラム「{column}」はデータセットに存在しません"
    ),
    ErrorCode.DATASET_NOT_FOUND: "データセットが見つかりません",
    ErrorCode.DATASET_BINDING_NOT_FOUND: "紐付け情報が見つかりません",
    ErrorCode.DATASET_OPERATION_FAILED: (
        "データセットの処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    # --- 長期記憶 ---
    ErrorCode.MEMORY_TOPIC_NAME_REQUIRED: "トピック名を入力してください",
    ErrorCode.MEMORY_CONTENT_REQUIRED: "内容を入力してください",
    ErrorCode.MEMORY_TOPIC_NAME_TOO_LONG: (
        "トピック名は{max_length}文字以内で設定してください"
    ),
    ErrorCode.MEMORY_CONTENT_TOO_LONG: "内容は{max_length}文字以内で設定してください",
    ErrorCode.MEMORY_FEATURE_DISABLED: "長期記憶機能が無効です",
    ErrorCode.MEMORY_STRATEGY_NOT_CONFIGURED: (
        "Semantic記憶戦略が設定されていません。"
        "SEMANTIC_MEMORY_STRATEGY_IDを設定してください。"
    ),
    ErrorCode.MEMORY_ALREADY_DELETED: (
        "記憶が見つかりません。既に削除されている可能性があります。"
    ),
    ErrorCode.MEMORY_SERVICE_UNAVAILABLE: "記憶サービスへの接続に失敗しました。",
    ErrorCode.MEMORY_OPERATION_FAILED: (
        "記憶の処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
    # --- Service層（Manager層が必ず自ドメイン例外に変換するため到達しない） ---
    ErrorCode.AGENT_SDK_UNAVAILABLE: "エージェントSDKが利用できません",
    ErrorCode.AGENT_MODEL_ADDITIONAL_MODELS_DISABLED: "選択されたモデルは現在無効化されています",
    ErrorCode.AGENT_INITIALIZATION_FAILED: "エージェントの初期化に失敗しました",
    ErrorCode.AGENT_COMMUNICATION_FAILED: "エージェントとの通信に失敗しました",
    ErrorCode.AI_BEDROCK_UNAVAILABLE: "Bedrockサービスが利用できません",
    ErrorCode.AI_BEDROCK_CONNECTION_FAILED: "Bedrockへの接続に失敗しました",
    ErrorCode.AI_BEDROCK_API_FAILED: "Bedrock APIの呼び出しに失敗しました",
    ErrorCode.AI_OPERATION_FAILED: "AI処理中にエラーが発生しました",
    ErrorCode.DATABASE_CREDENTIALS_INVALID: "データベース認証情報が無効です",
    ErrorCode.DATABASE_TABLES_NOT_FOUND: "データベーステーブルが見つかりません",
    ErrorCode.DATABASE_OPERATION_FAILED: "データベース処理中にエラーが発生しました",
    ErrorCode.S3_OPERATION_FAILED: "S3処理中にエラーが発生しました",
    ErrorCode.S3_OBJECT_NOT_FOUND: "S3オブジェクトが見つかりません",
}

FALLBACK_MESSAGE = "エラーが発生しました。時間をおいて再度お試しください。"


def user_message_for(exc: BaseException | None, *, default: str | None = None) -> str:
    """Resolve user-facing wording from an exception's error code.

    Args:
        exc: The caught exception. Its ``code`` and ``context`` attributes are
            read defensively, so plain exceptions are handled too.
        default: Wording for exceptions that carry no usable code. Use it to
            keep context-specific phrasing at ``except Exception`` sites (e.g.
            "レポートの生成に失敗しました").

    Returns:
        The catalog wording, or ``default`` (or :data:`FALLBACK_MESSAGE`) when
        the code is missing, uncatalogued, or its template cannot be filled.
        The exception's own message is never returned.
    """
    fallback = default or FALLBACK_MESSAGE
    code = getattr(exc, "code", None)
    if not isinstance(code, ErrorCode) or code is ErrorCode.UNKNOWN:
        # Builtin transport failures carry no code but are worth distinguishing,
        # since the user can act on them (check the connection and retry).
        if isinstance(exc, (ConnectionError, TimeoutError)):
            code = ErrorCode.NETWORK_ERROR
    template = _CATALOG.get(code) if isinstance(code, ErrorCode) else None
    if template is None:
        return fallback

    context = getattr(exc, "context", None)
    if not isinstance(context, dict):
        context = {}
    if "field" in context:
        # Managers pass a stable field key; the display label lives here.
        context = {**context, "field": _FIELD_LABELS.get(context["field"], "入力値")}
    try:
        return template.format(**context)
    except (KeyError, IndexError, ValueError, TypeError):
        # A wording bug must not become a 500, and must not leak the message.
        logger.warning("エラー文言の補間に失敗しました (code=%s)", code, exc_info=True)
        return fallback


def user_message_for_code(
    code: str | None,
    context: dict[str, object] | None = None,
    *,
    default: str | None = None,
) -> str:
    """Resolve wording from a *stored* error code rather than a live exception.

    Failures that happen in a background thread cannot be turned into wording by
    the router that started them: nothing is returned to that request. Those
    failures persist an ``ErrorCode`` value (see ``Survey.error_code``) which a
    later GET resolves here. Storing the exception message instead would put S3
    paths, role ARNs and SDK text on the screen (Issue #118).

    Unknown or unparsable codes fall back like :func:`user_message_for`, so a
    record written by an older version can never surface raw text.
    """
    parsed = ErrorCode.parse(code)
    if parsed is None:
        if code:
            # A code written by another revision. A generic message is still
            # better than showing nothing (and far better than raw text).
            logger.warning("未知のエラーコードです (code=%r)", code)
        return default or FALLBACK_MESSAGE
    return user_message_for(CodedError(code=parsed, context=context), default=default)


def error_kind_of(exc: BaseException | None) -> ErrorKind:
    """Resolve the presentation kind of an exception.

    Uncoded exceptions resolve to :attr:`ErrorKind.TRANSIENT` so that an
    unclassified failure is never presented as something the user can fix by
    editing input. ``ConnectionError`` / ``TimeoutError`` are mapped the same way
    as in :func:`user_message_for`.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, ErrorCode) and code is not ErrorCode.UNKNOWN:
        return code.kind
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return ErrorCode.NETWORK_ERROR.kind
    return ErrorCode.UNKNOWN.kind


def toast_response(
    exc: BaseException | None,
    *,
    default: str | None = None,
    status_code: int = 400,
) -> Response:
    """Return an empty response that shows the error as a toast.

    For ``ErrorKind.TRANSIENT`` errors there is nothing for the user to correct,
    so replacing the page region with an error panel only destroys the form they
    already filled in. Instead this returns no body and asks the client to raise
    a toast via ``HX-Trigger``.

    htmx processes ``HX-Trigger`` in ``htmx:beforeOnLoad``, before it decides
    whether to swap, so this works on 4xx responses even on htmx 1.9.10 (which
    refuses to swap non-2xx bodies). That is why TRANSIENT errors are unaffected
    by the swap limitation described in Issue #117.

    Args:
        exc: The caught exception. Only its code/context are used.
        default: Wording for exceptions carrying no usable code.
        status_code: HTTP status. Kept in the 4xx range so that
            ``htmx:responseError`` semantics and server logs stay accurate.
    """
    message = user_message_for(exc, default=default)
    # HTTPヘッダーは latin-1 でしかエンコードできないため、日本語文言は
    # ensure_ascii=True（既定）で \uXXXX にエスケープする。JSON.parse する
    # クライアント側で元の文字列に戻る。
    return Response(
        status_code=status_code,
        headers={
            "HX-Trigger": json.dumps(
                {"showToast": {"message": message, "type": "error"}}
            )
        },
    )


def is_transient(exc: BaseException | None) -> bool:
    """再試行で解決しうるエラー（画面を書き換えずトーストで通知すべきもの）か。

    Manager層の例外型は VALIDATION と TRANSIENT の両方を投げるため、Router の
    ``except`` 節は型では区別できない。表示方法の判断はこの関数に集約する
    （Router側に kind の分岐ロジックを散らさないため）。
    """
    return error_kind_of(exc) is ErrorKind.TRANSIENT


def is_correctable(exc: BaseException | None) -> bool:
    """利用者が入力を直せば解決するエラー（フォームを再描画すべきもの）か。

    ``VALIDATION``（入力の誤り）と ``CAPACITY``（入力量の超過）はどちらも
    「送信値を保持したままフォームを出し直す」のが正しい表示になる。逆に
    ``NOT_FOUND`` / ``CONFIG`` はフォームを出し直しても解決しないため含めない。
    """
    return error_kind_of(exc) in (ErrorKind.VALIDATION, ErrorKind.CAPACITY)


def field_of(exc: BaseException | None) -> str | None:
    """バリデーション対象のフィールドキーを返す（無い場合は ``None``）。

    Manager が ``context["field"]`` に安定キーを載せている場合のみ取得できる。
    テンプレートはこれを使ってエラーを該当フィールドの横に表示する。キーから
    表示名への変換はカタログ側（:data:`_FIELD_LABELS`）が持つので、Router や
    テンプレートが日本語のフィールド名を知る必要はない。
    """
    context = getattr(exc, "context", None)
    if not isinstance(context, dict):
        return None
    field = context.get("field")
    return field if isinstance(field, str) else None


#: 非2xx応答の本文をスワップさせるためのヘッダー。
#:
#: htmx 1.9.10 は ``status>=200 && status<400 && status!==204`` 以外の本文を
#: スワップしないため、4xx/5xx でエラーパーシャルを返しても画面に反映されない。
#: このヘッダーが付いた応答だけをクライアント側で許可する
#: （``web/static/js/app.js`` の ``htmx:beforeSwap`` を参照）。
#:
#: ステータスコードで一律に許可しないのは、汎用エラーパーシャルが ``hx-target``
#: （本体コンテンツや一覧）に流れ込んでフォームごと消える経路があるため。
#: 「表示してよい」判断はサーバー側が持つ（Issue #117）。
RENDER_RESPONSE_HEADER = {"X-Render-Response": "true"}


def mark_renderable(response: _ResponseT) -> _ResponseT:
    """非2xx応答をクライアントがDOMに反映してよいものとして印を付ける。

    Router は各画面固有のテンプレート・コンテキストを持つため、応答の組み立て
    自体はRouterに残し、この関数は「表示してよい」印だけを付ける。

    Examples:
        >>> return mark_renderable(
        ...     templates.TemplateResponse(
        ...         request, "partials/error.html",
        ...         {"request": request, "error": user_message_for(e)},
        ...         status_code=400,
        ...     )
        ... )
    """
    response.headers.update(RENDER_RESPONSE_HEADER)
    return response
