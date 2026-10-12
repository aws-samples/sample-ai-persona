"""uvicorn の起動の引数（NFR3.3、Infrastructure Design R-02）。

uvicorn の既定のアクセスログは URL のクエリ（/auth/callback?code=...&state=...）を
そのまま出すため止め、アプリの http.access に置き換える。keep-alive は ALB の
無操作タイムアウト（300 秒）より長くし、ALB より先にアプリが接続を閉じて 502 に
なるのを防ぐ。
"""

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent


def _dockerfile_cmd() -> list[str]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^CMD\s+(\[.*\])\s*$", text, re.MULTILINE)
    assert match, "Dockerfile に JSON 形式の CMD がない"
    return json.loads(match.group(1))


def _run_htmx_kwargs() -> dict[str, object]:
    tree = ast.parse((ROOT / "run_htmx.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
        ):
            return {
                kw.arg: ast.literal_eval(kw.value)
                for kw in node.keywords
                if kw.arg is not None
            }
    raise AssertionError("run_htmx.py に uvicorn.run がない")


def test_container_disables_the_uvicorn_access_log():
    assert "--no-access-log" in _dockerfile_cmd()


def test_container_keeps_connections_longer_than_the_alb():
    cmd = _dockerfile_cmd()
    assert "--timeout-keep-alive" in cmd
    assert int(cmd[cmd.index("--timeout-keep-alive") + 1]) == 310


def test_container_still_serves_the_app_on_port_80():
    cmd = _dockerfile_cmd()
    assert "web.main:app" in cmd
    assert cmd[cmd.index("--port") + 1] == "80"


@pytest.mark.parametrize(
    ("name", "expected"), [("access_log", False), ("timeout_keep_alive", 310)]
)
def test_local_runner_uses_the_same_settings(name, expected):
    assert _run_htmx_kwargs().get(name) == expected
