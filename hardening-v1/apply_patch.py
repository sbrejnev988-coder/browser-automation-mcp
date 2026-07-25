#!/usr/bin/env python3
"""Apply browser + Memory-Wiki overlay patches with backups and rollback."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import py_compile
import re
import shutil
import sys
from pathlib import Path

BROWSER_BEGIN = "# BEGIN HERMES_BROWSER_SECURITY_OVERLAY_V1"
BROWSER_END = "# END HERMES_BROWSER_SECURITY_OVERLAY_V1"
MEMORY_BEGIN = "# BEGIN HERMES_MEMORY_BROWSER_OVERLAY_V1"
MEMORY_END = "# END HERMES_MEMORY_BROWSER_OVERLAY_V1"

BROWSER_BLOCK = f'''\n{BROWSER_BEGIN}\nimport browser_security_overlay as _hba_security_overlay\n_hba_security_overlay.install(globals(), strict=True)\n{BROWSER_END}\n'''
MEMORY_BLOCK = f'''\n{MEMORY_BEGIN}\nimport memory_wiki_browser_overlay as _hmw_browser_overlay\n_hmw_browser_overlay.install(globals(), strict=True)\n{MEMORY_END}\n'''


def backup(path: Path, stamp: str) -> Path:
    target = path.with_name(path.name + f".bak_browser_memory_{stamp}")
    shutil.copy2(path, target)
    return target


def inject_before_main(text: str, block: str) -> str:
    marker = re.search(r"(?m)^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", text)
    if marker:
        return text[: marker.start()] + block + "\n" + text[marker.start() :]
    return text.rstrip() + "\n" + block


def compile_files(paths: list[Path]) -> None:
    for path in paths:
        py_compile.compile(str(path), doraise=True)


def apply(browser_root: Path, memory_root: Path, dry_run: bool = False) -> None:
    package_root = Path(__file__).resolve().parent
    browser_server = browser_root / "server.py"
    memory_init = memory_root / "__init__.py"
    if not browser_server.is_file():
        raise FileNotFoundError(f"browser server not found: {browser_server}")
    if not memory_init.is_file():
        raise FileNotFoundError(f"memory __init__.py not found: {memory_init}")

    browser_text = browser_server.read_text(encoding="utf-8")
    memory_text = memory_init.read_text(encoding="utf-8")
    required_browser = ("def _persist_to_wiki", "def _recall_from_wiki", '"browser_cookie_list"', '"browser_exec"')
    required_memory = ("class MemoryWikiProvider", "def _ingest_text")
    missing = [x for x in required_browser if x not in browser_text] + [x for x in required_memory if x not in memory_text]
    if missing:
        raise RuntimeError("unsupported repository revision; missing anchors: " + ", ".join(missing))

    if BROWSER_BEGIN not in browser_text:
        browser_text = inject_before_main(browser_text, BROWSER_BLOCK)
    if MEMORY_BEGIN not in memory_text:
        memory_text = memory_text.rstrip() + "\n" + MEMORY_BLOCK

    print(f"browser: {browser_server}")
    print(f"memory:  {memory_init}")
    print("memory __init__.py remains one monolithic file; no source sections are moved")
    if dry_run:
        print("dry-run: no files changed")
        return

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    browser_backup = backup(browser_server, stamp)
    memory_backup = backup(memory_init, stamp)
    helper_browser = browser_root / "browser_security_overlay.py"
    helper_memory = memory_root / "memory_wiki_browser_overlay.py"
    prior_helper_browser = helper_browser.read_bytes() if helper_browser.exists() else None
    prior_helper_memory = helper_memory.read_bytes() if helper_memory.exists() else None

    try:
        shutil.copy2(package_root / "overlays" / "browser_security_overlay.py", helper_browser)
        shutil.copy2(package_root / "overlays" / "memory_wiki_browser_overlay.py", helper_memory)
        browser_server.write_text(browser_text, encoding="utf-8")
        memory_init.write_text(memory_text, encoding="utf-8")
        compile_files([browser_server, memory_init, helper_browser, helper_memory])
    except Exception:
        shutil.copy2(browser_backup, browser_server)
        shutil.copy2(memory_backup, memory_init)
        if prior_helper_browser is None:
            helper_browser.unlink(missing_ok=True)
        else:
            helper_browser.write_bytes(prior_helper_browser)
        if prior_helper_memory is None:
            helper_memory.unlink(missing_ok=True)
        else:
            helper_memory.write_bytes(prior_helper_memory)
        raise

    print("PATCH APPLIED")
    print(f"backup: {browser_backup}")
    print(f"backup: {memory_backup}")
    print("Run: python3 verify_patch.py --browser-root ... --memory-root ...")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-root", required=True, type=Path)
    parser.add_argument("--memory-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        apply(args.browser_root.expanduser().resolve(), args.memory_root.expanduser().resolve(), args.dry_run)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
