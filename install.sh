#!/usr/bin/env bash
set -Eeuo pipefail

BROWSER_REPO="${1:-${BROWSER_AUTOMATION_REPO:-}}"
MEMORY_REPO="${2:-${MEMORY_WIKI_REPO:-}}"

if [[ -z "$BROWSER_REPO" || -z "$MEMORY_REPO" ]]; then
  echo "Usage: $0 /path/to/browser-automation-mcp /path/to/hermes-memory-wiki" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/apply_overlay.py" --browser-repo "$BROWSER_REPO" --memory-repo "$MEMORY_REPO"
python3 "$HERE/verify_overlay.py" --browser-repo "$BROWSER_REPO" --memory-repo "$MEMORY_REPO"

echo
echo "Overlay applied. Restart Hermes/MCP processes to load the updated server.py."
