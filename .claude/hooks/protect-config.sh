#!/usr/bin/env bash
set -euo pipefail

input="$(cat)"
file="$(echo "$input" | jq -r '.tool_input.file_path // .tool_input.path // empty')"

PROTECTED="pyproject.toml ruff.toml .ruff.toml mypy.ini setup.cfg tsconfig.json lefthook.yml"

for p in $PROTECTED; do
  case "$file" in
    *"$p")
      echo "BLOCKED: $p is a protected config file. Fix code to conform to rules, not the other way around." >&2
      exit 2
      ;;
  esac
done
