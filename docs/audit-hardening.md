# Browser Automation MCP audit & hardening notes

Session-derived checklist for auditing/repairing a CDP-backed MCP browser server.

## What to inspect in `server.py`

1. Confirm every advertised tool has a handler and is registered in `HANDLERS`.
2. Check MCP JSON-RPC shapes:
   - `initialize` returns protocolVersion, capabilities, serverInfo.
   - `tools/list` returns `{tools: [...]}` with JSON schemas.
   - `tools/call` returns `content: [{type: "text", text: ...}]`; prefer `isError: true` for tool-level failures instead of JSON-RPC `error` when possible.
3. Audit CDP helper lifecycle:
   - WebSocket must close on success and timeout.
   - Join worker threads after close.
   - `_safe_ws()` should actually retry; do not just refresh the tab and re-raise.
   - Avoid mutating caller command dicts with transient `_id` fields.
4. Audit JavaScript generation:
   - Never interpolate CSS selectors or storage keys as `'{sel}'` / `'{key}'`.
   - Use `json.dumps(sel)` / `json.dumps(key)` in Python before embedding in JS.
   - Add `returnByValue: true` when handlers read `Runtime.evaluate.result.value`.
   - Add `awaitPromise: true` for promise-based waits.
5. Audit tab targeting:
   - If all tools call `_get_tab()` with no id, they operate on the first page target and race with multi-tab workflows.
   - Prefer optional `tab_id` / `url_filter` for all page-specific tools.

## Common concrete bugs

### `/json/close/{id}` is not JSON

Chrome/Brave returns plain text from `/json/close/{id}`. If the shared HTTP helper always does `json.loads()`, `browser_closetab` may physically close the tab and still report:

```text
Expecting value: line 1 column 1 (char 0)
```

Fix with a `raw=True` path or tolerant JSON parsing:

```python
resp = urllib.request.urlopen(req, timeout=5).read()
if raw:
    return resp
try:
    return json.loads(resp)
except json.JSONDecodeError:
    return {"raw": resp.decode(errors="replace")}
```

Then call close as raw:

```python
_http("GET", f"/json/close/{tab_id}", raw=True)
```

### `browser_wait` returns a Promise object

If the expression is `new Promise(...)` but `Runtime.evaluate` lacks `awaitPromise: True`, CDP returns a remote Promise object rather than the final boolean. Include:

```python
{"awaitPromise": True, "returnByValue": True}
```

### Selector/key injection

Bad:

```python
f"document.querySelector('{sel}')"
f"localStorage.getItem('{key}')"
```

Good:

```python
sel_js = json.dumps(sel)
key_js = json.dumps(key)
f"document.querySelector({sel_js})"
f"localStorage.getItem({key_js})"
```

### WebSocket leak pattern

If `_ws_cmd()` starts `WebSocketApp.run_forever()` in a daemon thread and only closes on timeout, success paths can leak connections/threads. Always close in `finally` and `join(timeout=1)`.

## Browser/CDP infrastructure audit commands

Run as the service owner when checking a user-level systemd unit:

```bash
sudo -u Hermes XDG_RUNTIME_DIR=/run/user/$(id -u Hermes) systemctl --user status brave-cdp --no-pager
sudo -u Hermes XDG_RUNTIME_DIR=/run/user/$(id -u Hermes) systemctl --user show brave-cdp -p ActiveState -p SubState -p NRestarts -p ExecMainPID
sudo -u Hermes XDG_RUNTIME_DIR=/run/user/$(id -u Hermes) journalctl --user -u brave-cdp -n 30 --no-pager
ss -tlnp | grep 9223
curl -s http://127.0.0.1:9223/json/version
curl -s http://127.0.0.1:9223/json
ps -ef | grep -E 'Xvfb|brave|chromium|chrome' | grep -v grep
```

Important: running `systemctl --user status brave-cdp` as `root` can incorrectly say the unit does not exist if the unit belongs to `Hermes`.

## Security checks

- Confirm CDP binds to `127.0.0.1`, not `0.0.0.0`.
- `--remote-allow-origins=*` is tolerable for localhost-only CDP but should not be combined with public binding.
- `/json` exposes target metadata; WebSocket CDP exposes cookies/localStorage/sessionStorage.
- Redact cookie values and token-like storage keys in reports/logs.
- Grep browser logs for `accountId|deviceId|sessionId|auth|token|bearer|password|cookie`, but expect benign hits from flags like `--password-store=basic`.

## Stress-test recipe

1. Snapshot existing `/json` targets.
2. Open test tabs via `/json/new?<url>` or the MCP `browser_newtab` tool.
3. For each tab, run:
   - `Runtime.evaluate(document.body?.innerText.slice(0,5000))`
   - `Page.captureScreenshot({format: "png"})`
4. Verify non-empty text and screenshot base64.
5. Close only created tabs; remember `/json/close` returns plain text.
6. Re-check `/json` to ensure no test tabs remain.

Useful test URLs: Wildberries, GitHub, Google, Habr. Yandex may redirect to CAPTCHA; count that as browser/network success but note anti-bot friction.
