"""Look up the AgentCore Memory Strategy ID with GetMemory at startup.

The Data stack creates the Memory, so the Strategy ID is not copied by hand. If it is
given by an environment variable, that value is used; if the lookup fails, long-term
memory is disabled and startup continues. The boto3 client is replaced with unittest.mock.
"""

import logging
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from src.config import Config
from src.services.memory.strategy_lookup import resolve_memory_strategies


def _config(monkeypatch: pytest.MonkeyPatch, **env: str) -> Config:
    for name in (
        "ENABLE_LONG_TERM_MEMORY",
        "AGENTCORE_MEMORY_ID",
        "SUMMARY_MEMORY_STRATEGY_ID",
        "SEMANTIC_MEMORY_STRATEGY_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Config()


def _client(strategies: list[dict[str, str]]) -> Mock:
    client = Mock()
    client.get_memory.return_value = {
        "memory": {"id": "mem-1", "strategies": strategies}
    }
    return client


ENABLED = {"ENABLE_LONG_TERM_MEMORY": "true", "AGENTCORE_MEMORY_ID": "mem-1"}


def test_strategy_ids_are_looked_up_at_startup(monkeypatch):
    cfg = _config(monkeypatch, **ENABLED)
    client = _client(
        [
            {"strategyId": "summary-abc", "type": "SUMMARIZATION"},
            {"strategyId": "semantic-xyz", "type": "SEMANTIC"},
        ]
    )

    resolve_memory_strategies(cfg, client=client)

    client.get_memory.assert_called_once_with(memoryId="mem-1")
    assert cfg.SUMMARY_MEMORY_STRATEGY_ID == "summary-abc"
    assert cfg.SEMANTIC_MEMORY_STRATEGY_ID == "semantic-xyz"
    assert cfg.ENABLE_LONG_TERM_MEMORY is True


def test_ids_from_the_environment_win_and_skip_the_call(monkeypatch):
    cfg = _config(
        monkeypatch,
        **ENABLED,
        SUMMARY_MEMORY_STRATEGY_ID="summary-env",
        SEMANTIC_MEMORY_STRATEGY_ID="semantic-env",
    )
    client = _client([])

    resolve_memory_strategies(cfg, client=client)

    client.get_memory.assert_not_called()
    assert cfg.SUMMARY_MEMORY_STRATEGY_ID == "summary-env"


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetMemory"),
        EndpointConnectionError(endpoint_url="https://bedrock-agentcore.invalid"),
    ],
)
def test_lookup_failure_disables_long_term_memory_and_continues(
    monkeypatch, caplog, error
):
    cfg = _config(monkeypatch, **ENABLED)
    client = Mock()
    client.get_memory.side_effect = error
    caplog.set_level(logging.WARNING)

    resolve_memory_strategies(cfg, client=client)

    assert cfg.ENABLE_LONG_TERM_MEMORY is False
    assert cfg.SUMMARY_MEMORY_STRATEGY_ID is None
    assert "long-term memory" in caplog.text.lower()


def test_nothing_happens_when_memory_is_disabled(monkeypatch):
    cfg = _config(monkeypatch, AGENTCORE_MEMORY_ID="mem-1")
    client = _client([])

    resolve_memory_strategies(cfg, client=client)

    client.get_memory.assert_not_called()


def test_missing_strategy_type_leaves_that_id_unset(monkeypatch):
    cfg = _config(monkeypatch, **ENABLED)
    client = _client([{"strategyId": "summary-abc", "type": "SUMMARIZATION"}])

    resolve_memory_strategies(cfg, client=client)

    assert cfg.SUMMARY_MEMORY_STRATEGY_ID == "summary-abc"
    assert cfg.SEMANTIC_MEMORY_STRATEGY_ID is None
    assert cfg.ENABLE_LONG_TERM_MEMORY is True


def test_memory_service_uses_the_ids_looked_up_at_startup(monkeypatch):
    """The looked-up IDs live on the shared config, not in the environment.

    The memory strategies must read that shared object; a fresh ``Config()``
    re-reads the environment and loses them (manual knowledge input then
    fails with MEMORY_STRATEGY_NOT_CONFIGURED).
    """
    from unittest.mock import patch

    from src import config as config_module
    from src.services.memory.memory_service import MemoryService

    shared = _config(monkeypatch, **ENABLED)
    monkeypatch.setattr(config_module, "config", shared)
    resolve_memory_strategies(
        shared,
        client=_client(
            [
                {"strategyId": "summary-abc", "type": "SUMMARIZATION"},
                {"strategyId": "semantic-xyz", "type": "SEMANTIC"},
            ]
        ),
    )

    with patch("bedrock_agentcore.memory.MemoryClient"):
        service = MemoryService(memory_id="mem-1", validate_connection=False)

    assert service.is_semantic_enabled is True
    assert service._semantic_strategy.memory_strategy_id == "semantic-xyz"
    assert service._strategy.memory_strategy_id == "summary-abc"
