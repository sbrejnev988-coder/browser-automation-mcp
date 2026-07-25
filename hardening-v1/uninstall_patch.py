#!/usr/bin/env python3
from __future__ import annotations
import argparse
import re
from pathlib import Path


def remove_block(text: str, begin: str, end: str) -> str:
    pattern = re.compile(r"\n?" + re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.S)
    return pattern.sub("\n", text)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--browser-root", required=True, type=Path)
    p.add_argument("--memory-root", required=True, type=Path)
    a = p.parse_args()
    bs = a.browser_root.expanduser().resolve() / "server.py"
    mi = a.memory_root.expanduser().resolve() / "__init__.py"
    bs.write_text(remove_block(bs.read_text(encoding="utf-8"), "# BEGIN HERMES_BROWSER_SECURITY_OVERLAY_V1", "# END HERMES_BROWSER_SECURITY_OVERLAY_V1"), encoding="utf-8")
    mi.write_text(remove_block(mi.read_text(encoding="utf-8"), "# BEGIN HERMES_MEMORY_BROWSER_OVERLAY_V1", "# END HERMES_MEMORY_BROWSER_OVERLAY_V1"), encoding="utf-8")
    (a.browser_root / "browser_security_overlay.py").unlink(missing_ok=True)
    (a.memory_root / "memory_wiki_browser_overlay.py").unlink(missing_ok=True)
    print("overlay blocks removed; timestamped backups were kept")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
