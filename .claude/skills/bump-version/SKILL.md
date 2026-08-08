---
name: bump-version
description: This skill should be used when the user asks to "bump version", "update version", "release patch/minor/major", or mentions semantic versioning. Increments the project version.
---

# Version Bump

Increment the project's semantic version.

## Arguments

`$ARGUMENTS`: `major` / `minor` / `patch` (default: `patch`)

## Single Source of Truth

The version lives **only** in `pyproject.toml` (`version = "x.y.z"`). Everything else reads it at runtime:

- `src/__init__.py` — `__version__ = version("ai-persona-system")` (from installed package metadata via `importlib.metadata`)
- `web/main.py` — FastAPI `version=__version__`, `/health` returns `__version__`
- `web/templates/base.html` — footer `v{{ app_version }}` (Jinja global set from `__version__`)

There are **no literal version strings** to replace outside `pyproject.toml`. Do not edit `main.py` or `base.html`.

## Workflow

1. Show the current version: `uv version`

2. Bump and re-lock in one step (default `patch`):

   ```bash
   uv version --bump patch    # or: major / minor
   ```

   This rewrites `pyproject.toml` and updates `uv.lock`.

   Do **not** edit `pyproject.toml` with Edit/Write — a `PreToolUse` hook
   (`.claude/hooks/protect-config.sh`) blocks writes to protected config files.
   `uv version --bump` goes through Bash and is not affected.

3. Confirm the new version: `uv version` and `git diff pyproject.toml uv.lock`

4. Report changes without committing.
