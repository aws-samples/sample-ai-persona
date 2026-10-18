"""WAF のログイン系のパスとアプリのルートの照合（U9、NFR4.16、約束 C11）。

``cdk/lib/edge-stack.ts`` の ``LOGIN_RATE_LIMITED_PATHS`` は、アプリのログイン系の URL と
食い違うとレート制限が効かなくなる。TypeScript は実行せず、定数の行を文字列として読み、
``web/routers/auth.py`` のルートと ``NEW_PASSWORD_PATH`` に照らす。
"""

import re
from pathlib import Path

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


def test_the_check_reads_the_auth_routes():
    """空振りしないこと（ルートの一覧が読めている）。"""
    assert {"/login", "/auth/callback"} <= _route_paths()
