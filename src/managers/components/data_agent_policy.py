"""Whether the data analysis agent may be used here (closed network U3).

The data agent has entry points in four Managers (``SettingsManager``,
``PersonaGenerationManager``, ``SurveyDatasetManager``, ``ReportManager``) and
Managers cannot import each other, so the decision lives in this component
(``.claude/rules/architecture.md``, Component layer).

Rules (``construction/u3-closed-feature-gates/functional-design/rules.md``):

- BR3.1: ``allowed`` is ``Config.DATA_AGENT_ALLOWED`` (false only in the closed
  network). ``available`` is ``allowed`` and the agent is enabled and has a
  runtime ARN.
- BR3.2: when not allowed, every entry point stops with
  ``DATA_AGENT_NOT_ALLOWED`` before any data agent tool, service or agent is
  built (:meth:`DataAgentPolicy.ensure_allowed`).
- BR3.6: ``allowed`` is a fact of the environment and needs no permission.

Reads ``config`` only; holds no Service.
"""

from ...config import config
from ...models.errors import CodedError, ErrorCode


class DataAgentPolicy:
    """The two data agent decisions and the refusal."""

    @staticmethod
    def allowed() -> bool:
        """Whether this environment may use the data agent at all (BR3.1)."""
        return bool(config.DATA_AGENT_ALLOWED)

    @staticmethod
    def available() -> bool:
        """Whether the data agent can be used now (allowed, enabled, ARN set)."""
        return bool(
            DataAgentPolicy.allowed()
            and config.ENABLE_DATA_AGENT
            and config.DATA_AGENT_RUNTIME_ARN
        )

    @staticmethod
    def ensure_allowed(error_type: type[CodedError] = CodedError) -> None:
        """Stop when this environment may not use the data agent (BR3.2).

        Args:
            error_type: The caller's Manager exception, so that its router's
                ``except`` clause catches the refusal.

        Raises:
            CodedError: ``DATA_AGENT_NOT_ALLOWED`` (FORBIDDEN), as ``error_type``.
        """
        if not DataAgentPolicy.allowed():
            raise error_type(
                "data agent is not allowed in this environment",
                code=ErrorCode.DATA_AGENT_NOT_ALLOWED,
            )
