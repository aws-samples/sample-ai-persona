"""
CSRF保護ミドルウェア

カスタムヘッダー検証方式:
- 状態変更リクエスト（POST/PUT/DELETE/PATCH）に対して
  HX-Request または X-Requested-With ヘッダーの存在を検証する
- htmxは自動で HX-Request ヘッダーを付与するため、htmx経由は自動で通過
- JS側の fetch() には X-Requested-With ヘッダーを明示的に付与する
"""

from typing import Any
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
EXEMPT_PATHS = {"/health"}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if (
            request.method in UNSAFE_METHODS
            and request.url.path not in EXEMPT_PATHS
        ):
            has_custom_header = (
                request.headers.get("HX-Request")
                or request.headers.get("X-Requested-With")
            )
            if not has_custom_header:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF validation failed"},
                )
        response = await call_next(request)
        return response  # type: ignore[no-any-return]
