# Server Deployment Recipe

This recipe deploys the Browser Automation MCP server on Linux, Android/Termux proot, or a remote Hermes host. The server itself is a Python stdio MCP process; it needs a Chrome/Chromium/Brave CDP endpoint.

## 1. Install Python dependency

```bash
python3 -m pip install -r requirements.txt
```

If installing globally is blocked, use a virtualenv:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## 2. Start a browser with CDP

Linux/headless example:

```bash
chromium-browser \
  --headless=new \
  --no-sandbox \
  --disable-dev-shm-usage \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --user-data-dir=/tmp/browser-automation-profile
```

Brave example:

```bash
brave-browser \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --remote-allow-origins=* \
  --no-first-run \
  --no-default-browser-check \
  --user-data-dir=/tmp/brave-browser-automation-profile
```

Verify CDP:

```bash
curl -s http://127.0.0.1:9223/json/version
```

Expected: JSON containing `Browser` and `webSocketDebuggerUrl`.

## 3. Hermes MCP configuration

```yaml
mcp_servers:
  browser-automation:
    enabled: true
    command: /usr/bin/python3
    args:
      - /absolute/path/to/browser-automation-mcp/server.py
    timeout: 120
    connect_timeout: 30
    env:
      BROWSER_CDP_URL: http://127.0.0.1:9223
      BROWSER_TIMEOUT: "30"
      BROWSER_HTTP_TIMEOUT: "10"
      BROWSER_AUTOSTART_CDP: "1"
      BROWSER_ARTIFACT_DIR: /root/.hermes/cache/documents/browser-automation
```

Optional env vars:

| Variable | Default | Purpose |
|---|---:|---|
| `BROWSER_CDP_URL` | `http://127.0.0.1:9223` | CDP HTTP endpoint |
| `BROWSER_TIMEOUT` | `30` | WebSocket command timeout in seconds |
| `BROWSER_HTTP_TIMEOUT` | `10` | CDP `/json` HTTP timeout in seconds |
| `BROWSER_AUTOSTART_CDP` | `1` | Try `BROWSER_CDP_START_CMD` when CDP is down |
| `BROWSER_CDP_START_CMD` | `~/.local/bin/browser-cdp-start` | Best-effort browser/CDP start command |
| `BROWSER_ARTIFACT_DIR` | `~/.hermes/cache/documents/browser-automation` | File artifact output directory |
| `BROWSER_AUTH_TOKEN` | empty | Optional bearer token for protected CDP bridges |

## 4. Smoke tests

Without requiring a live browser:

```bash
python3 -m py_compile server.py scripts/smoke_mcp.py scripts/make_release_export.py
python3 scripts/smoke_mcp.py ./server.py
```

With a live CDP endpoint:

```bash
BROWSER_CDP_URL=http://127.0.0.1:9223 python3 scripts/smoke_mcp.py ./server.py --health
```

## 5. Remote/SSH wrapper pattern

For a remote browser host, keep Hermes local but run the MCP server over SSH stdio:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec ssh user@host 'BROWSER_CDP_URL=http://127.0.0.1:9223 exec /path/to/browser-automation-mcp/server.py'
```

Then set Hermes `command` to the wrapper path.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `CDP not reachable` | Browser is not running or wrong port | Check `/json/version`, start browser |
| `Connection refused` | CDP bound to IPv6 or different address | Add `--remote-debugging-address=127.0.0.1` |
| WebSocket 403 | Browser rejects CDP WebSocket origin | Add `--remote-allow-origins=*` for localhost-only browser |
| Many stale tabs | Automation did not close created tabs | Use `browser_closetab` and isolate test tabs |
| No artifact files | Directory missing/permission issue | Set writable `BROWSER_ARTIFACT_DIR` |
| `websocket-client not installed` | Missing dependency | `python3 -m pip install -r requirements.txt` |
