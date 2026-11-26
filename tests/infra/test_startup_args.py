"""uvicorn startup arguments.

uvicorn's default access log prints the URL query (/auth/callback?code=...&state=...)
as is, so it is turned off and replaced with the app's http.access. Keep-alive is
longer than the ALB idle timeout (300 s) so the app does not close the connection
before the ALB and cause a 502.
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


def test_container_waits_for_a_graceful_shutdown_within_the_ecs_stop_timeout():
    """uvicorn's shutdown wait is 60 seconds. It includes the coordinator's round wait
    (30 s) and fits within the ECS stopTimeout (120 s)."""
    cmd = _dockerfile_cmd()
    assert "--timeout-graceful-shutdown" in cmd
    seconds = int(cmd[cmd.index("--timeout-graceful-shutdown") + 1])
    assert seconds == 60
    assert 30 < seconds < 120


def test_container_serves_the_app_on_an_unprivileged_port():
    """A non-root user cannot bind below 1024, so 8080 is used (matches CDK's CONTAINER_PORT)."""
    cmd = _dockerfile_cmd()
    assert "web.main:app" in cmd
    assert cmd[cmd.index("--port") + 1] == "8080"


def test_container_runs_as_a_non_root_user():
    """The last USER is not root (UID 0)."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    users = re.findall(r"^USER\s+(\S+)\s*$", text, re.MULTILINE)
    assert users, "Dockerfile に USER がない"
    user = users[-1].split(":")[0]
    assert user not in ("root", "0")


@pytest.mark.parametrize(
    ("name", "expected"), [("access_log", False), ("timeout_keep_alive", 310)]
)
def test_local_runner_uses_the_same_settings(name, expected):
    assert _run_htmx_kwargs().get(name) == expected


def test_local_runner_listens_on_loopback_only():
    """Local login (AUTH_MODE=local) does no verification, so it is not reachable from others on the same network."""
    assert _run_htmx_kwargs().get("host") == "127.0.0.1"
