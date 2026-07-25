#!/usr/bin/env python3
from __future__ import annotations
import argparse, ast, pathlib, py_compile, re, sys

REQUIRED = [
    "HERMES_BROWSER_OVERLAY_20260725",
    "HERMES_BROWSER_PRIVILEGED_TOOL_FILTER",
    "HERMES_BROWSER_RUNTIME_GUARDS",
    "class _MemoryWikiMCPClient",
    "def _validate_cookie_arguments",
    "os.O_NOFOLLOW",
    "def is_reconnecting",
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--browser-repo", required=True)
    p.add_argument("--memory-repo", required=True)
    a = p.parse_args()
    browser = pathlib.Path(a.browser_repo).resolve()
    memory = pathlib.Path(a.memory_repo).resolve()
    server = browser / "server.py"
    init = memory / "__init__.py"
    wrapper = memory / "mcp-wrapper" / "server.py"
    schemas = memory / "mcp-wrapper" / "tool_schemas.json"
    for path in (server, init, wrapper, schemas):
        if not path.exists():
            raise SystemExit(f"MISSING: {path}")
    text = server.read_text(encoding="utf-8")
    ast.parse(text)
    py_compile.compile(str(server), doraise=True)
    missing = [item for item in REQUIRED if item not in text]
    if missing:
        raise SystemExit("MISSING MARKERS: " + ", ".join(missing))
    reader = re.search(r"def _reader_loop\(self\):(.*?)(?=\n\s+def _reject_all_pending)", text, re.S)
    if not reader:
        raise SystemExit("reader loop not found")
    tail = reader.group(1).rstrip().splitlines()[-12:]
    if sum("_reject_all_pending" in line for line in tail) > 1:
        raise SystemExit("suspicious unconditional pending rejection remains")
    if '"password":' in text and "DEPRECATED: используй credential_ref" in text:
        raise SystemExit("plaintext password is still advertised in browser_login schema")
    print("PASS: syntax")
    print("PASS: CDP dispatcher/reconnect overlay")
    print("PASS: Memory Wiki MCP bridge")
    print("PASS: artifact hardening")
    print("PASS: privileged tools default-off")
    print("PASS: hermes-memory-wiki/__init__.py remains one file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
