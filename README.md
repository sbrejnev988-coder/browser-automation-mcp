# Browser Automation MCP

> **Version 1.5.1** — Full-featured MCP server for Chrome/Chromium/Brave automation via Chrome DevTools Protocol (CDP).  
> **40 MCP tools** — navigation, interaction, DOM extraction, network discovery, screenshots, PDFs, storage, batch execution, raw CDP passthrough.  
> **Zero Node.js dependency** — pure Python (`websocket-client` + stdlib), runs on Linux, Termux/proot, macOS, WSL, remote servers.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture](#architecture)
3. [Installation](#installation)
4. [Browser Setup](#browser-setup)
5. [Hermes Configuration](#hermes-configuration)
6. [Environment Variables](#environment-variables)
7. [Complete Tool Reference](#complete-tool-reference)
   - [Observation](#observation)
   - [Navigation & Interaction](#navigation--interaction)
   - [DOM & Data Extraction](#dom--data-extraction)
   - [Storage & Session](#storage--session)
   - [Artifacts](#artifacts)
   - [Network & API Discovery](#network--api-discovery)
   - [Raw CDP](#raw-cdp)
8. [Batch Execution](#batch-execution)
9. [Security Model](#security-model)
10. [Remote Browser (SSH Wrapper)](#remote-browser-ssh-wrapper)
11. [Headless RDP / Windows Setup](#headless-rdp--windows-setup)
12. [Troubleshooting](#troubleshooting)
13. [Development](#development)
14. [Repository Hygiene](#repository-hygiene)

---

## Quick Start

```bash
# 1. Install
git clone <repo> browser-automation-mcp
cd browser-automation-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Start a browser with CDP
chromium --headless=new --no-sandbox \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --user-data-dir=/tmp/browser-profile &

# 3. Verify CDP
curl -s http://127.0.0.1:9223/json/version | python3 -m json.tool

# 4. Smoke test
python3 scripts/smoke_mcp.py ./server.py
BROWSER_CDP_URL=http://127.0.0.1:9223 python3 scripts/smoke_mcp.py ./server.py --health

# 5. Add to Hermes config (see below)
```

---

## Architecture

```
┌──────────┐   stdio JSON-RPC    ┌──────────────┐   WebSocket CDP   ┌──────────┐
│  Hermes  │ ◄──────────────────► │  server.py   │ ◄───────────────► │  Chrome  │
│  Agent   │    MCP protocol      │  (Python)    │   /devtools/page │  / Brave  │
└──────────┘                      └──────────────┘                   └──────────┘
                                        │
                                        ▼ HTTP GET /json/*
                                  ┌──────────┐
                                  │ CDP HTTP │
                                  │ :9223    │
                                  └──────────┘
```

- **Transport:** stdio JSON-RPC (MCP protocol) between Hermes and `server.py`
- **Browser control:** WebSocket CDP (`/devtools/page/<id>`) for all page operations
- **Browser discovery:** HTTP `GET /json/version`, `GET /json/list` for tab enumeration
- **Single dependency:** `websocket-client` (~300KB). No Selenium, no Puppeteer, no Node.js.

### Connection Lifecycle

1. `server.py` starts, listens on stdio for MCP `initialize`
2. On first tool call, connects to CDP HTTP endpoint (`/json/version` health check)
3. Opens WebSocket to `/devtools/page/<targetId>` for each page operation
4. WebSocket connections are reused per-target within the MCP session
5. Auto-reconnects if CDP browser crashes (with `BROWSER_AUTOSTART_CDP=1`)

---

## Installation

### Requirements

| Dependency | Version | Purpose |
|---|---|---|
| Python | ≥ 3.10 | Runtime |
| `websocket-client` | ≥ 1.6 | CDP WebSocket communication |
| Chrome / Chromium / Brave | Any recent (≥ 120) | Browser with CDP |

### From Source

```bash
git clone <repo-url> browser-automation-mcp
cd browser-automation-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### On Android/Termux (proot Ubuntu)

```bash
# Inside proot:
apt-get install -y python3 python3-pip chromium-browser
cd browser-automation-mcp
pip install --break-system-packages websocket-client

# Chromium needs --no-sandbox in proot
chromium-browser --headless=new --no-sandbox \
  --disable-dev-shm-usage \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --user-data-dir=/tmp/chromium-profile &
```

### On Remote VPS (Ubuntu 24.04)

```bash
apt-get install -y python3-pip chromium-browser
pip install --break-system-packages websocket-client
# Or with venv:
python3 -m venv /opt/browser-automation/venv
/opt/browser-automation/venv/bin/pip install websocket-client
```

---

## Browser Setup

### Chrome / Chromium (headless)

```bash
chromium-browser \
  --headless=new \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --remote-allow-origins=* \
  --user-data-dir=/tmp/browser-automation-profile \
  --window-size=1920,1080
```

### Brave Browser

```bash
brave-browser \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --remote-allow-origins=* \
  --no-first-run \
  --no-default-browser-check \
  --disable-search-engine-choice-screen \
  --user-data-dir=/tmp/brave-profile
```

### Systemd Service (auto-start on boot)

```ini
# ~/.config/systemd/user/browser-cdp.service
[Unit]
Description=Chrome CDP for Browser Automation MCP
After=network.target

[Service]
ExecStart=/usr/bin/chromium-browser \
  --headless=new --no-sandbox --disable-dev-shm-usage \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 \
  --remote-allow-origins=* \
  --user-data-dir=/tmp/browser-automation-profile
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now browser-cdp.service
systemctl --user status browser-cdp.service
```

### Verify CDP

```bash
curl -s http://127.0.0.1:9223/json/version
# Expected: {"Browser": "Chrome/...", "webSocketDebuggerUrl": "ws://..."}

curl -s http://127.0.0.1:9223/json/list | python3 -m json.tool
# Expected: [{"type": "page", "url": "about:blank", ...}, ...]
```

---

## Hermes Configuration

### Local Browser

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  browser-automation:
    enabled: true
    command: /path/to/.venv/bin/python3
    args:
      - /path/to/browser-automation-mcp/server.py
    timeout: 120
    connect_timeout: 30
    env:
      BROWSER_CDP_URL: http://127.0.0.1:9223
      BROWSER_TIMEOUT: "30"
      BROWSER_HTTP_TIMEOUT: "10"
      BROWSER_AUTOSTART_CDP: "1"
      BROWSER_CDP_START_CMD: /home/user/.local/bin/browser-cdp-start
      BROWSER_ARTIFACT_DIR: /root/.hermes/cache/documents/browser-automation
```

### Remote Browser (SSH wrapper)

```yaml
mcp_servers:
  browser-automation:
    enabled: true
    command: /home/user/.local/bin/browser-mcp-wrapper
    timeout: 120
    connect_timeout: 30
```

Wrapper script (`~/.local/bin/browser-mcp-wrapper`):

```bash
#!/usr/bin/env bash
set -euo pipefail
exec ssh -i /path/to/key -o BatchMode=yes -o ConnectTimeout=12 \
  user@remote-host \
  'BROWSER_CDP_URL=http://127.0.0.1:9223 exec /home/user/.local/bin/browser-mcp'
```

### Multiple Profiles (multi-user)

```yaml
mcp_servers:
  browser-automation-profile-a:
    enabled: true
    command: /usr/bin/python3
    args: [/path/to/server.py]
    env:
      BROWSER_CDP_URL: http://127.0.0.1:9223
      BROWSER_ARTIFACT_DIR: /root/.hermes/cache/documents/browser-automation/profile-a

  browser-automation-profile-b:
    enabled: true
    command: /usr/bin/python3
    args: [/path/to/server.py]
    env:
      BROWSER_CDP_URL: http://127.0.0.1:9224
      BROWSER_ARTIFACT_DIR: /root/.hermes/cache/documents/browser-automation/profile-b
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BROWSER_CDP_URL` | `http://127.0.0.1:9223` | CDP HTTP endpoint |
| `BROWSER_TIMEOUT` | `30` | WebSocket command timeout (seconds) |
| `BROWSER_HTTP_TIMEOUT` | `10` | CDP HTTP request timeout (seconds) |
| `BROWSER_AUTOSTART_CDP` | `1` | Auto-start browser via `BROWSER_CDP_START_CMD` if CDP is down |
| `BROWSER_CDP_START_CMD` | `~/.local/bin/browser-cdp-start` | Command to start browser+CDP |
| `BROWSER_ARTIFACT_DIR` | `~/.hermes/cache/documents/browser-automation` | Output directory for screenshots, PDFs, HTML |
| `BROWSER_AUTH_TOKEN` | *(empty)* | Optional Bearer token for protected CDP endpoints |
| `BROWSER_CDP_RETRY_COUNT` | `3` | Number of CDP WebSocket reconnection attempts |
| `BROWSER_CDP_RETRY_DELAY` | `1.0` | Delay between CDP reconnection attempts (seconds) |

---

## Complete Tool Reference

### Observation

Tools for understanding page state without mutating it.

#### `browser_health`
Check MCP server version, CDP reachability, browser version, active tabs.

```json
{"name": "browser_health", "arguments": {"autostart": false, "include_metrics": true}}
```

Response includes: `mcp_version`, `cdp_reachable`, `browser_version`, `user_agent`, `tab_count`, `cdp_latency_ms`.

#### `browser_tabs`
List all open tabs/targets.

```json
{"name": "browser_tabs", "arguments": {"include_non_page": false, "include_debug_url": false}}
```

`webSocketDebuggerUrl` is hidden unless `include_debug_url=true` (security default).

#### `browser_page_summary`
Quick structural overview: title, URL, forms, links, buttons, images.

```json
{"name": "browser_page_summary", "arguments": {"max_text": 4000, "max_items": 30, "tab_id": "..."}}
```

#### `browser_elements`
Discover interactive/visible elements with stable selectors, text, ARIA roles, values, and bounding rectangles.

```json
{"name": "browser_elements", "arguments": {"kind": "clickable", "query": "submit", "max_items": 20}}
```

`kind`: `all` | `clickable` | `input` | `link` | `button` | `select`

Sensitive input values (password/token/auth) are redacted.

#### `browser_snapshot`
**Recommended first observation primitive.** Combines summary, interactive elements, and optional screenshot.

```json
{"name": "browser_snapshot", "arguments": {"max_text": 4000, "max_items": 40, "include_screenshot": true}}
```

### Navigation & Interaction

#### `browser_navigate`
Navigate to URL. Returns title, URL, tab_id.

```json
{"name": "browser_navigate", "arguments": {"url": "https://example.com", "wait_until_ready": true}}
```

Optional `persist_to_wiki=true` saves the page to memory-wiki.

#### `browser_newtab`
Open a new tab.

```json
{"name": "browser_newtab", "arguments": {"url": "https://example.com", "wait_until_ready": true}}
```

#### `browser_closetab`
Close a tab by `tab_id` or `url_filter`.

```json
{"name": "browser_closetab", "arguments": {"tab_id": "ABC123..."}}
{"name": "browser_closetab", "arguments": {"url_filter": "example.com"}}
```

#### `browser_session_tabs`
Manage tabs: `list`, `switch`, `close_all`, `close_others`.

```json
{"name": "browser_session_tabs", "arguments": {"action": "list"}}
```

#### `browser_click`
Click by CSS selector or `selector_pack` (self-healing).

```json
{"name": "browser_click", "arguments": {"selector": "button.primary"}}
{"name": "browser_click", "arguments": {"selector_pack": {"primary": "#submit", "text": "Submit", "aria": "submit form"}}}
```

#### `browser_click_text`
Click element by visible text — no CSS selector needed.

```json
{"name": "browser_click_text", "arguments": {"text": "Sign In", "exact": true, "index": 0}}
```

#### `browser_click_heal`
Self-healing click: tries multiple strategies (text, CSS, ARIA, XPath fallback).

```json
{"name": "browser_click_heal", "arguments": {"text": "Continue", "selector_pack": {"primary": "#continue"}}}
```

#### `browser_type`
Type text into an input field.

```json
{"name": "browser_type", "arguments": {"selector": "#email", "text": "user@example.com", "clear_first": true}}
```

#### `browser_wait`
Wait for a CSS selector to appear.

```json
{"name": "browser_wait", "arguments": {"selector": ".loaded", "timeout_ms": 10000}}
```

#### `browser_wait_text`
Wait for visible text in DOM.

```json
{"name": "browser_wait_text", "arguments": {"text": "Success", "timeout_ms": 10000, "case_sensitive": false}}
```

#### `browser_scroll`
Scroll page or element.

```json
{"name": "browser_scroll", "arguments": {"direction": "down", "amount": 800}}
{"name": "browser_scroll", "arguments": {"direction": "bottom"}}
```

Directions: `down` | `up` | `top` | `bottom`. Optional `selector` scrolls a specific element.

#### `browser_select`
Select an option in a `<select>` element.

```json
{"name": "browser_select", "arguments": {"selector": "select[name=country]", "label": "Germany"}}
{"name": "browser_select", "arguments": {"selector": "#size", "value": "xl"}}
{"name": "browser_select", "arguments": {"selector": "#size", "index": 2}}
```

#### `browser_fill_form`
Fill multiple fields and optionally submit.

```json
{"name": "browser_fill_form", "arguments": {
  "fields": {"#name": "John", "#email": "john@example.com", "#message": "Hello"},
  "submit_selector": "button[type=submit]"
}}
```

#### `browser_login`
Navigate → fill credentials → submit → return cookies.

```json
{"name": "browser_login", "arguments": {
  "url": "https://example.com/login",
  "username_selector": "#email",
  "password_selector": "#password",
  "submit_selector": "button[type=submit]",
  "username": "user@example.com",
  "password": "hunter2",
  "redact": true
}}
```

### DOM & Data Extraction

#### `browser_exec`
Execute JavaScript in the page context.

```json
{"name": "browser_exec", "arguments": {"expression": "document.title"}}
{"name": "browser_exec", "arguments": {"expression": "fetch('/api/data').then(r => r.json())", "await_promise": true}}
```

Access to full DOM: `window`, `document`, `fetch`, `localStorage`, etc.

#### `browser_gettext`
Get `innerText` of elements.

```json
{"name": "browser_gettext", "arguments": {"selector": "article"}}
{"name": "browser_gettext", "arguments": {"selector": "p", "all": true}}
```

#### `browser_gethtml`
Get `outerHTML` or `innerHTML`.

```json
{"name": "browser_gethtml", "arguments": {"selector": "#content", "outer": true}}
```

#### `browser_getvalue`
Get the current `value` of an input.

```json
{"name": "browser_getvalue", "arguments": {"selector": "input[name=q]"}}
```

### Storage & Session

#### `browser_cookies`
Extract cookies, `document.cookie`, localStorage, and sessionStorage.

```json
{"name": "browser_cookies", "arguments": {"redact": true, "tab_id": "..."}}
```

Secret-like values (passwords, tokens, API keys) are redacted by default.

#### `browser_localstorage`
Read localStorage.

```json
{"name": "browser_localstorage", "arguments": {"key": "auth_token", "redact": true}}
```

If `key` is omitted, returns all keys.

#### `browser_sessionstorage`
Read sessionStorage (same interface as localStorage).

```json
{"name": "browser_sessionstorage", "arguments": {"redact": true}}
```

### Artifacts

#### `browser_screenshot`
Take a screenshot, return as base64 PNG.

```json
{"name": "browser_screenshot", "arguments": {"full_page": true}}
```

Prefer `browser_screenshot_file` for chat/CI — base64 is heavy in MCP messages.

#### `browser_screenshot_file`
Save screenshot as PNG file, return `{path, bytes, media_hint}`.

```json
{"name": "browser_screenshot_file", "arguments": {"full_page": true, "filename_prefix": "evidence"}}
```

Files saved under `BROWSER_ARTIFACT_DIR`. Set `persist_to_wiki=true` to index in memory-wiki.

#### `browser_pdf`
Save page as PDF, return base64.

```json
{"name": "browser_pdf", "arguments": {}}
```

#### `browser_pdf_file`
Save PDF to file.

```json
{"name": "browser_pdf_file", "arguments": {"filename_prefix": "invoice"}}
```

#### `browser_html_file`
Save current DOM HTML to file.

```json
{"name": "browser_html_file", "arguments": {"filename_prefix": "page-snapshot"}}
```

### Network & API Discovery

#### `browser_network_log`
Capture XHR/fetch/JSON API calls over a time window.

```json
{"name": "browser_network_log", "arguments": {"duration_ms": 3000, "reload": true, "include_all": false, "max_items": 100}}
```

By default: XHR/fetch only, no Cookie/Authorization headers. `include_all=true` for all network requests.

#### `browser_find_api_calls`
Reload page and score likely API endpoints.

```json
{"name": "browser_find_api_calls", "arguments": {"duration_ms": 5000, "reload": true, "max_items": 60}}
```

Each result includes `api_score` based on resource type, JSON MIME, and URL patterns.

#### `browser_network_har`
Full HAR-like capture with request/response bodies.

```json
{"name": "browser_network_har", "arguments": {"duration_ms": 5000, "include_bodies": true}}
```

### Raw CDP

#### `browser_cdp`
Send arbitrary CDP commands. Escape hatch for operations not covered by dedicated tools.

```json
{"name": "browser_cdp", "arguments": {
  "method": "Runtime.evaluate",
  "params": {"expression": "document.cookie", "returnByValue": true},
  "target_id": "ABC123..."
}}
```

```json
{"name": "browser_cdp", "arguments": {
  "method": "Input.dispatchMouseEvent",
  "params": {"type": "mousePressed", "x": 100, "y": 200, "button": "left", "clickCount": 1}
}}
```

```json
{"name": "browser_cdp", "arguments": {
  "method": "Page.setFileInputFiles",
  "params": {"files": ["/path/to/file.pdf"]}
}}
```

CDP method reference: https://chromedevtools.github.io/devtools-protocol/

---

## Batch Execution

Run multiple browser actions sequentially in a single MCP call:

```json
{
  "name": "browser_batch",
  "arguments": {
    "steps": [
      {"tool": "browser_navigate", "arguments": {"url": "https://example.com/login"}},
      {"tool": "browser_wait", "arguments": {"selector": "#login-form"}},
      {"tool": "browser_type", "arguments": {"selector": "#email", "text": "user@example.com"}},
      {"tool": "browser_type", "arguments": {"selector": "#password", "text": "hunter2"}},
      {"tool": "browser_click", "arguments": {"selector": "button[type=submit]"}},
      {"tool": "browser_wait_text", "arguments": {"text": "Dashboard"}},
      {"tool": "browser_screenshot_file", "arguments": {"filename_prefix": "after-login"}}
    ],
    "stop_on_error": true
  }
}
```

Nested `browser_batch` is rejected (no recursion).

---

## Security Model

### Default Hardening (v1.4.0)

| Area | Default | Override |
|---|---|---|
| Cookie values | Redacted | `redact=false` |
| `document.cookie` | Redacted | `redact=false` |
| localStorage/sessionStorage | Redacted | `redact=false` |
| `webSocketDebuggerUrl` in tabs | Hidden | `include_debug_url=true` |
| Network capture Authorization headers | Stripped | *(never included)* |
| Network capture Cookie headers | Stripped | *(never included)* |
| Login password in response | Redacted | `redact=false` |
| Input values (password/token/auth) | Redacted | *(always redacted)* |

### Recommended Practices

1. **CDP on localhost only** — never expose `--remote-debugging-port` on `0.0.0.0`
2. **Use SSH tunnels for remote CDP** — never expose CDP to the internet
3. **Isolate browser profile** — use a dedicated `--user-data-dir` with no sensitive cookies
4. **Set `BROWSER_ARTIFACT_DIR`** — screenshots/PDFs go to a known, cleanable location
5. **Audit artifacts** — clean `BROWSER_ARTIFACT_DIR` periodically

---

## Remote Browser (SSH Wrapper)

For controlling a browser on a different machine:

### Wrapper Script

```bash
#!/usr/bin/env bash
# ~/.local/bin/browser-mcp-wrapper
set -euo pipefail
KEY="/path/to/ssh_key"
HOST="user@remote-host"
exec ssh -i "$KEY" \
  -o BatchMode=yes \
  -o ConnectTimeout=12 \
  -o StrictHostKeyChecking=accept-new \
  -o ServerAliveInterval=30 \
  "$HOST" \
  'BROWSER_CDP_URL=http://127.0.0.1:9223 BROWSER_AUTOSTART_CDP=1 exec /home/user/.local/bin/browser-mcp'
```

### Remote `browser-mcp` Script

```bash
#!/usr/bin/env bash
# ~/.local/bin/browser-mcp (on remote host)
set -euo pipefail
cd /home/user/browser-automation-mcp
exec .venv/bin/python3 server.py
```

### Diagram

```
┌───────────┐  SSH stdio   ┌───────────┐  CDP WebSocket  ┌──────────┐
│  Hermes   │ ◄───────────►│  Remote    │ ◄──────────────►│  Chrome  │
│  (local)  │  MCP tunnel  │  VPS/mac   │  localhost:9223 │  :9223   │
└───────────┘              └───────────┘                  └──────────┘
```

---

## Headless RDP / Windows Setup

### Via XRDP (Linux)

```bash
# On remote Linux with XRDP
sudo apt-get install -y xrdp chromium-browser
sudo systemctl enable --now xrdp

# Start Chrome in virtual display
DISPLAY=:10.0 chromium-browser \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --user-data-dir=/tmp/chrome-rdp-profile &
```

### Via Windows SSH + CDP

1. Install Chrome on Windows
2. Create CDP launcher: `C:\Users\User\start-cdp.bat`
   ```bat
   "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
     --remote-debugging-address=127.0.0.1 ^
     --remote-debugging-port=9223 ^
     --user-data-dir=C:\Temp\chrome-cdp-profile
   ```
3. Connect via SSH from Hermes host, run MCP server locally on Windows, or use SSH wrapper to tunnel stdio.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `CDP not reachable` | Browser not running | `curl http://127.0.0.1:9223/json/version` |
| `Connection refused` on 9223 | CDP bound to `[::1]` (IPv6) | Add `--remote-debugging-address=127.0.0.1` |
| WebSocket `403 Forbidden` | Origin check failure | Add `--remote-allow-origins=*` |
| `SingletonLock` error | Stale browser profile | `rm -rf /tmp/browser-profile/SingletonLock` |
| `websocket-client not found` | Missing Python dep | `pip install websocket-client` |
| Artifacts not saved | `BROWSER_ARTIFACT_DIR` not writable | `mkdir -p $BROWSER_ARTIFACT_DIR && chmod 755` |
| Many stale tabs | Automation didn't close tabs | Use `browser_closetab` after test runs |
| CDP timeout on slow pages | Page too heavy | Increase `BROWSER_TIMEOUT` |
| `ModuleNotFoundError` in proot | PEP 668 (externally-managed) | Use `--break-system-packages` or venv |

---

## Development

### Project Structure

```
browser-automation-mcp/
├── server.py                 # Main MCP server (107K, ~3000 lines)
├── requirements.txt          # websocket-client
├── scripts/
│   ├── smoke_mcp.py          # MCP protocol smoke test
│   └── make_release_export.py # Clean export for GitHub publishing
├── docs/
│   ├── api-reference.md      # Full API reference (32 tools)
│   ├── server-deployment.md   # Deployment recipes
│   ├── audit-hardening.md    # Security audit checklist
│   └── github-publication.md # Release workflow
├── examples/
│   └── hermes-config.yaml    # Example Hermes MCP config
├── CHANGELOG.md              # Version history
├── SECURITY.md               # Security policy
├── LICENSE                   # MIT
├── pyproject.toml            # Python project metadata
└── .github/workflows/ci.yml  # CI: py_compile + smoke test
```

### Running Tests

```bash
# Compile check
python3 -m py_compile server.py scripts/smoke_mcp.py scripts/make_release_export.py

# Smoke test (no browser)
python3 scripts/smoke_mcp.py ./server.py

# Smoke test (with live CDP)
BROWSER_CDP_URL=http://127.0.0.1:9223 python3 scripts/smoke_mcp.py ./server.py --health
```

### Creating a Release

```bash
python3 -m py_compile server.py scripts/smoke_mcp.py scripts/make_release_export.py
python3 scripts/smoke_mcp.py ./server.py
python3 scripts/make_release_export.py --dst /tmp/browser-automation-mcp-v1.4.0
cd /tmp/browser-automation-mcp-v1.4.0
python3 scripts/smoke_mcp.py ./server.py
# Verify no secrets leaked
python3 - <<'PY'
import re, pathlib
for p in pathlib.Path('.').rglob('*'):
    if p.is_file() and '.git' not in p.parts:
        t = p.read_text(errors='ignore')
        if any(rx.search(t) for rx in [
            re.compile(r'sk-[A-Za-z0-9]{20,}'),
            re.compile(r'-----BEGIN.*PRIVATE KEY-----'),
            re.compile(r'(?i)(api[_-]?key|token|password|secret)\s*=\s*["\''][^"\''\s]{16,}["\''']')
        ]):
            raise SystemExit(f'SECRET LEAK: {p}')
print('Secret scan: PASS')
PY
```

### Adding a New Tool

1. Add handler function `browser_my_new_tool(arguments)` in `server.py`
2. Register in `TOOL_HANDLERS` dict
3. Add schema to `TOOL_SCHEMAS`
4. Update `TOOL_VISIBILITY` (default `True`)
5. Add to `docs/api-reference.md`
6. Run `python3 -m py_compile server.py && python3 scripts/smoke_mcp.py ./server.py`

---

## Repository Hygiene

Before any `git push`:

```bash
# 1. Syntax check
python3 -m py_compile server.py scripts/*.py

# 2. Smoke test
python3 scripts/smoke_mcp.py ./server.py

# 3. Secret scan
grep -rE 'sk-[A-Za-z0-9]{20,}|-----BEGIN.*PRIVATE KEY-----|api[_-]?key\s*=\s*['\''"][^'\''"]{16,}' . --exclude-dir=.git --exclude-dir=__pycache__ --exclude='*.bak*' --exclude='backups/*'

# 4. Export clean
python3 scripts/make_release_export.py --dst /tmp/browser-automation-export
cd /tmp/browser-automation-export
python3 scripts/smoke_mcp.py ./server.py
```

---

## License

MIT. See [LICENSE](LICENSE).

---

## Version History

| Version | Date | Highlights |
|---|---|---|
| 1.4.0 | 2026-06 | File artifacts, `browser_snapshot`, network discovery, security hardening |
| 1.3.0 | 2026-05 | `browser_elements`, `browser_click_text`, `browser_wait_text`, batch execution |
| 1.2.0 | 2026-05 | `browser_cdp` raw passthrough, `selector_pack` self-healing, `browser_click_heal` |
| 1.1.0 | 2026-04 | Storage tools, `browser_login`, `browser_fill_form`, multi-tab management |
| 1.0.0 | 2026-04 | Initial release: navigation, click, type, wait, scroll, select, screenshot |
