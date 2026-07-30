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

# ErrorCode -> user-facing wording. Values are ``str.format`` templates whose
# placeholders are filled from ``CodedError.context``.
_CATALOG: dict[ErrorCode, str] = {
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
    template = _CATALOG.get(code) if isinstance(code, ErrorCode) else None
    if template is None:
        return fallback

    context = getattr(exc, "context", None)
    if not isinstance(context, dict):
        context = {}
    try:
        return template.format(**context)
    except (KeyError, IndexError, ValueError, TypeError):
        # A wording bug must not become a 500, and must not leak the message.
        logger.warning("エラー文言の補間に失敗しました (code=%s)", code, exc_info=True)
        return fallback
