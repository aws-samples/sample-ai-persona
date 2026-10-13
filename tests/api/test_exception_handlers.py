"""例外から HTTP 応答への変換（約束 C13、BR2.14、NFR3.6、NFR9.21）の API テスト。

4 つのハンドラ（CodedError・RequestValidationError・StarletteHTTPException・Exception）を
小さなアプリに登録し、種類ごとの状態コードと API の本文 ``{detail, error_code}`` を確かめる。
htmx と画面は、既存の表示（トースト・エラー画面・ログイン画面への移動）のまま。

想定外の例外（``Exception``）のハンドラは、Starlette では一番外側の
``ServerErrorMiddleware`` で動く（ほかのミドルウェアの外側）。TestClient は既定で
例外を送り直すため、``raise_server_exceptions=False`` で作って応答を確かめる
（NFR Design R-06）。
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.models.errors import CodedError, ErrorCode
from web.error_messages import FALLBACK_MESSAGE, user_message_for
from web.main import app as main_app
from web.main import register_exception_handlers

SECRET = "arn:aws:dynamodb:ap-northeast-1:999999999999:table/secret-42"


class _Body(BaseModel):
    name: str
    count: int


def _small_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/boom/{code}")
    async def api_boom(code: str) -> None:
        parsed = ErrorCode.parse(code)
        raise CodedError(SECRET, code=parsed)

    @app.get("/page/boom/{code}")
    async def page_boom(code: str) -> None:
        raise CodedError(SECRET, code=ErrorCode.parse(code))

    @app.post("/api/things")
    async def create_thing(body: _Body) -> dict:
        return {"ok": True}

    @app.get("/api/crash")
    async def api_crash() -> None:
        raise RuntimeError(SECRET)

    @app.get("/page/crash")
    async def page_crash() -> None:
        raise RuntimeError(SECRET)

    register_exception_handlers(app)
    return app


@pytest.fixture
def api():
    with TestClient(
        _small_app(), raise_server_exceptions=False, follow_redirects=False
    ) as c:
        yield c


def _assert_body(response, code: ErrorCode, detail: str | None = None) -> None:
    body = response.json()
    assert set(body) == {"detail", "error_code"}
    assert body["error_code"] == code.value
    assert body["detail"] == (
        detail if detail is not None else user_message_for(CodedError(code=code))
    )
    assert SECRET not in response.text


def test_the_main_app_registers_the_four_handlers():
    for exc_type in (
        CodedError,
        RequestValidationError,
        StarletteHTTPException,
        Exception,
    ):
        assert exc_type in main_app.exception_handlers


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.AUTH_REQUIRED, 401),
        (ErrorCode.ACCESS_DENIED, 403),
        (ErrorCode.PERSONA_NOT_FOUND, 404),
        (ErrorCode.PERSONA_FIELD_REQUIRED, 400),
        (ErrorCode.FILE_TOO_LARGE, 413),
        (ErrorCode.SURVEY_BATCH_ROLE_NOT_CONFIGURED, 500),
        (ErrorCode.LOOKUP_UNAVAILABLE, 503),
    ],
    ids=lambda v: v.name if isinstance(v, ErrorCode) else str(v),
)
def test_status_follows_the_kind(api, code, status):
    response = api.get(f"/api/boom/{code.value}")
    assert response.status_code == status
    _assert_body(response, code)


def test_unknown_code_is_500(api):
    response = api.get("/api/boom/unknown")
    assert response.status_code == 500
    _assert_body(response, ErrorCode.UNKNOWN, detail=FALLBACK_MESSAGE)


def test_malformed_api_request_is_request_invalid(api):
    response = api.post("/api/things", json={"name": "<b>x</b>", "count": "many"})
    assert response.status_code == 422
    _assert_body(response, ErrorCode.REQUEST_INVALID)
    for internal in ("loc", "int_parsing", "many", "<b>x</b>"):
        assert internal not in response.text


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("GET", "/api/nope", 404), ("DELETE", "/api/things", 405)],
)
def test_framework_http_errors_are_http_error(api, method, path, status):
    response = api.request(method, path)
    assert response.status_code == status
    _assert_body(response, ErrorCode.HTTP_ERROR)


def test_unexpected_exception_is_500_unknown_without_its_text(api):
    response = api.get("/api/crash")
    assert response.status_code == 500
    _assert_body(response, ErrorCode.UNKNOWN, detail=FALLBACK_MESSAGE)
    assert response.headers["cache-control"] == "no-store"


class TestHtmxAndPages:
    def test_htmx_transient_is_a_toast(self, api):
        response = api.get(
            f"/page/boom/{ErrorCode.LOOKUP_UNAVAILABLE.value}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 503
        assert response.text == ""
        toast = json.loads(response.headers["HX-Trigger"])["showToast"]
        assert toast["message"] == user_message_for(
            CodedError(code=ErrorCode.LOOKUP_UNAVAILABLE)
        )

    def test_htmx_forbidden_is_a_toast_without_swapping(self, api):
        response = api.get(
            f"/page/boom/{ErrorCode.ACCESS_DENIED.value}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 403
        assert response.text == ""
        assert "HX-Trigger" in response.headers

    def test_page_not_found_is_an_html_page(self, api):
        response = api.get(
            f"/page/boom/{ErrorCode.PERSONA_NOT_FOUND.value}",
            headers={"accept": "text/html"},
        )
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        assert user_message_for(CodedError(code=ErrorCode.PERSONA_NOT_FOUND)) in (
            response.text
        )
        assert SECRET not in response.text

    def test_page_unauthorized_goes_to_login(self, api):
        response = api.get(f"/page/boom/{ErrorCode.AUTH_REQUIRED.value}")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    def test_page_crash_is_an_html_page_without_its_text(self, api):
        response = api.get("/page/crash", headers={"accept": "text/html"})
        assert response.status_code == 500
        assert "text/html" in response.headers["content-type"]
        assert SECRET not in response.text
