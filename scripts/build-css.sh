#!/bin/bash
# Tailwind CSS ビルドスクリプト
# 使い方:
#   ビルド:     ./scripts/build-css.sh
#   ウォッチ:   ./scripts/build-css.sh --watch
#   minify:     ./scripts/build-css.sh --minify

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TAILWIND_BIN="$PROJECT_ROOT/tailwindcss"
INPUT="$PROJECT_ROOT/web/static/css/tailwind-input.css"
OUTPUT="$PROJECT_ROOT/web/static/css/tailwind.css"

if [ ! -f "$TAILWIND_BIN" ]; then
    echo "Error: tailwindcss binary not found at $TAILWIND_BIN"
    echo "Download it from: https://github.com/tailwindlabs/tailwindcss/releases"
    exit 1
fi

echo "Building Tailwind CSS..."
"$TAILWIND_BIN" -i "$INPUT" -o "$OUTPUT" "$@"
echo "Done: $OUTPUT"
