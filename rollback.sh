#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${1:?Usage: rollback.sh /path/to/browser-automation-mcp [backup-dir]}"
BACKUP="${2:-}"
if [[ -z "$BACKUP" ]]; then
  BACKUP="$(find "$REPO/.overlay-backups" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -n1)"
fi
[[ -n "$BACKUP" && -f "$BACKUP/server.py" ]] || { echo "Backup not found" >&2; exit 2; }
cp -a "$BACKUP/server.py" "$REPO/server.py"
python3 -m py_compile "$REPO/server.py"
echo "Restored: $BACKUP/server.py"
