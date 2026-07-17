# GitHub Publication Workflow

This repository should be published from a clean source-only export.

## Build export

```bash
python3 scripts/make_release_export.py --dst /tmp/browser-automation-mcp-export
```

The export includes only public source/docs/scripts/examples and excludes:

- `.env` files
- backups
- browser profiles
- generated screenshots/PDF/HAR/trace artifacts
- `__pycache__`
- runtime logs/databases/cache

## Verify export

```bash
cd /tmp/browser-automation-mcp-export
python3 -m py_compile server.py scripts/smoke_mcp.py scripts/make_release_export.py
python3 scripts/smoke_mcp.py ./server.py
```

## Publish/update existing repo

```bash
git clone https://github.com/<owner>/browser-automation-mcp.git
cd browser-automation-mcp
python3 /path/to/source/scripts/make_release_export.py --dst /tmp/browser-automation-mcp-export
python3 - <<'PY'
import shutil
from pathlib import Path
src = Path('/tmp/browser-automation-mcp-export')
dst = Path('.')
for item in src.rglob('*'):
    if item.is_file():
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
PY
python3 scripts/smoke_mcp.py ./server.py
git status --short
git add -A
git commit -m "Refresh browser automation MCP release"
git push
```

Do not embed access tokens in command lines or remote URLs.
