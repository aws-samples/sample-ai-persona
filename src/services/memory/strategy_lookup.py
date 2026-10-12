"""
Look up AgentCore Memory strategy IDs at startup (v1.0.0).

In v1.0.0 the Data stack creates the Memory, so nobody copies strategy IDs into
``parameters.ts`` any more (``infrastructure-specification.md``, AgentCore Memory
row). At startup the app calls ``GetMemory`` once and fills
``SUMMARY_MEMORY_STRATEGY_ID`` / ``SEMANTIC_MEMORY_STRATEGY_ID`` on the config,
which the memory strategies and the session manager factory already read.

IDs given as environment variables win and skip the call. When the lookup
fails, long-term memory is turned off and startup continues, the same outcome
as the previous "missing ID means no memory" behaviour.
"""

import logging
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from src.config import Config

logger = logging.getLogger(__name__)

_CLIENT_CONFIG = BotoConfig(
    connect_timeout=5,
    read_timeout=5,
    retries={"total_max_attempts": 2, "mode": "standard"},
)
_SUMMARY_TYPE = "SUMMARIZATION"
_SEMANTIC_TYPE = "SEMANTIC"


def resolve_memory_strategies(cfg: Config, client: Any = None) -> None:
    """Fill missing strategy IDs on ``cfg`` from ``GetMemory``.

    Args:
        cfg: The configuration to update in place.
        client: Optional ``bedrock-agentcore-control`` client (tests).
    """
    if not cfg.ENABLE_LONG_TERM_MEMORY or not cfg.AGENTCORE_MEMORY_ID:
        return
    if cfg.SUMMARY_MEMORY_STRATEGY_ID and cfg.SEMANTIC_MEMORY_STRATEGY_ID:
        return
    control = client or boto3.client(
        "bedrock-agentcore-control",
        region_name=cfg.AGENTCORE_MEMORY_REGION,
        config=_CLIENT_CONFIG,
    )
    try:
        response = control.get_memory(memoryId=cfg.AGENTCORE_MEMORY_ID)
    except (ClientError, BotoCoreError) as e:
        logger.warning(
            "AgentCore Memory strategy lookup failed (%s); long-term memory is disabled",
            type(e).__name__,
        )
        cfg.ENABLE_LONG_TERM_MEMORY = False
        return
    strategies = (response.get("memory") or {}).get("strategies") or []
    by_type = {
        s.get("type"): s.get("strategyId")
        for s in strategies
        if isinstance(s, dict) and s.get("strategyId")
    }
    if not cfg.SUMMARY_MEMORY_STRATEGY_ID:
        cfg.SUMMARY_MEMORY_STRATEGY_ID = by_type.get(_SUMMARY_TYPE)
    if not cfg.SEMANTIC_MEMORY_STRATEGY_ID:
        cfg.SEMANTIC_MEMORY_STRATEGY_ID = by_type.get(_SEMANTIC_TYPE)
    logger.info(
        "AgentCore Memory strategies resolved (summary=%s, semantic=%s)",
        bool(cfg.SUMMARY_MEMORY_STRATEGY_ID),
        bool(cfg.SEMANTIC_MEMORY_STRATEGY_ID),
    )
