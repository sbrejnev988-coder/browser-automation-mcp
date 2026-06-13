# Browser Automation MCP

Local/custom MCP server for controlling Chrome/Chromium/Brave through the Chrome DevTools Protocol (CDP). This is **not** the public BrowserMCP package; it is a small stdio MCP server designed for Hermes Agent, Termux/proot, Linux servers, and remote browser hosts.

## Features

- Direct CDP over `websocket-client`, no Node runtime required.
- Navigation, tabs, click/type/wait/scroll/select, form fill and login helpers.
- Agent-friendly element discovery:
  - `browser_elements`
  - `browser_click_text`
  - `browser_wait_text`
- File artifacts instead of huge base64 blobs:
  - `browser_screenshot_file`
  - `browser_pdf_file`
  - `browser_html_file`
- Page observation primitive:
  - `browser_snapshot`
- Lightweight Network/API discovery:
  - `browser_network_log`
  - `browser_find_api_calls`
- Safer defaults:
  - cookies/storage redacted by default;
  - `webSocketDebuggerUrl` hidden unless `include_debug_url=true`;
  - artifact files written under `BROWSER_ARTIFACT_DIR`.

## Requirements

- Python 3.10+
- `websocket-client`
- Running Chrome/Chromium/Brave with CDP enabled, usually on `http://127.0.0.1:9223`

Install dependency:

```bash
python3 -m pip install -r requirements.txt
```

Start browser example:

```bash
chromium-browser \
  --headless=new \
  --no-sandbox \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --user-data-dir=/tmp/browser-automation-profile
```

On Android/Termux/proot, run the browser however your environment supports it, but keep CDP bound to localhost.

## Hermes MCP config

```yaml
mcp_servers:
  browser-automation:
    enabled: true
    command: /usr/bin/python3
    args:
      - /path/to/browser-automation/server.py
    timeout: 120
    connect_timeout: 30
    env:
      BROWSER_CDP_URL: http://127.0.0.1:9223
      BROWSER_TIMEOUT: "30"
      BROWSER_HTTP_TIMEOUT: "10"
      BROWSER_AUTOSTART_CDP: "1"
      BROWSER_ARTIFACT_DIR: /root/.hermes/cache/documents/browser-automation
```

## Tool groups

### Observation

- `browser_health`
- `browser_tabs`
- `browser_page_summary`
- `browser_elements`
- `browser_snapshot`
- `browser_gettext`
- `browser_gethtml`
- `browser_getvalue`

### Interaction

- `browser_navigate`
- `browser_newtab`
- `browser_closetab`
- `browser_click`
- `browser_click_text`
- `browser_type`
- `browser_wait`
- `browser_wait_text`
- `browser_scroll`
- `browser_select`
- `browser_fill_form`
- `browser_login`
- `browser_batch`

### Artifacts

- `browser_screenshot` — base64 PNG
- `browser_screenshot_file` — PNG file path
- `browser_pdf` — base64 PDF
- `browser_pdf_file` — PDF file path
- `browser_html_file` — HTML file path

### Storage/session

- `browser_cookies`
- `browser_localstorage`
- `browser_sessionstorage`

These redact secret-like values by default. Pass `redact=false` only in a trusted local debugging context.

### Network/API discovery

- `browser_network_log` — captures XHR/fetch/JSON-like requests for a bounded time window.
- `browser_find_api_calls` — reloads by default and scores likely API endpoints.

The server intentionally does not return request cookies or authorization headers.

## Documentation

- `docs/api-reference.md` — all 32 MCP tools.
- `docs/server-deployment.md` — Linux/Termux/Hermes deployment.
- `docs/audit-hardening.md` — public-safe audit checklist.
- `docs/github-publication.md` — clean source-only release workflow.
- `CHANGELOG.md` — version history.

## Smoke test

```bash
python3 scripts/smoke_mcp.py ./server.py
```

Optional health test against a running CDP:

```bash
BROWSER_CDP_URL=http://127.0.0.1:9223 python3 scripts/smoke_mcp.py ./server.py --health
```

## Security defaults

- CDP should listen on `127.0.0.1`, not a public interface.
- `browser_tabs` and `browser_newtab` hide `webSocketDebuggerUrl` unless explicitly requested.
- Cookie/storage extraction redacts by default.
- File artifacts are saved locally under `BROWSER_ARTIFACT_DIR`; the tool returns paths, not embedded blobs.

## Repository hygiene

Before publishing:

```bash
python3 -m py_compile server.py scripts/smoke_mcp.py scripts/make_release_export.py
python3 scripts/smoke_mcp.py ./server.py
python3 scripts/make_release_export.py --dst /tmp/browser-automation-mcp-export
cd /tmp/browser-automation-mcp-export
python3 scripts/smoke_mcp.py ./server.py
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
for p in Path('.').rglob('*'):
    if p.is_file() and '.git' not in p.parts:
        text = p.read_text(errors='ignore')
        for name, rx in patterns.items():
            if rx.search(text):
                findings.append((str(p), name))
if findings:
    raise SystemExit(f'possible secrets: {findings}')
print('secret scan ok')
PY
```
