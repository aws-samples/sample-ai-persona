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
import re

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.models.errors import CodedError, ErrorCode
from web.error_messages import (
    FALLBACK_MESSAGE,
    UNIFIED_NOT_FOUND_TOAST_CODES,
    user_message_for,
)
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
        # 404 の画面は理由を区別しない（M11）。資源別の文言は出さない
        assert _not_found_text(response.text) == M11
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


#: M11（mockups.md）の固定の見出しと本文。画面の仕様の固定文言として確かめる
M11 = ("ページが見つかりません", "指定されたページは存在しないか、表示できません。")
BROWSER = {"accept": "text/html"}


def _not_found_text(html: str) -> tuple[str, str]:
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    message = re.search(
        r'<p[^>]*data-testid="error-page-message"[^>]*>(.*?)</p>', html, re.DOTALL
    )
    assert heading and message, "エラー画面の見出しか本文がない"
    return (" ".join(heading.group(1).split()), " ".join(message.group(1).split()))


def _title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    assert match
    return " ".join(match.group(1).split())


class TestNotFoundPage:
    """M11 / NFR1.9: one 404 screen whatever the reason (WF8)."""

    def test_heading_body_links_and_title(self, api):
        response = api.get(
            f"/page/boom/{ErrorCode.PERSONA_NOT_FOUND.value}", headers=BROWSER
        )

        assert _not_found_text(response.text) == M11
        assert _title(response.text) == "ページが見つかりません - AIペルソナシステム"
        assert re.search(r'<a href="/"[^>]*>\s*トップへ\s*</a>', response.text)
        assert "一覧に戻る" in response.text

    def test_given_wording_is_not_shown(self, api):
        response = api.get(
            f"/page/boom/{ErrorCode.PERSONA_NOT_FOUND.value}", headers=BROWSER
        )

        assert (
            user_message_for(CodedError(code=ErrorCode.PERSONA_NOT_FOUND))
            not in response.text
        )

    @pytest.mark.parametrize(
        "code",
        sorted(UNIFIED_NOT_FOUND_TOAST_CODES, key=str),
        ids=lambda c: c.name,
    )
    def test_every_resource_code_matches_an_unknown_url(self, api, code):
        unknown = api.get("/page/no-such-thing", headers=BROWSER)
        response = api.get(f"/page/boom/{code.value}", headers=BROWSER)

        assert response.status_code == unknown.status_code == 404
        assert _not_found_text(response.text) == _not_found_text(unknown.text) == M11

    def test_router_http_exception_detail_is_hidden_too(self, test_app, login_as):
        from unittest.mock import patch

        from src.models.errors import CodedError, ErrorCode

        with patch("web.routers.persona.get_persona_manager") as get_manager:
            get_manager.return_value.get_persona_detail.side_effect = CodedError(
                "not readable", code=ErrorCode.PERSONA_NOT_FOUND
            )
            with TestClient(test_app, follow_redirects=False) as browser:
                login_as(browser)
                response = browser.get("/persona/nope", headers=BROWSER)

        assert response.status_code == 404
        assert _not_found_text(response.text) == M11
        assert 'href="/persona/management"' in response.text

    def test_other_errors_keep_their_screen(self, api):
        response = api.get("/page/crash", headers=BROWSER)

        heading, message = _not_found_text(response.text)
        assert heading == "エラーが発生しました"
        assert message == FALLBACK_MESSAGE
        assert _title(response.text) == "エラー - AIペルソナシステム"
