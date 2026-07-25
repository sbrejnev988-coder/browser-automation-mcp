#!/usr/bin/env python3
from __future__ import annotations
import argparse
import importlib.util
import py_compile
import sys
from pathlib import Path


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--browser-root", required=True, type=Path)
    p.add_argument("--memory-root", required=True, type=Path)
    a = p.parse_args()
    br, mr = a.browser_root.expanduser().resolve(), a.memory_root.expanduser().resolve()
    files = [br / "server.py", br / "browser_security_overlay.py", mr / "__init__.py", mr / "memory_wiki_browser_overlay.py"]
    for f in files:
        if not f.is_file():
            print(f"FAIL missing {f}")
            return 1
        py_compile.compile(str(f), doraise=True)
    if "HERMES_BROWSER_SECURITY_OVERLAY_V1" not in (br / "server.py").read_text(encoding="utf-8"):
        print("FAIL browser marker missing")
        return 1
    if "HERMES_MEMORY_BROWSER_OVERLAY_V1" not in (mr / "__init__.py").read_text(encoding="utf-8"):
        print("FAIL memory marker missing")
        return 1
    b = load(br / "browser_security_overlay.py", "verify_browser_overlay")
    m = load(mr / "memory_wiki_browser_overlay.py", "verify_memory_overlay")
    assert b.OVERLAY_VERSION and m.OVERLAY_VERSION
    print("PASS syntax and overlay markers")
    print("PASS memory __init__.py remains a single file")
    print("PASS browser policy + durable bridge helpers load")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
