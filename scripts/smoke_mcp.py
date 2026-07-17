#!/usr/bin/env python3
"""Minimal stdio MCP smoke test for browser-automation/server.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict


def send(proc: subprocess.Popen[str], message: Dict[str, Any]) -> Dict[str, Any]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("MCP server closed stdout")
    return json.loads(line)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("server", nargs="?", default="server.py")
    parser.add_argument("--health", action="store_true", help="also call browser_health; requires reachable CDP for ok=true")
    args = parser.parse_args()

    server = Path(args.server).resolve()
    if not server.exists():
        raise SystemExit(f"server not found: {server}")

    # Smoke test must not try to spawn a real browser in CI or package checks.
    env = dict(**__import__("os").environ)
    env.setdefault("BROWSER_AUTOSTART_CDP", "0")

    proc = subprocess.Popen(
        [sys.executable, str(server)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        init = send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        info = init.get("result", {}).get("serverInfo", {})
        if info.get("name") != "browser-automation-mcp":
            raise RuntimeError(f"unexpected serverInfo: {info}")

        tools_resp = send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = tools_resp.get("result", {}).get("tools", [])
        names = {tool.get("name") for tool in tools}
        required = {
            "browser_navigate",
            "browser_elements",
            "browser_click_text",
            "browser_wait_text",
            "browser_screenshot_file",
            "browser_pdf_file",
            "browser_html_file",
            "browser_snapshot",
            "browser_network_log",
            "browser_find_api_calls",
        }
        missing = sorted(required - names)
        if missing:
            raise RuntimeError(f"missing tools: {missing}")

        print(json.dumps({"ok": True, "serverInfo": info, "tool_count": len(tools)}, ensure_ascii=False))

        if args.health:
            health = send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "browser_health", "arguments": {"autostart": False}}})
            print(json.dumps({"health": health.get("result")}, ensure_ascii=False)[:4000])

        return 0
    finally:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
