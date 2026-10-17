"""
Cognito communication (v1.0.0 U1 / U8).

One Service unit for the login calls to Amazon Cognito, so the two clients can
share the circuit breaker without one Service importing another
(``.claude/rules/architecture.md``; ``tests/unit/test_architecture_deps.py``
treats a subpackage as one unit):

- :mod:`.circuit_breaker`: the per-task breaker and its neutral
  ``CircuitOpenError``;
- :mod:`.oidc_service`: Managed Login (``AUTH_MODE=oidc``);
- :mod:`.cognito_auth_service`: the admin API (``AUTH_MODE=admin_api``).

Each client turns ``CircuitOpenError`` into its own error code.
"""
