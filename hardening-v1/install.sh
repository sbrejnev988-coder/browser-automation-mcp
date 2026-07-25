#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "Usage: $0 /path/to/browser-automation-mcp /path/to/hermes-memory-wiki"
}

BROWSER_ROOT="${1:-}"
MEMORY_ROOT="${2:-}"
if [[ -z "$BROWSER_ROOT" || -z "$MEMORY_ROOT" ]]; then
  usage
  exit 2
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
python3 "$SCRIPT_DIR/apply_patch.py" \
  --browser-root "$BROWSER_ROOT" \
  --memory-root "$MEMORY_ROOT"
python3 "$SCRIPT_DIR/verify_patch.py" \
  --browser-root "$BROWSER_ROOT" \
  --memory-root "$MEMORY_ROOT"
