# Changelog

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
