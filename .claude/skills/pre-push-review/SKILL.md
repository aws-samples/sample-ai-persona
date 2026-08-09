---
name: pre-push-review
description: This skill should be used when the user asks to "review before push", "pre-push check", "is this ready to push", or mentions push readiness. Runs code quality, security, test coverage, dead-code, AWS safety, and documentation checks.
---

# Pre-Push Review

Compound review gate covering quality, security, coverage, dead code, AWS safety, and documentation consistency.

## Workflow

1. **Scope detection:**
   - `git fetch origin main` first — the diff below uses the merge-base with `origin/main`, so a stale local ref would include already-merged commits or miss new ones, shifting every downstream LLM-judged step (3/4/6/8).
   - `git diff origin/main...HEAD` to identify changed files.

2. **Code quality:**
   - `uv run ruff check .`
   - `uv run mypy src/ web/`

3. **Architecture violation check:**
   - Dependency direction is machine-checked (deterministic, **FAIL**): `uv run pytest tests/unit/test_architecture_deps.py -q`. This AST-scans every layer for reverse imports (ratchet with `_BASELINE`); a new violation fails the run. Do not re-judge dependency direction by eye — the test is the source of truth.
   - Responsibility placement (Manager doing I/O, Service holding business rules, etc.) is **not** statically checkable; apply the WARN criteria in [references/architecture-violations.md](references/architecture-violations.md) to changed Python files as advisory findings, not FAILs.

4. **Security review** — run the built-in `/security-review` on the pending changes (it targets the current branch, so it works pre-push without a PR). This is Anthropic's maintained security auditor; prefer it over hand-applying a static checklist. Report its findings as advisory (**WARN**), not as a deterministic gate. Use [references/security-review.md](references/security-review.md) only as a fallback when `/security-review` is unavailable. The deterministic parts (exception leakage to responses, `traceback` in routers) are separately enforced by `tests/api/test_error_exposure.py` — run `uv run pytest tests/api/test_error_exposure.py -q` if router error-handling changed.

5. **Test coverage:**
   - The `unit` marker only exercises `src/managers/` (marker mapping: `unit`→`src/managers`, `integration`→`src/services`, `api`→`web/routers`). Measure coverage of that scope, not all of `src/` — `--cov=src` under `-m unit` would undercount `services`/`models` and make the number meaningless.
   - `uv run pytest -m unit --cov=src/managers --cov-report=term-missing -q`
   - Flag if `src/managers` coverage < 70% (skill-local threshold; not wired to `pyproject.toml`).

6. **AWS safety** (only if CDK/infra files changed):
   - Primary signal — run the tool, don't rely on memory: `cd cdk && npx cdk diff`. Any resource marked `[-]`/`[+]` for the same logical id (replacement) is a **data-loss FAIL** for stateful resources (DynamoDB, S3, Cognito user pool). This is deterministic; prefer it over static reasoning.
   - If `cdk diff` cannot run (no AWS creds / synth-only context), fall back to the static criteria in [references/aws-safety.md](references/aws-safety.md) and report that the authoritative check was skipped — do not present the fallback as equivalent.

7. **CDK best practices** (only if CDK files changed):
   - Apply `/cdk-best-practices` skill criteria to changed CDK code.

8. **Documentation consistency:**
   - Check if code changes require doc updates per `/update-docs` criteria.

9. **Dead code check** (advisory, **WARN** — never auto-delete):
   - `ruff` (F401 unused import, F841 unused local) is already run in step 2 and catches the trivial cases. It does **not** catch a module-level symbol (function / method / class / constant) that is only referenced by tests, because tests are real references. That gap is what this step covers by hand.
   - For each **public symbol added, renamed, or left behind by a refactor** in the diff (functions, methods, class attributes, `ErrorCode` members, catalog entries), count non-definition references in **production code only** (`src/`, `web/`), excluding the file that defines it:
     `rg -n "\.<symbol>\(|<SYMBOL>" src web -g '!<defining-file>'`
   - **WARN** when a symbol has **zero production references and is reachable only from `tests/`** — that is the dead-code signature (a refactor moved the caller to a new module but left the old callee and its tests behind). Also trace symbols the *removed* lines used to reference, so a deletion does not leave orphaned constants, error-code enum members, or message-catalog entries the deleted code was the sole user of.
   - Before concluding dead, confirm there is no dynamic access (`getattr`, template globals, string-dispatch, `__all__`) and check `git log -S "<symbol>"` to see whether the caller was dropped by an earlier refactor.
   - Report findings as advisory only. **Deletion is a separate, human-approved step** — a review gate must not perform destructive edits. If removing, delete the symbol, its tests, and any now-orphaned constants / error codes / catalog entries together, then re-run steps 2 and 5.

10. **Adversarial code review** (for substantive diffs — new Router/Manager, auth/IAM, external input, or anything the steps above flagged):
   - Run `/code-review` for a general independent pass over the subjective checks (responsibility placement step 3, docs step 8, general bugs). Security specifically is already covered by `/security-review` in step 4 — this is the broader review.
   - Note: `/code-review` operates on a pull request (`gh pr`), so it fits *after* a PR exists. When running strictly pre-push with no PR yet, rely on step 4 (`/security-review`, which works on the branch) and defer `/code-review` to PR time.
   - Skip only for trivial diffs (typos, comments, single-line mechanical edits).

11. **Version consistency:**
   - The version has a single source: `pyproject.toml` (see `/bump-version`). `src/__init__.py`, `web/main.py`, and `web/templates/base.html` read it at runtime.
   - Flag if changed code introduces a hardcoded version literal outside `pyproject.toml` (it would drift from the single source).

12. **Summary** — report deterministic results and advisory findings separately so they are not conflated:
   - **Machine checks** (steps 2, 3-dependency, 5, 6-cdk-diff, error-exposure tests): each is PASS or **FAIL** with a reproducible command. A FAIL blocks push.
   - **Advisory findings** (steps 3-responsibility, 4, 8, 9-dead-code, 10): **WARN** with rationale; reviewer decides. Never present these as FAIL, and never present a passing advisory step as a guarantee.
   - Overall: **FAIL** if any machine check failed; otherwise PASS with any WARNs listed.

## Additional Resources

### Reference Files

- **[`references/architecture-violations.md`](references/architecture-violations.md)** — Layered architecture dependency and constraint checks
- **[`references/security-review.md`](references/security-review.md)** — Security review criteria for code changes
- **[`references/aws-safety.md`](references/aws-safety.md)** — AWS resource safety checks for infrastructure changes
