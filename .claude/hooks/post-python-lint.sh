#!/usr/bin/env bash
set -euo pipefail

input="$(cat)"
file="$(echo "$input" | jq -r '.tool_input.file_path // .tool_input.path // empty')"

case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac

# プロジェクトルートへ移動する（uv run はルートの pyproject.toml を必要とする）。
# 絶対パスをハードコードすると作者の環境でしか動かないため、Claude Code が渡す
# CLAUDE_PROJECT_DIR を使い、無い場合はこのスクリプトの位置から辿る。
cd "${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

uv run ruff check --fix "$file" >/dev/null 2>&1 || true
uv run ruff format "$file" >/dev/null 2>&1 || true

diag="$(uv run ruff check "$file" 2>&1 | head -20)"

if [ -n "$diag" ]; then
  jq -Rn --arg msg "$diag" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext: $msg
    }
  }'
fi
