#!/usr/bin/env python3
"""Create a clean source-only export for publishing this MCP server."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

EXCLUDE_DIRS = {".git", "__pycache__", "backups", ".venv", "venv", "artifacts", "screenshots"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".png", ".pdf", ".har", ".trace"}
INCLUDE_ROOT_FILES = {
    "server.py",
    "README.md",
    "SECURITY.md",
    "LICENSE",
    "CHANGELOG.md",
    "requirements.txt",
    "pyproject.toml",
    ".gitignore",
}
INCLUDE_DIRS = {"scripts", "examples", "docs", ".github"}


def should_copy(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return False
    if path.is_dir():
        return rel.parts[0] in INCLUDE_DIRS if rel.parts else True
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    if len(rel.parts) == 1:
        return path.name in INCLUDE_ROOT_FILES
    return rel.parts[0] in INCLUDE_DIRS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dst", default="/tmp/browser-automation-mcp-export")
    args = parser.parse_args()

    src = Path(args.src).resolve()
    dst = Path(args.dst).resolve()
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    copied = []
    for path in src.rglob("*"):
        if not should_copy(path, src) or path.is_dir():
            continue
        rel = path.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(str(rel))

    print(f"exported {len(copied)} files to {dst}")
    for item in copied:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
