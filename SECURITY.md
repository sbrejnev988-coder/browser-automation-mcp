# Security Policy

## Local-only CDP

Bind Chrome DevTools Protocol to localhost:

```bash
--remote-debugging-address=127.0.0.1 --remote-debugging-port=9223
```

Do not expose CDP directly to the public internet. CDP can fully control the browser profile.

## Redaction defaults

The server redacts cookies and secret-like storage keys by default. Tools that can reveal sensitive data require explicit arguments, for example `redact=false` or `include_debug_url=true`.

## Secrets

Do not commit:

- cookies
- browser profiles
- `.env` files
- bearer tokens
- API keys
- private SSH keys
- screenshots/PDFs containing private data

## Reporting issues

When reporting a security issue, include:

- server version from `browser_health`
- Python version
- browser version from `/json/version`
- minimal reproduction steps
- sanitized logs only
