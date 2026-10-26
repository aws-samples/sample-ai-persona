"""DataAgentPolicy のユニットテスト（閉域対応 U3 BR3.1、BR3.2）。

部品は config だけを読むので、値は monkeypatch で差し替える。
"""

import pytest

from src.config import config
from src.managers.components.data_agent_policy import DataAgentPolicy
from src.models.errors import CodedError, ErrorCode
from tests.error_helpers import raises_code

pytestmark = pytest.mark.unit


class _ManagerError(CodedError):
    pass


def _set(monkeypatch, *, allowed: bool, enabled: bool, arn: str | None) -> None:
    monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", allowed)
    monkeypatch.setattr(config, "ENABLE_DATA_AGENT", enabled)
    monkeypatch.setattr(config, "DATA_AGENT_RUNTIME_ARN", arn)


@pytest.mark.parametrize(
    ("allowed", "enabled", "arn", "available"),
    [
        (True, True, "arn:agent", True),
        (True, False, "arn:agent", False),
        (True, True, None, False),
        (True, True, "", False),
        (False, True, "arn:agent", False),
        (False, False, None, False),
    ],
)
def test_available_needs_allowed_enabled_and_an_arn(
    monkeypatch, allowed, enabled, arn, available
):
    _set(monkeypatch, allowed=allowed, enabled=enabled, arn=arn)
    assert DataAgentPolicy.allowed() is allowed
    assert DataAgentPolicy.available() is available


def test_ensure_allowed_passes_when_allowed(monkeypatch):
    _set(monkeypatch, allowed=True, enabled=False, arn=None)
    DataAgentPolicy.ensure_allowed(_ManagerError)


def test_ensure_allowed_stops_with_the_callers_error_type(monkeypatch):
    _set(monkeypatch, allowed=False, enabled=True, arn="arn:agent")
    with raises_code(_ManagerError, ErrorCode.DATA_AGENT_NOT_ALLOWED):
        DataAgentPolicy.ensure_allowed(_ManagerError)


def test_ensure_allowed_defaults_to_coded_error(monkeypatch):
    _set(monkeypatch, allowed=False, enabled=False, arn=None)
    with raises_code(CodedError, ErrorCode.DATA_AGENT_NOT_ALLOWED):
        DataAgentPolicy.ensure_allowed()
