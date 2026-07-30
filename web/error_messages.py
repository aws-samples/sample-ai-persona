"""
User-facing error wording catalog (presentation layer).

Routers must resolve wording through :func:`user_message_for` and must never
write ``str(e)`` into a response. That keeps diagnostic detail in the logs and
makes ``py/stack-trace-exposure`` structurally impossible; the rule is enforced
mechanically by ``tests/api/test_error_exposure.py``.

The catalog is module-private on purpose: keeping every lookup behind
:func:`user_message_for` means adding a locale dimension later stays confined to
this file, with no change at the call sites.
"""

import logging

from src.models.errors import ErrorCode

logger = logging.getLogger(__name__)

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
    ErrorCode.GENERATION_CAPACITY_EXCEEDED: (
        "生成するデータ量が大きすぎて処理しきれませんでした。"
        "ペルソナ生成数を減らすか、アップロードするファイルを小さくして再度お試しください。"
    ),
    ErrorCode.REPORT_CAPACITY_EXCEEDED: (
        "分析対象のデータ量が大きすぎてレポートを生成しきれませんでした。"
        "対象を絞るか、議論ログを短くして再度お試しください。"
    ),
    ErrorCode.FILE_TOO_LARGE: (
        "ファイルサイズが制限を超えています。最大サイズ: {max_size_mb:.1f}MB"
    ),
    ErrorCode.FILE_FORMAT_NOT_ALLOWED: (
        "許可されていないファイル形式です。対応形式: {allowed_formats}"
    ),
    ErrorCode.FILE_MIME_UNSUPPORTED: "サポートされていないファイル種別です。",
    ErrorCode.FILE_EMPTY: "ファイルが空です。",
    ErrorCode.FILE_NOT_FOUND: "指定されたファイルが見つかりません。",
    ErrorCode.INTERVIEW_FILE_CONTENT_TOO_SHORT: (
        "ファイル内容が短すぎます。"
        "インタビューなどの内容を含むテキストファイルをアップロードしてください。"
    ),
    ErrorCode.MARKET_REPORT_CONTENT_TOO_SHORT: (
        "ファイル内容が短すぎます。"
        "市場調査レポートなどの詳細な内容を含むファイルをアップロードしてください。"
    ),
    ErrorCode.FILE_ENCODING_UNSUPPORTED: (
        "テキストファイルとして読み取れません。"
        "UTF-8、Shift_JIS、EUC-JPのいずれかでエンコードされた"
        "テキストファイルをアップロードしてください。"
    ),
    ErrorCode.CSV_ENCODING_UNSUPPORTED: (
        "CSVファイルとして読み取れません。"
        "UTF-8、Shift_JIS、EUC-JPのいずれかでエンコードしてください。"
    ),
    ErrorCode.FILE_NAME_INVALID: "ファイル名に不正な文字が含まれています。",
    ErrorCode.FILE_NAME_TOO_LONG: "ファイル名が長すぎます。",
    ErrorCode.FILE_HIDDEN_NOT_ALLOWED: "隠しファイルはアップロードできません。",
    ErrorCode.FILE_BINARY_NOT_ALLOWED: "バイナリファイルはアップロードできません。",
    ErrorCode.FILE_DELETE_NOT_ALLOWED: "指定されたファイルは削除できません。",
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
    ErrorCode.SURVEY_TEMPLATE_TOO_FEW_OPTIONS: (
        "選択式質問「{question_text}」には2つ以上の選択肢が必要です"
    ),
    ErrorCode.SURVEY_TEMPLATE_TOO_MANY_IMAGES: "画像は1枚まで添付できます",
    ErrorCode.SURVEY_TEMPLATE_IMAGE_NAME_MISSING: "画像には名前を設定してください",
    ErrorCode.SURVEY_TARGET_COUNT_TOO_LOW: (
        "対象ペルソナ数は{min_count}以上で指定してください"
    ),
    ErrorCode.SURVEY_TARGET_COUNT_TOO_HIGH: "対象ペルソナ数は{max_count}人までです",
    ErrorCode.SURVEY_TARGET_COUNT_TOO_HIGH_WITH_IMAGES: (
        "画像付きアンケートの場合、対象ペルソナ数は{max_count}人までです"
    ),
    ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED: (
        "Nemotronデータセットがまだダウンロードされていません。"
        "アンケート調査 > ペルソナデータ設定からデータセットをダウンロードしてください。"
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
    ErrorCode.DISCUSSION_OPERATION_FAILED: (
        "議論の処理中にエラーが発生しました。時間をおいて再度お試しください。"
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
    # --- ペルソナ ---
    # {field} は _FIELD_LABELS で表示名に解決される。
    ErrorCode.PERSONA_FIELD_REQUIRED: "{field}が設定されていません",
    ErrorCode.PERSONA_FIELD_TOO_LONG: "{field}は{max_length}文字以内で設定してください",
    ErrorCode.PERSONA_LIST_EMPTY: "{field}が1つも設定されていません",
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
    ErrorCode.MEMORY_SERVICE_UNAVAILABLE: "記憶サービスへの接続に失敗しました。",
    ErrorCode.MEMORY_OPERATION_FAILED: (
        "記憶の処理中にエラーが発生しました。時間をおいて再度お試しください。"
    ),
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
