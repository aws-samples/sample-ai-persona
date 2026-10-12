"""構造化ログ（JSON 1 行）の単体テスト（NFR3.3、NFR8.1、NFR8.2）。"""

import io
import json
from datetime import datetime

import pytest

from src.managers.shared.structured_log import FORBIDDEN_FIELDS, log_event


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


def _lines(stream: io.StringIO) -> list[str]:
    return stream.getvalue().splitlines()


def test_writes_exactly_one_json_line(stream):
    log_event("auth.login_succeeded", stream=stream, sub="user-1", auth_method="oidc")
    lines = _lines(stream)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "auth.login_succeeded"
    assert record["sub"] == "user-1"
    assert record["auth_method"] == "oidc"


def test_timestamp_is_iso8601_utc(stream):
    log_event("auth.logout", stream=stream)
    ts = json.loads(_lines(stream)[0])["ts"]
    parsed = datetime.fromisoformat(ts)
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("field", sorted(FORBIDDEN_FIELDS))
def test_forbidden_fields_are_dropped(stream, field):
    log_event(
        "auth.login_failed",
        stream=stream,
        reason="state_mismatch",
        **{field: "s3cr3t-value"},
    )
    line = _lines(stream)[0]
    assert "s3cr3t-value" not in line
    assert field not in json.loads(line)


def test_forbidden_list_covers_secrets():
    for field in (
        "access_token",
        "refresh_token",
        "session_id",
        "code",
        "state",
        "code_verifier",
        "password",
        "cookie",
    ):
        assert field in FORBIDDEN_FIELDS


def test_none_values_are_omitted_and_newlines_cannot_split_the_line(stream):
    log_event("http.access", stream=stream, sub=None, path='/a\n{"event": "x"}')
    lines = _lines(stream)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "sub" not in record
    assert record["path"] == '/a\n{"event": "x"}'
