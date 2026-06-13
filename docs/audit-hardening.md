# Audit and Hardening Checklist

Use this checklist before publishing or deploying the Browser Automation MCP server.

## MCP protocol checks

- `initialize` returns `protocolVersion`, `capabilities`, and `serverInfo`.
- `tools/list` returns all advertised tools.
- Every tool in `TOOLS` has a matching entry in `HANDLERS`.
- `tools/call` returns MCP `content: [{type: "text", text: "...json..."}]`.
- Tool failures use `isError: true` with a bounded JSON error payload.

Smoke command:

```bash
python3 scripts/smoke_mcp.py ./server.py
```

## CDP lifecycle checks

- WebSocket connections are closed in `finally` blocks.
- `/json/close/{id}` is treated as plain text, not mandatory JSON.
- Long waits use bounded timeouts.
- Test tabs are closed after smoke/stress tests.
- `tab_id` / `url_filter` are supported on page-specific tools to avoid multi-tab races.

## JavaScript generation checks

- CSS selectors and storage keys are embedded with JSON quoting, not raw string interpolation.
- Promise-based waits use `awaitPromise: true`.
- Tool outputs are bounded where practical.

## Public-safe redaction checks

Defaults should be safe for public tooling:

- `browser_cookies(redact=true)` by default.
- `browser_login(redact=true)` by default.
- `browser_localstorage(redact=true)` by default.
- `browser_sessionstorage(redact=true)` by default.
- `browser_tabs` hides `webSocketDebuggerUrl` without `include_debug_url=true`.
- `browser_newtab` hides `webSocketDebuggerUrl` without `include_debug_url=true`.
- Network tools do not return Cookie/Authorization headers.

## CDP exposure checks

CDP is powerful enough to read browser state. Keep it private:

```bash
curl -s http://127.0.0.1:9223/json/version
```

Confirm the browser is bound to localhost. Do not bind CDP to `0.0.0.0` on public hosts.

## Repository publication checks

Run from a clean export, not a dirty runtime profile:

```bash
python3 scripts/make_release_export.py --dst /tmp/browser-automation-mcp-export
cd /tmp/browser-automation-mcp-export
python3 -m py_compile server.py scripts/smoke_mcp.py scripts/make_release_export.py
python3 scripts/smoke_mcp.py ./server.py
```

Secret scan example:

```bash
python3 - <<'PY'
import re
from pathlib import Path
patterns = {
    'private_key': re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
    'bearer_literal': re.compile(r'Authorization:\s*Bearer\s+[A-Za-z0-9._~+/-]{20,}', re.I),
    'token_assignment': re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*=\s*['\"][^'\"]{16,}['\"]"),
    'sk_key': re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'),
}
findings = []
for path in Path('.').rglob('*'):
    if path.is_file() and '.git' not in path.parts:
        text = path.read_text(errors='ignore')
        for name, rx in patterns.items():
            if rx.search(text):
                findings.append((str(path), name))
if findings:
    raise SystemExit(f'possible secrets: {findings}')
print('secret scan ok')
PY
```

## Stress-test recipe with a live browser

1. Record current tabs with `browser_tabs`.
2. `browser_newtab` to `https://example.com`.
3. Run `browser_snapshot`.
4. Run `browser_screenshot_file`.
5. Run `browser_find_api_calls` with a short duration.
6. Close only the created tab.
7. Verify no extra test tabs remain.
