#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 -m py_compile "$HERE/apply_overlay.py" "$HERE/verify_overlay.py" "$HERE/tests/test_overlay_patcher.py"
if python3 -c 'import pytest' >/dev/null 2>&1; then
  (cd "$HERE" && python3 -m pytest -q)
else
  echo "PASS: py_compile (pytest is not installed; installer performs its own post-apply verification)"
fi
