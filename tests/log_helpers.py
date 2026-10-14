"""Reading the one-line JSON logs of ``log_event`` in tests (stdout, via capsys)."""

import json
from typing import Any

import pytest


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
