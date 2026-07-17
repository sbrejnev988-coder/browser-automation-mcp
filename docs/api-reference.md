# Browser Automation MCP — API Reference

Current server version: **1.4.0**. The server exposes **32 MCP tools** over stdio and controls Chrome/Chromium/Brave through Chrome DevTools Protocol (CDP).

All `tools/call` responses are MCP text content containing JSON. Tool-level failures return `isError: true` with a JSON payload.

## Common tab arguments

Most page tools accept:

```json
{
  "tab_id": "optional CDP target id",
  "url_filter": "optional substring to select a tab by URL"
}
```

If neither is provided, the first non-DevTools page target is used, or a new `about:blank` page is created.

## Observation tools

### browser_health

Checks MCP version, CDP reachability, browser version, and current page targets.

```json
{"name": "browser_health", "arguments": {"autostart": false}}
```

### browser_tabs

Lists targets/tabs. `webSocketDebuggerUrl` is hidden by default.

```json
{"name": "browser_tabs", "arguments": {"include_non_page": false}}
```

To expose debug URLs in a trusted local environment:

```json
{"name": "browser_tabs", "arguments": {"include_debug_url": true}}
```

### browser_page_summary

Returns bounded visible text and structural page lists.

```json
{"name": "browser_page_summary", "arguments": {"max_text": 4000, "max_items": 30}}
```

### browser_elements

Discovers visible/interactive elements with stable selectors, text, roles, values, and rectangles.

```json
{"name": "browser_elements", "arguments": {"kind": "clickable", "query": "submit", "max_items": 20}}
```

Sensitive input values are redacted when type/name/id/placeholder looks like password/token/auth/session/cookie.

### browser_snapshot

Recommended first observation primitive for agents. Combines summary, interactive elements, and optional screenshot file.

```json
{"name": "browser_snapshot", "arguments": {"max_text": 4000, "max_items": 40, "include_screenshot": true}}
```

## Navigation and interaction tools

### browser_navigate

```json
{"name": "browser_navigate", "arguments": {"url": "https://example.com", "wait_until_ready": true}}
```

### browser_newtab

Opens a new tab. Debug URL hidden unless `include_debug_url=true`.

```json
{"name": "browser_newtab", "arguments": {"url": "https://example.com"}}
```

### browser_closetab

```json
{"name": "browser_closetab", "arguments": {"tab_id": "..."}}
```

or:

```json
{"name": "browser_closetab", "arguments": {"url_filter": "example.com"}}
```

### browser_click

```json
{"name": "browser_click", "arguments": {"selector": "button.primary"}}
```

### browser_click_text

Clicks visible element by text/aria/value/title without hand-written CSS selectors.

```json
{"name": "browser_click_text", "arguments": {"text": "Submit", "exact": true}}
```

### browser_type

```json
{"name": "browser_type", "arguments": {"selector": "#email", "text": "user@example.com", "clear_first": true}}
```

### browser_wait

Waits for CSS selector.

```json
{"name": "browser_wait", "arguments": {"selector": ".loaded", "timeout_ms": 10000}}
```

### browser_wait_text

Waits for visible text inside body or a selected element.

```json
{"name": "browser_wait_text", "arguments": {"text": "Saved", "selector": "body", "timeout_ms": 10000}}
```

### browser_scroll

```json
{"name": "browser_scroll", "arguments": {"direction": "down", "amount": 800}}
```

Supported directions: `down`, `up`, `top`, `bottom`. Passing `selector` scrolls that element into view.

### browser_select

Selects an option by value, label substring, or index.

```json
{"name": "browser_select", "arguments": {"selector": "select[name=country]", "label": "Germany"}}
```

### browser_fill_form

```json
{"name": "browser_fill_form", "arguments": {"fields": {"#email": "user@example.com"}, "submit_selector": "button[type=submit]"}}
```

### browser_login

Navigates, fills credentials, submits, then returns redacted cookies by default.

```json
{
  "name": "browser_login",
  "arguments": {
    "url": "https://example.com/login",
    "username_selector": "#email",
    "password_selector": "#password",
    "submit_selector": "button[type=submit]",
    "username": "user@example.com",
    "password": "example-password",
    "redact": true
  }
}
```

### browser_batch

Runs multiple browser actions sequentially in one MCP call. Nested `browser_batch` is rejected.

```json
{
  "name": "browser_batch",
  "arguments": {
    "steps": [
      {"tool": "browser_navigate", "arguments": {"url": "https://example.com"}},
      {"tool": "browser_wait_text", "arguments": {"text": "Example Domain"}}
    ],
    "stop_on_error": true
  }
}
```

## DOM/data extraction tools

### browser_exec

Executes JavaScript and returns `Runtime.evaluate.result.value`.

```json
{"name": "browser_exec", "arguments": {"expression": "document.title"}}
```

For promises:

```json
{"name": "browser_exec", "arguments": {"expression": "fetch('/api/data').then(r => r.json())", "await_promise": true}}
```

### browser_gettext

```json
{"name": "browser_gettext", "arguments": {"selector": "body"}}
```

Set `all=true` for all matching elements.

### browser_gethtml

```json
{"name": "browser_gethtml", "arguments": {"selector": "#content", "outer": true}}
```

### browser_getvalue

```json
{"name": "browser_getvalue", "arguments": {"selector": "input[name=q]"}}
```

## Storage/session tools

### browser_cookies

Extracts cookies, `document.cookie`, localStorage and sessionStorage. Redacted by default.

```json
{"name": "browser_cookies", "arguments": {"redact": true}}
```

### browser_localstorage / browser_sessionstorage

Secret-like keys are redacted by default.

```json
{"name": "browser_localstorage", "arguments": {"redact": true}}
```

```json
{"name": "browser_sessionstorage", "arguments": {"key": "theme", "redact": true}}
```

## Artifact tools

### browser_screenshot

Returns base64 PNG. Prefer `browser_screenshot_file` for chat/CI/public automation.

```json
{"name": "browser_screenshot", "arguments": {"full_page": false}}
```

### browser_screenshot_file

Saves PNG under `BROWSER_ARTIFACT_DIR` and returns `{path, bytes, media_hint}`.

```json
{"name": "browser_screenshot_file", "arguments": {"full_page": true, "filename_prefix": "evidence"}}
```

### browser_pdf

Returns base64 PDF. Prefer `browser_pdf_file` for most workflows.

```json
{"name": "browser_pdf", "arguments": {}}
```

### browser_pdf_file

```json
{"name": "browser_pdf_file", "arguments": {"filename_prefix": "page"}}
```

### browser_html_file

Saves current `document.documentElement.outerHTML`.

```json
{"name": "browser_html_file", "arguments": {"filename_prefix": "page"}}
```

## Network/API discovery tools

### browser_network_log

Captures CDP Network events for a bounded time window. By default returns XHR/fetch/JSON-like calls only and does not include Cookie/Authorization headers.

```json
{"name": "browser_network_log", "arguments": {"duration_ms": 3000, "reload": false, "include_all": false, "max_items": 100}}
```

### browser_find_api_calls

Reloads by default and scores likely API endpoints.

```json
{"name": "browser_find_api_calls", "arguments": {"duration_ms": 3000, "reload": true, "max_items": 60}}
```

Each returned API candidate includes an `api_score` based on resource type, JSON MIME type, and URL pattern.
