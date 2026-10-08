#!/usr/bin/env bash
set -euo pipefail

input="$(cat)"
cmd="$(echo "$input" | jq -r '.tool_input.command // empty')"

if echo "$cmd" | grep -qiE '(rm\s+-rf\s+(/|\.|src|web|cdk|tests))|git\s+push\s+(-f|--force)|git\s+reset\s+--hard|git\s+clean\s+-f|aws\s+dynamodb\s+delete-table|aws\s+s3\s+rb|aws\s+s3\s+rm\s+.*--recursive|aws\s+cognito-idp\s+delete-user-pool|cdk\s+destroy|destroy\.sh|DROP\s+(TABLE|DATABASE)|RemovalPolicy\.DESTROY'; then
  echo "BLOCKED: Dangerous command detected. This operation requires explicit user confirmation outside of Claude Code." >&2
  exit 2
fi
