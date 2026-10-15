"""
CSRF保護ミドルウェア

カスタムヘッダー検証方式（BR1.19、NFR4.2）:
- 状態変更リクエスト（POST/PUT/DELETE/PATCH）に対して
  HX-Request または X-Requested-With ヘッダーの存在を検証する
- htmxは自動で HX-Request ヘッダーを付与するため、htmx経由は自動で通過
- JS側の fetch() には X-Requested-With ヘッダーを明示的に付与する
- v1.0.0 で ``/api/`` の除外をやめた。対象外は ``/health`` だけ
- ログイン関連のフォーム送信（``POST /signout``）はカスタムヘッダーを付けられない
  ため、``Origin`` がアプリ自身（設定 ``APP_BASE_URL`` のスキーム・ホスト・ポート）
  と一致することで通す（Functional Design レビュー R-12）
- v1.0.0 U6: SSE のため GET で AI を動かす入口（:data:`SAME_ORIGIN_GET_PATHS`）は
  ``Sec-Fetch-Site: same-origin`` を確かめる（他サイトからの最上位の遷移で
  ``SameSite=Lax`` の cookie が付いて動くのを防ぐ）
"""

import re
from typing import Any, TextIO
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.config import config
from src.managers.auth_manager import record_event
from src.models.errors import CodedError, ErrorCode
from web.error_messages import user_message_for

UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
EXEMPT_PATHS = {"/health"}
#: カスタムヘッダーの代わりに Origin で確かめるフォーム送信（U8 で POST /login 等を足す）
ORIGIN_CHECKED_PATHS = {"/signout"}
#: SSE のため GET で AI を動かす入口（U6、security-design.md §4、NFR4.13）。
#: ``Sec-Fetch-Site`` が ``same-origin`` でなければ（ない場合も）断る。
#: ``EventSource`` / ``fetch`` は同じオリジンへの要求でこの値を送る
SAME_ORIGIN_GET_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/discussion/stream"),
    re.compile(r"/discussion/[^/]+/report/generate"),
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_of(url: str | None) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` with default ports filled in, or ``None``."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError:
        return None
    return scheme, parts.hostname.lower(), port


class CSRFMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests without a custom header or own Origin."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        app_base_url: str | None = None,
        log_stream: TextIO | None = None,
    ) -> None:
        super().__init__(app)
        self._allowed_origin = _origin_of(app_base_url or config.APP_BASE_URL)
        self._log_stream = log_stream

    def _passes(self, request: Request) -> bool:
        if request.headers.get("HX-Request") or request.headers.get("X-Requested-With"):
            return True
        if request.url.path in ORIGIN_CHECKED_PATHS:
            origin = _origin_of(request.headers.get("Origin"))
            return origin is not None and origin == self._allowed_origin
        return False

    @staticmethod
    def _same_origin_get_refused(request: Request) -> bool:
        return (
            request.method == "GET"
            and any(p.fullmatch(request.url.path) for p in SAME_ORIGIN_GET_PATHS)
            and request.headers.get("Sec-Fetch-Site") != "same-origin"
        )

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if (
            request.method in UNSAFE_METHODS
            and request.url.path not in EXEMPT_PATHS
            and not self._passes(request)
        ) or self._same_origin_get_refused(request):
            record_event(
                "auth.csrf_rejected",
                stream=self._log_stream,
                method=request.method,
                path=request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": user_message_for(CodedError(code=ErrorCode.CSRF_FAILED)),
                    "error_code": ErrorCode.CSRF_FAILED.value,
                },
            )
        response = await call_next(request)
        return response  # type: ignore[no-any-return]
