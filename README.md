# Browser Automation MCP Server

Universal Browser Automation MCP server that controls Chrome/Chromium through the Chrome DevTools Protocol (CDP) without depending on public BrowserMCP.

## Features

- Direct CDP HTTP/WebSocket client
- Navigation, screenshots, JavaScript evaluation and DOM helpers
- Form filling, login helper, text click/wait helpers and select controls
- Tab discovery/switching and basic operational health/autostart support
- Batch execution to reduce MCP round trips

## Requirements

- Python 3.10+
- `websocket-client` Python package
- A local or remote Chrome/Chromium CDP endpoint

## Quick start

```bash
pip install websocket-client
export BROWSER_CDP_URL=http://127.0.0.1:9223
python3 server.py
```

Optional environment variables:

- `BROWSER_AUTH_TOKEN` or `CODEX_DEBUG_TOKEN` — bearer token for protected CDP endpoints
- `BROWSER_TIMEOUT` — CDP command timeout, default `30`
- `BROWSER_HTTP_TIMEOUT` — HTTP timeout, default `10`
- `BROWSER_AUTOSTART_CDP` — enable/disable best-effort CDP autostart, default enabled
- `BROWSER_CDP_START_CMD` — command used to start CDP when unavailable

See `docs/api-reference.md` for tool details.
