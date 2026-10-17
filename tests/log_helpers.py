"""Reading the one-line JSON logs of ``log_event`` in tests (stdout, via capsys).

Also holds :func:`assert_not_leaked`, the one check (U8 NFR3.15, AC1.2.5) that a
sentinel password appears in no log, exception, response body or stored item.
"""

import json
import logging
import traceback
from collections.abc import Iterable, Mapping
from typing import Any

import pytest

#: Sentinel password for U8 tests. Never a real credential.
SENTINEL_PASSWORD = "Pw-SENTINEL-8f3a!"


class LogCapture:
    """Collects the JSON log lines written to stdout so far."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys
        self._records: list[dict[str, Any]] = []

    def records(self) -> list[dict[str, Any]]:
        out = self._capsys.readouterr().out
        for line in out.splitlines():
            if line.startswith("{"):
                self._records.append(json.loads(line))
        return self._records

    def events(self, name: str) -> list[dict[str, Any]]:
        """Records of one event name."""
        return [r for r in self.records() if r.get("event") == name]


def _as_text(source: Any) -> str:
    """Everything a reader could see of ``source``, as one string."""
    if isinstance(source, str):
        return source
    if isinstance(source, BaseException):
        return "\n".join(
            [
                str(source),
                repr(source.args),
                repr(getattr(source, "context", None)),
                "".join(traceback.format_exception(source)),
            ]
        )
    if isinstance(source, logging.LogRecord):
        parts = [source.getMessage()]
        if source.exc_info:
            parts.append("".join(traceback.format_exception(*source.exc_info)))
        return "\n".join(parts)
    if isinstance(source, pytest.LogCaptureFixture):
        return "\n".join([source.text, *(_as_text(r) for r in source.records)])
    if isinstance(source, Mapping):
        return json.dumps(source, default=repr, ensure_ascii=False)
    if isinstance(source, Iterable) and not isinstance(source, (bytes, bytearray)):
        return "\n".join(_as_text(item) for item in source)
    return repr(source)


def assert_not_leaked(secret: str, *sources: Any) -> None:
    """Fail when ``secret`` appears in any of ``sources``.

    A source may be a string (a response body, captured stdout), an exception
    (message, args, ``context`` and the formatted traceback), log records or
    ``caplog``, a mapping (a stored item, a structured log record) or a list
    of any of these. The failure message names the source type, never the
    secret.
    """
    for index, source in enumerate(sources):
        assert secret not in _as_text(source), (
            f"the secret leaked into source #{index} ({type(source).__name__})"
        )
