# Changelog

## 1.5.1

### P0 Fixes (DEA forensic audit)
- **Heartbeat lock**: connection list copied under lock, network I/O performed outside lock — no more blocking other tabs during heartbeat
- **Dead connection cleanup**: `conn.close()` called before removing from dict — WebSocket + reader thread properly terminated
- **Reader loop**: `_closed.set()` on disconnect prevents `is_alive` from returning True for dead connections; `_closed.clear()` on successful reconnect
- **Retry safety**: non-idempotent CDP methods (`Page.navigate`, `Input.dispatchMouseEvent`, `DOM.setFileInputFiles`, etc.) skip retry — no more double-clicks
- **_active_tab_id integration**: resolution order is now `tab_id > url_filter > active_tab_id > single_tab > AMBIGUOUS_TARGET`
- **DNS SSRF protection**: `socket.getaddrinfo()` resolves hostnames and blocks loopback/private/link-local/reserved IPs
- **Cookie delete schema**: `name` is now required (CDP requirement), `domain` is optional
- **pyproject.toml**: version synced to 1.5.0

### Known issues (P1)
- HAR capture uses separate WebSocket without Bearer auth — needs dispatcher migration
- `persist_to_wiki`/`browser_recall` stubs — Memory Wiki integration via broker pattern not yet implemented
- Legacy `browser_cookies` mixes cookies + storage — should be deprecated
- No browser integration test in CI

## 1.5.0

### Breaking CDP fixes
- **CDPConnection dispatcher**: single reader thread, Future-based multiplexing, global monotonic IDs — fixes heartbeat response corruption, ID collisions, missing lock
- **Tab safety**: `AMBIGUOUS_TARGET` error when multiple tabs without explicit selection
- **Navigation policy**: blocks `file://`, `javascript:`, private IPs before navigation

### New tools
- `browser_cookie_list`: scope-based (`current_page`|`browser_context`), structured redaction
- `browser_cookie_set`: `Storage.setCookies` with full cookie attributes
- `browser_cookie_delete`: by name/domain/path
- `browser_cookie_clear`: page or browser context scope

### Security
- `browser_login`: `credential_ref` (`vault://` or `env:`) replaces plaintext password
- Artifacts: 0600 permissions, `O_CREAT|O_EXCL`, `secrets.token_hex(8)`, symlink check
- WebSocket auth: Bearer token in handshake header
- BrowserError structured exception class


## 1.4.0

- Added file artifact tools: `browser_screenshot_file`, `browser_pdf_file`, `browser_html_file`.
- Added `browser_snapshot` as the recommended agent observation primitive.
- Added lightweight Network/API discovery tools: `browser_network_log`, `browser_find_api_calls`.
- Hardened public-safe defaults:
  - cookies/storage/login outputs redact secret-like values by default;
  - tab tools hide `webSocketDebuggerUrl` unless `include_debug_url=true`;
  - network capture does not return Cookie/Authorization headers.
- Added GitHub-ready packaging/docs: `README.md`, `SECURITY.md`, `LICENSE`, `pyproject.toml`, examples, smoke tests, release export script, and CI workflow.

## 1.3.0

- Added agent-friendly DOM tools: `browser_elements`, `browser_click_text`, `browser_wait_text`.
- Added select control support and batch execution.
- Improved CDP health/autostart handling.
