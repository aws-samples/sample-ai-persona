"""AuthMiddleware（入口の判定）の API テスト。

US1.1（AC1.1.1、AC1.1.2）、US1.3（AC1.3.3、AC1.3.4）、BR1.1、BR1.2、BR1.9、
BR1.17、BR1.18、BR1.20、BR1.21、NFR4.1、NFR4.8、NFR8.5、NFR9.4、NFR9.8。

実アプリ（AUTH_MODE=local）での振る舞いと、小さな Starlette アプリに
AuthManager のモックを入れた振る舞いの両方で確かめる。
"""

import asyncio
import io
import json
import re
import threading
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from src.managers.auth_manager import AuthManager
from src.models.authz import Principal, Role
from src.models.errors import AuthRequiredError, ErrorCode, LookupUnavailableError
from web.auth_middleware import (
    PUBLIC_PATHS,
    AuthMiddleware,
    is_passive_request,
    is_public_path,
)

_PRINCIPAL = Principal(sub="user-1", roles=frozenset({Role.MEMBER}))


# =============================================================================
# 小さなアプリ（AuthManager はモック）
# =============================================================================


async def _whoami(request: Request) -> JSONResponse:
    principal = request.state.principal
    return JSONResponse({"sub": principal.sub})


async def _ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _small_app(manager: Mock, *, sample_rate: float = 0.0, stream: Any = None) -> Any:
    app = Starlette(
        routes=[
            Route("/page", _whoami),
            Route("/api/thing", _whoami),
            Route("/health", _ok),
            Route("/discussion/stream", _whoami),
        ]
    )
    return AuthMiddleware(
        app, manager_getter=lambda: manager, sample_rate=sample_rate, log_stream=stream
    )


def _manager(**behaviour: Any) -> Mock:
    manager = Mock(spec=AuthManager)
    manager.check_request.return_value = _PRINCIPAL
    for name, value in behaviour.items():
        setattr(manager.check_request, name, value)
    return manager


class TestUnauthenticatedResponses:
    def test_page_redirects_to_login_with_next(self, test_app):
        with TestClient(test_app, follow_redirects=False) as anonymous:
            response = anonymous.get("/persona/management?tab=mine")

        assert response.status_code == 303
        location = urlsplit(response.headers["location"])
        assert location.path == "/login"
        assert parse_qs(location.query)["next"] == ["/persona/management?tab=mine"]

    def test_htmx_gets_hx_redirect_to_the_current_page(self, test_app):
        with TestClient(test_app, follow_redirects=False) as anonymous:
            response = anonymous.get(
                "/persona/list/partial",
                headers={
                    "HX-Request": "true",
                    "HX-Current-URL": "http://testserver/persona/management",
                },
            )

        assert response.status_code == 401
        redirect = urlsplit(response.headers["HX-Redirect"])
        assert redirect.path == "/login"
        assert parse_qs(redirect.query)["next"] == ["/persona/management"]

    def test_api_answers_401_json_without_redirect(self, test_app):
        with TestClient(test_app, follow_redirects=False) as anonymous:
            response = anonymous.get("/api/personas")

        assert response.status_code == 401
        assert "location" not in response.headers
        body = response.json()
        assert body["error_code"] == ErrorCode.AUTH_REQUIRED
        assert body["detail"]

    def test_expired_session_is_reported_as_expired(self):
        manager = _manager(
            side_effect=AuthRequiredError("idle", code=ErrorCode.SESSION_EXPIRED)
        )
        with TestClient(_small_app(manager), follow_redirects=False) as c:
            c.cookies.set("session_id", "old")
            page = c.get("/page")
            api = c.get("/api/thing")

        query = parse_qs(urlsplit(page.headers["location"]).query)
        assert query["reason"] == ["expired"]
        assert query["next"] == ["/page"]
        assert api.status_code == 401
        assert api.json()["error_code"] == ErrorCode.SESSION_EXPIRED
        # 消えたセッションの Cookie は消す
        assert any(
            c.startswith("session_id=") for c in page.headers.get_list("set-cookie")
        )

    def test_unknown_session_cookie_is_cleared(self, test_app):
        with TestClient(test_app, follow_redirects=False) as anonymous:
            anonymous.cookies.set("session_id", "no-such-session")
            response = anonymous.get("/persona/management")

        assert response.status_code == 303
        cleared = [
            c
            for c in response.headers.get_list("set-cookie")
            if c.startswith("session_id=")
        ]
        assert cleared and "max-age=0" in cleared[0].lower()


class TestPublicPaths:
    @pytest.mark.parametrize(
        "path", ["/health", "/api/health", "/static/css/tailwind.css"]
    )
    def test_public_paths_pass_without_a_session(self, test_app, path):
        with TestClient(test_app, follow_redirects=False) as anonymous:
            response = anonymous.get(path)
        assert response.status_code == 200
        assert response.headers.get("cache-control") != "no-store"

    def test_login_and_callback_are_reachable_without_a_session(self, test_app):
        with TestClient(test_app, follow_redirects=False) as anonymous:
            assert anonymous.get("/login?reason=signed_out").status_code == 200
            assert anonymous.get("/auth/callback").status_code == 200

    def test_public_list_is_exactly_the_documented_one(self):
        assert PUBLIC_PATHS == frozenset(
            {"/health", "/api/health", "/login", "/auth/callback", "/signout"}
        )
        assert is_public_path("/static/js/app.js")
        assert not is_public_path("/static")
        assert not is_public_path("/healthz")


class TestSignedInRequests:
    def test_signed_in_responses_are_not_stored(self, client):
        response = client.get("/persona/management")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    def test_principal_is_on_request_state(self):
        with TestClient(_small_app(_manager())) as c:
            c.cookies.set("session_id", "sid")
            assert c.get("/page").json() == {"sub": "user-1"}

    def test_users_without_roles_are_stopped_at_the_entrance(self):
        """U1 の「ロールがなくても既存の画面を使える」は U2 の BR2.8 で置き換えた。

        詳しい振る舞いは ``tests/api/test_role_required.py``。
        """
        manager = _manager(return_value=Principal(sub="user-1"))
        with TestClient(_small_app(manager), follow_redirects=False) as c:
            c.cookies.set("session_id", "sid")
            page = c.get("/page")
            api = c.get("/api/thing")
        assert page.status_code == 303
        assert api.status_code == 403
        assert api.json()["error_code"] == ErrorCode.ROLE_REQUIRED

    def test_passive_requests_are_marked(self):
        manager = _manager()
        with TestClient(_small_app(manager)) as c:
            c.cookies.set("session_id", "sid")
            c.get("/discussion/stream")
            c.get("/page")
        passive_flags = [
            call.kwargs["passive"] for call in manager.check_request.call_args_list
        ]
        assert passive_flags == [True, False]

    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("GET", "/discussion/stream", True),
            ("GET", "/interview/abc-123/stream", True),
            ("GET", "/interview/abc-123/status", True),
            ("GET", "/survey/persona-data/nemotron-status", True),
            ("GET", "/api/jobs/job-1", True),
            ("POST", "/interview/abc-123/message/stream", False),
            ("GET", "/interview/abc-123", False),
            ("GET", "/persona/management", False),
        ],
    )
    def test_passive_list(self, method, path, expected):
        assert is_passive_request(method, path) is expected


class TestFailClosed:
    def test_unreachable_sessions_table_answers_503(self):
        manager = _manager(side_effect=LookupUnavailableError("down"))
        with TestClient(_small_app(manager), follow_redirects=False) as c:
            c.cookies.set("session_id", "sid")
            page = c.get("/page")
            api = c.get("/api/thing")

        assert page.status_code == 503
        assert api.status_code == 503
        assert api.json()["error_code"] == ErrorCode.LOOKUP_UNAVAILABLE

    async def test_slow_sessions_table_does_not_block_health(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_check(session_id: Any, *, passive: bool) -> Principal:
            entered.set()
            release.wait(5)
            return _PRINCIPAL

        manager = _manager(side_effect=slow_check)
        transport = httpx.ASGITransport(app=_small_app(manager))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies={"session_id": "s"}
        ) as c:
            slow = asyncio.create_task(c.get("/page"))
            await asyncio.to_thread(entered.wait, 5)
            health = await asyncio.wait_for(c.get("/health"), timeout=2)
            assert health.status_code == 200
            assert not slow.done()
            release.set()
            assert (await slow).status_code == 200


class TestTimingLog:
    def test_request_check_duration_is_logged_at_the_sample_rate(self):
        stream = io.StringIO()
        app = _small_app(_manager(), sample_rate=1.0, stream=stream)
        with TestClient(app) as c:
            c.cookies.set("session_id", "sid")
            c.get("/page")
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        checked = [e for e in events if e["event"] == "auth.request_checked"]
        assert len(checked) == 1
        assert isinstance(checked[0]["duration_ms"], (int, float))

    def test_nothing_is_logged_when_the_sample_rate_is_zero(self):
        stream = io.StringIO()
        app = _small_app(_manager(), sample_rate=0.0, stream=stream)
        with TestClient(app) as c:
            c.cookies.set("session_id", "sid")
            c.get("/page")
        assert "auth.request_checked" not in stream.getvalue()


# =============================================================================
# 網羅テスト（NFR4.1）: 例外の一覧にない URL は、未ログインで 200 を返さない
# =============================================================================


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _walk(routes: list[Any], prefix: str, found: list[tuple[str, str]]) -> None:
    for route in routes:
        context = getattr(route, "include_context", None)
        if context is not None:
            # FastAPI 0.137 は include_router した Router を遅延の入れ物で持つ
            _walk(route.original_router.routes, prefix + context.prefix, found)
        elif isinstance(route, Mount):
            found.append(("GET", f"{prefix}{route.path}/js/app.js"))
        else:
            for method in sorted(getattr(route, "methods", None) or {"GET"}):
                if method != "HEAD":
                    found.append((method, _concrete(prefix + route.path)))


def _all_routes(test_app: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    _walk(list(test_app.routes), "", found)
    return found


def test_every_route_requires_login_unless_listed(test_app):
    routes = _all_routes(test_app)
    assert len(routes) > 50  # 全 Router が登録されていること

    with TestClient(
        test_app,
        headers={"X-Requested-With": "XMLHttpRequest"},
        follow_redirects=False,
        raise_server_exceptions=False,
    ) as anonymous:
        for method, path in routes:
            if is_public_path(path):
                continue
            response = anonymous.request(method, path)
            assert response.status_code in (303, 401), (method, path)
