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
