# Browser Automation MCP — API Reference

26 tools wrapping Chrome DevTools Protocol via WebSocket. All tools return JSON with `{ok: true, ...}`.

## browser_navigate

Navigate to URL. Auto-waits for DOM.

```json
// Request
{"name": "browser_navigate", "arguments": {"url": "https://example.com"}}

// Response
{"ok": true, "url": "https://example.com/page", "title": "Page Title"}
```

## browser_screenshot

Capture page as PNG base64.

```json
// Request
{"name": "browser_screenshot", "arguments": {"full_page": false}}

// Response
{"ok": true, "screenshot_base64": "iVBORw0KGgo...", "format": "png"}
```

## browser_cookies

Extract all cookies, document.cookie, localStorage, sessionStorage.

```json
// Request
{"name": "browser_cookies", "arguments": {"url_filter": "example.com"}}

// Response
{
  "ok": true,
  "cookies": [{"name": "session", "value": "abc123", "domain": ".example.com", "path": "/", "httpOnly": true, "secure": true, "sameSite": "Lax"}],
  "document_cookie": "session=abc123; _ga=GA1.2.456...",
  "url": "https://example.com/dashboard",
  "title": "Dashboard",
  "localStorage": {"token": "eyJ...", "theme": "dark"},
  "sessionStorage": {"csrf": "xyz"}
}
```

## browser_localstorage / browser_sessionstorage

```json
// All keys
{"name": "browser_localstorage", "arguments": {}}
// → {"ok": true, "data": {"token": "eyJ...", "theme": "dark", "lang": "ru"}}

// Specific key
{"name": "browser_localstorage", "arguments": {"key": "token"}}
// → {"ok": true, "data": {"token": "eyJ..."}}
```

## browser_exec

Execute JavaScript in page context.

```json
// Simple
{"name": "browser_exec", "arguments": {"expression": "document.title"}}
// → {"ok": true, "result": "Page Title", "type": "string"}

// JSON extraction
{"name": "browser_exec", "arguments": {"expression": "JSON.stringify(Array.from(document.querySelectorAll('.product-card')).map(c => ({name: c.querySelector('.name')?.innerText, price: c.querySelector('.price')?.innerText})))"}}
// → {"ok": true, "result": "[{\"name\":\"Item 1\",\"price\":\"100 ₽\"},...]", "type": "string"}

// Async
{"name": "browser_exec", "arguments": {"expression": "fetch('/api/data').then(r => r.json())", "await_promise": true}}
```

## browser_click / browser_type

```json
// Click
{"name": "browser_click", "arguments": {"selector": ".btn-primary"}}
// → {"ok": true, "clicked": true, "tag": "BUTTON", "text": "Submit"}

// Type
{"name": "browser_type", "arguments": {"selector": "#email", "text": "user@example.com", "clear_first": true}}
// → {"ok": true, "typed": true, "value": "user@example.com"}
```

## browser_gettext / browser_gethtml / browser_getvalue

```json
// Single element text
{"name": "browser_gettext", "arguments": {"selector": ".price"}}
// → {"ok": true, "text": "1 234 ₽"}

// All matching elements
{"name": "browser_gettext", "arguments": {"selector": ".product-name", "all": true}}
// → {"ok": true, "text": ["Product A", "Product B", "Product C"]}

// HTML
{"name": "browser_gethtml", "arguments": {"selector": "#content", "outer": true}}
// → {"ok": true, "html": "<div id=\"content\">...</div>"}

// Input value
{"name": "browser_getvalue", "arguments": {"selector": "#email"}}
// → {"ok": true, "value": "user@example.com"}
```

## browser_wait

Wait for element to appear.

```json
{"name": "browser_wait", "arguments": {"selector": ".loaded", "timeout_ms": 15000}}
// → {"ok": true, "found": true, "selector": ".loaded"}
```

## browser_elements / browser_click_text / browser_wait_text

Agent-friendly DOM discovery and text-driven interaction. Added in MCP v1.3.0.

```json
// Discover visible interactive elements and stable selectors
{"name": "browser_elements", "arguments": {"kind": "clickable", "query": "submit", "max_items": 20}}
// → {"ok": true, "count": 2, "elements": [{"selector": "#submit", "kind": "button", "text": "Submit", "rect": {"x": 10, "y": 20, "width": 80, "height": 32}}]}

// Click without hand-writing CSS selectors
{"name": "browser_click_text", "arguments": {"text": "Submit", "exact": true}}
// → {"ok": true, "clicked": true, "text": "Submit", "tag": "BUTTON", "matches_count": 1}

// Wait for user-visible text after navigation/clicks
{"name": "browser_wait_text", "arguments": {"text": "Saved", "selector": "body", "timeout_ms": 10000}}
// → {"ok": true, "found": true, "text": "Settings Saved"}
```

Notes:
- `browser_elements` redacts sensitive input values when type/name/id/placeholder looks like password/token/auth/session/cookie.
- Use `browser_elements` first when selectors are unknown, then `browser_click_text` for brittle/dynamic UIs.

## browser_fill_form

Fill multiple fields at once.

```json
// Request
{"name": "browser_fill_form", "arguments": {
  "fields": {
    "#email": "user@example.com",
    "#password": "secret123",
    "#name": "John"
  },
  "submit_selector": "#submit"  // optional: click submit after filling
}}
// → {"ok": true, "fields_filled": 3, "submitted": true, "details": {...}}
```

## browser_login

Full login flow: navigate → wait for form → fill credentials → submit → return cookies.

```json
// Request
{"name": "browser_login", "arguments": {
  "url": "https://example.com/login",
  "username_selector": "#email",
  "password_selector": "#password",
  "submit_selector": "button[type='submit']",
  "username": "user@example.com",
  "password": "secret123",
  "extra_fields": {"#captcha": "manual-input"}  // optional
}}
// → {"ok": true, "login_completed": true, "cookies": [...], "url": "..."}
```

## browser_tabs / browser_newtab / browser_closetab

```json
// List tabs
{"name": "browser_tabs", "arguments": {}}
// → {"ok": true, "tabs": [{"id": "ABC123...", "url": "...", "title": "...", "type": "page"}], "count": 5}

// New tab
{"name": "browser_newtab", "arguments": {"url": "https://example.com"}}
// → {"ok": true, "tab_id": "DEF456..."}

// Close tab
{"name": "browser_closetab", "arguments": {"url_filter": "example.com"}}
// → {"ok": true, "closed": "DEF456..."}
```

## browser_scroll / browser_pdf

```json
// Scroll
{"name": "browser_scroll", "arguments": {"direction": "down", "amount": 500}}
// → {"ok": true, "result": "scrolled down 500px"}
{"name": "browser_scroll", "arguments": {"direction": "bottom"}}
{"name": "browser_scroll", "arguments": {"selector": "#footer"}}

// PDF
{"name": "browser_pdf", "arguments": {}}
// → {"ok": true, "pdf_base64": "JVBERi0xLjQK...", "format": "pdf"}
```

## Error Responses

```json
{"ok": true, "error": "element not found: .nonexistent"}
{"ok": false, "error": "timeout waiting for .slow-element"}
```
