# Server Deployment Recipe

Full deployment of Browser Automation MCP on a remote Linux server (Ubuntu 24.04, Hermes Agent).

## 1. Prerequisites

```bash
# Xvfb for headless display
apt install xvfb

# Python websocket-client (MCP server dependency)
pip install websocket-client  # or: pip install --break-system-packages websocket-client

# Brave browser
apt install brave-browser  # or snap install brave
```

## 2. Start Xvfb

```bash
Xvfb :99 -screen 0 1920x1080x24 &
# Or as systemd service (usually already running from desktop)
```

## 3. Brave systemd user service

File: `~/.config/systemd/user/brave-cdp.service`

```ini
[Unit]
Description=Brave Browser with CDP
After=network-online.target

[Service]
Type=simple
Environment=DISPLAY=:99
ExecStart=/usr/bin/brave-browser \
  --remote-debugging-port=9223 \
  --remote-allow-origins=* \
  --no-first-run \
  --no-default-browser-check \
  --user-data-dir=/tmp/brave-debug-profile \
  --disable-gpu \
  --no-sandbox \
  --disable-dev-shm-usage \
  --password-store=basic
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

CRITICAL: `--remote-allow-origins=*` is MANDATORY. Without it, CDP WebSocket connections get 403 Forbidden.

Enable and start:
```bash
systemctl --user daemon-reload
systemctl --user enable brave-cdp
systemctl --user start brave-cdp
```

Verify:
```bash
curl -s 127.0.0.1:9223/json/version  # Should return Chrome version JSON
```

## 4. Deploy MCP Server

Copy `server.py` to server:
```bash
mkdir -p ~/.hermes/mcp-servers/browser-automation
scp server.py user@host:~/.hermes/mcp-servers/browser-automation/server.py
```

## 5. Config.yaml

Add to `mcp_servers`:
```yaml
mcp_servers:
  browser-automation:
    command: /usr/bin/python3
    args:
      - /home/Hermes/.hermes/mcp-servers/browser-automation/server.py
    timeout: 60
    connect_timeout: 15
    enabled: true
    env:
      BROWSER_CDP_URL: "http://127.0.0.1:9223"
      CODEX_DEBUG_TOKEN: ${CODEX_DEBUG_TOKEN}
```

Add to `terminal.env_passthrough`:
```yaml
terminal:
  env_passthrough:
    - BROWSER_CDP_URL
    - BROWSER_AUTH_TOKEN
    - BROWSER_TIMEOUT
    - CODEX_DEBUG_TOKEN
    - CDP_BRIDGE_PORT
    - CDP_BRIDGE_TOKEN
```

## 6. Restart Hermes

```bash
# Find correct restart command for the deployment
/home/Hermes/.hermes/bin/herm-start restart
# OR
/home/Hermes/.hermes/hermes restart
```

## 7. Verify

```bash
# Test MCP tools list
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | \
  python3 ~/.hermes/mcp-servers/browser-automation/server.py 2>/dev/null

# Should return 19 tools: browser_navigate, browser_cookies, browser_exec...

# Quick smoke test
export CODEX_DEBUG_TOKEN="your-token"
python3 /tmp/smoke_test.py  # See templates/smoke-test.py
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Connection Refused` on :9223 | Brave not running | `systemctl --user restart brave-cdp` |
| `ws_error: Handshake status 403` | Missing `--remote-allow-origins=*` | Restart brave-cdp (service has the flag) |
| Empty results from navigate | handler calls `_safe_ws()` 3 times | Single `_safe_ws` call with all commands |
| MCP returns `error` not `result` | CDP unreachable or WebSocket timeout | Check `curl 127.0.0.1:9223/json/version` |
| 220+ `chrome://newtab/` tabs | Brave session leak | `systemctl --user restart brave-cdp` |
