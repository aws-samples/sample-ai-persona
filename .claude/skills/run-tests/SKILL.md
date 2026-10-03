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
| `web/templates/*.html`, `web/static/*` | `uv run pytest tests/api/ -q --tb=short` (+ E2E, see step 3) |
| `cdk/**/*.ts` | `cd cdk && npx tsc --noEmit && npx cdk synth --no-staging` |
| No match | `uv run pytest -m unit -q --tb=short` |

3. Execute mapped tests.
   - **E2E (`uv run pytest -m e2e`)**: if `web/templates/`, `web/static/`, or `web/routers/` changed, *propose* the fixed browser regression suite (`tests/e2e/`) after the mapped tests pass. API tests cannot see htmx / Alpine behavior on screen. Do **not** run it automatically: it takes ~10 minutes and makes real Bedrock calls, so ask first. Check the preconditions in the `tests/e2e/conftest.py` module docstring before running (app on :8000, Chromium installed, `AWS_REGION` matching `.env`). If the app is not running the suite skips — report that as "skipped", not "passed".

4. Report results:
   - Pass/fail counts
   - Failure analysis (if any)
   - Suggest new tests for uncovered code
