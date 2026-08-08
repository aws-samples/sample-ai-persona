---
name: run-tests
description: This skill should be used when the user asks to "run tests", "test my changes", "check if tests pass", or mentions testing changed files. Detects modified files and routes to the appropriate test suite.
---

# Smart Test Runner

Detect changed files and execute the matching test suite.

## Workflow

1. Detect changed files:
   - Feature branch: `git diff --name-only origin/main...HEAD`
   - Main branch: `git diff --name-only HEAD~1`

2. Route to test suite based on file patterns:

| Changed path | Test command |
|---|---|
| `src/managers/*.py` | `uv run pytest tests/unit/ -q --tb=short` |
| `src/services/*.py` | `uv run pytest tests/integration/ -q --tb=short` |
| `web/routers/*.py` | `uv run pytest tests/api/ -q --tb=short` |
| `web/templates/*.html`, `web/static/*` | `uv run pytest tests/api/ -q --tb=short` |
| `cdk/**/*.ts` | `cd cdk && npx tsc --noEmit && npx cdk synth --no-staging` |
| No match | `uv run pytest -m unit -q --tb=short` |

3. Execute mapped tests.

4. Report results:
   - Pass/fail counts
   - Failure analysis (if any)
   - Suggest new tests for uncovered code
