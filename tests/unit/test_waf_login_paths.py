"""WAF のログイン系のパスとアプリのルートの照合（U9、NFR4.16、約束 C11）。

``cdk/lib/edge-stack.ts`` の ``LOGIN_RATE_LIMITED_PATHS`` は、アプリのログイン系の URL と
食い違うとレート制限が効かなくなる。TypeScript は実行せず、定数の行を文字列として読み、
``web/routers/auth.py`` のルートと ``NEW_PASSWORD_PATH`` に照らす。
"""

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from web.routers import auth

ROOT = Path(__file__).parent.parent.parent
EDGE_STACK = ROOT / "cdk" / "lib" / "edge-stack.ts"

_CONSTANT = re.compile(
    r"export const LOGIN_RATE_LIMITED_PATHS\s*=\s*\[(?P<body>[^\]]*)\]", re.DOTALL
)


def _waf_paths() -> list[str]:
    match = _CONSTANT.search(EDGE_STACK.read_text(encoding="utf-8"))
    assert match is not None, "LOGIN_RATE_LIMITED_PATHS is not found in edge-stack.ts"
    return re.findall(r"'([^']*)'", match.group("body"))


def _route_paths() -> set[str]:
    return {getattr(route, "path", "") for route in auth.router.routes}


def test_the_constant_has_the_three_login_paths():
    assert _waf_paths() == ["/login", "/auth/callback", "/auth/new-password"]


@pytest.mark.parametrize("path", ["/login", "/auth/callback", "/auth/new-password"])
def test_each_rate_limited_path_is_an_auth_route(path):
    assert path in _waf_paths()
    assert path in _route_paths()


def test_new_password_path_matches_the_app_constant():
    assert auth.NEW_PASSWORD_PATH in _waf_paths()
    assert auth.NEW_PASSWORD_PATH == "/auth/new-password"


_TRANSFORMATION = re.compile(
    r"textTransformations:\s*\[\{\s*priority:\s*0,\s*type:\s*'(\w+)'"
)


def test_the_waf_decodes_the_path_once_before_comparing():
    """The app decodes the path once (``/%6cogin`` is ``/login``); the WAF must too."""
    text = EDGE_STACK.read_text(encoding="utf-8")
    login_rule = text[text.index("loginRateLimitRule") :]
    match = _TRANSFORMATION.search(login_rule)
    assert match is not None, "the LoginRateLimit text transformation is not found"
    assert match.group(1) == "URL_DECODE"


@pytest.mark.parametrize(
    ("encoded", "path"),
    [
        ("/%6cogin", "/login"),
        ("/%6Cogin", "/login"),
        ("/auth/%63allback", "/auth/callback"),
        ("/auth/new%2dpassword", "/auth/new-password"),
    ],
)
def test_an_encoded_path_reaches_the_same_route_and_is_counted(encoded, path):
    """An encoded login path is the same route for the app and, after one URL_DECODE,
    the same rate-limited path for the WAF.

    The ASGI server (uvicorn) puts the once-decoded path in ``scope["path"]`` and the
    router matches on it, so ``/%6cogin`` is served as ``/login``.
    """
    from starlette.routing import Match

    scope = {
        "type": "http",
        "method": "GET",
        "path": unquote(encoded),
        "raw_path": encoded.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
    }
    matched = {
        getattr(r, "path", "")
        for r in auth.router.routes
        if r.matches(scope)[0] is not Match.NONE
    }
    assert matched == {path}
    assert unquote(encoded) in _waf_paths()


def test_the_check_reads_the_auth_routes():
    """空振りしないこと（ルートの一覧が読めている）。"""
    assert {"/login", "/auth/callback"} <= _route_paths()
