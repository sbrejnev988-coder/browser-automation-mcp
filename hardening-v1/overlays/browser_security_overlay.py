"""Runtime security and Memory-Wiki bridge overlay for browser-automation-mcp.

The overlay intentionally does not remove any MCP tools. It adds policy checks,
secret-safe auditing and a durable filesystem bridge to hermes-memory-wiki.
No third-party dependencies are required.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, MutableMapping, Optional, Tuple
from urllib.parse import urlparse

OVERLAY_VERSION = "1.0.0"
_LOCK = threading.RLock()

_SENSITIVE_KEY_RE = re.compile(
    r"(?:cookie|session|token|secret|pass(?:word|wd)?|auth|bearer|jwt|csrf|xsrf|"
    r"api[-_]?key|access[-_]?key|credential|private[-_]?key)", re.I
)
_SECRET_SOURCE_RE = re.compile(
    r"(?:document\s*\.\s*cookie|localStorage|sessionStorage|indexedDB|"
    r"password[^\n]{0,80}\.value|querySelector\([^\n]{0,120}password)", re.I
)
_EXFIL_SINK_RE = re.compile(
    r"(?:fetch\s*\(|XMLHttpRequest|sendBeacon\s*\(|new\s+WebSocket\s*\(|"
    r"\.src\s*=|location\s*=|location\.href\s*=)", re.I
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str) -> Tuple[str, ...]:
    return tuple(x.strip().lower().lstrip(".") for x in os.environ.get(name, "").split(",") if x.strip())


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser().resolve()


def _bridge_root() -> Path:
    override = os.environ.get("BROWSER_MEMORY_WIKI_BRIDGE_DIR", "").strip()
    return Path(override).expanduser().resolve() if override else _hermes_home() / "memory-wiki" / "browser_bridge"


def _audit_path() -> Path:
    override = os.environ.get("BROWSER_SECURITY_AUDIT_LOG", "").strip()
    return Path(override).expanduser().resolve() if override else _hermes_home() / "browser-automation" / "audit.jsonl"


def _domain_from(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return (urlparse(text).hostname or "").lower().rstrip(".")
    return text.lstrip(".").split(":", 1)[0].lower().rstrip(".")


def _domain_allowed(domain: str, allowlist: Iterable[str]) -> bool:
    domain = _domain_from(domain)
    allowed = tuple(allowlist)
    if not allowed:
        return False
    return any(domain == item or domain.endswith("." + item) for item in allowed)


def _redacted(value: Any) -> str:
    try:
        length = len(str(value))
    except Exception:
        length = 0
    return f"[redacted len={length}]"


def _sanitize(value: Any, *, redact_all_values: bool = False, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated-depth]"
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if _SENSITIVE_KEY_RE.search(key_s) or (redact_all_values and key_s.lower() == "value"):
                out[key_s] = _redacted(item)
            else:
                out[key_s] = _sanitize(item, redact_all_values=redact_all_values, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, redact_all_values=redact_all_values, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return value[:12000]
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(tmp), flags, 0o600)
    try:
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with _LOCK:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)


def _audit(tool: str, args: Mapping[str, Any], decision: str, reason: str = "") -> None:
    safe_args = _sanitize(dict(args))
    # Never retain expressions or form contents in the audit log.
    for key in ("expression", "password", "username", "value", "text", "fields", "extra_fields"):
        if key in safe_args:
            safe_args[key] = _redacted(args.get(key))
    record = {
        "ts": int(time.time()),
        "overlay_version": OVERLAY_VERSION,
        "tool": tool,
        "decision": decision,
        "reason": reason[:300],
        "args": safe_args,
    }
    try:
        _append_jsonl(_audit_path(), record)
    except Exception:
        # Audit failure must not expose secrets through an exception message.
        pass


def _cookie_target_domain(args: Mapping[str, Any]) -> str:
    for key in ("domain_filter", "domain", "url", "url_filter"):
        if args.get(key):
            return _domain_from(args.get(key))
    return ""


def _raw_cookie_authorized(args: Mapping[str, Any]) -> bool:
    if not _env_bool("BROWSER_ALLOW_RAW_COOKIE_VALUES", False):
        return False
    if not bool(args.get("confirm_sensitive_data")):
        return False
    allowlist = _csv_env("BROWSER_COOKIE_ALLOWED_DOMAINS")
    domain = _cookie_target_domain(args)
    scope = str(args.get("scope") or "current_page")
    if scope == "browser_context" and not domain:
        return False
    # For current_page the dispatcher may not expose the selected tab URL here.
    return bool(domain and _domain_allowed(domain, allowlist)) or (scope == "current_page" and not domain and "*" in allowlist)


def _validate_cookie_set(args: Mapping[str, Any]) -> None:
    name = str(args.get("name") or "")
    value = str(args.get("value") or "")
    url = str(args.get("url") or "")
    domain = str(args.get("domain") or "")
    path = str(args.get("path") or "/")
    secure = bool(args.get("secure", True))
    same_site = str(args.get("sameSite") or "Lax")

    if not name or any(ch in name for ch in "\r\n\t ;,"):
        raise PermissionError("invalid cookie name")
    if len(value.encode("utf-8", errors="ignore")) > 4096:
        raise PermissionError("cookie value exceeds 4096 bytes")
    if bool(url) == bool(domain):
        raise PermissionError("provide exactly one of url or domain")
    if same_site == "None" and not secure:
        raise PermissionError("SameSite=None requires secure=true")
    if name.startswith("__Secure-") and (not secure or (url and urlparse(url).scheme != "https")):
        raise PermissionError("__Secure- cookie requires Secure and HTTPS")
    if name.startswith("__Host-"):
        if not secure or domain or path != "/" or not url or urlparse(url).scheme != "https":
            raise PermissionError("__Host- cookie requires HTTPS url, Secure, Path=/ and no Domain")
    partition = args.get("partitionKey")
    if partition:
        top = str((partition or {}).get("topLevelSite") or "")
        if not top.startswith("https://"):
            raise PermissionError("partitionKey.topLevelSite must be HTTPS")


def preflight(tool: str, args: MutableMapping[str, Any]) -> None:
    """Validate a tool invocation. Raises PermissionError on policy violation."""
    tool = str(tool or "")
    if not isinstance(args, MutableMapping):
        raise PermissionError("tool arguments must be an object")

    if tool == "browser_cookie_set":
        _validate_cookie_set(args)
    elif tool == "browser_cookie_clear":
        if str(args.get("scope") or "current_page") == "browser_context" and not bool(args.get("confirm_destructive")):
            raise PermissionError("browser_context cookie clear requires confirm_destructive=true")
    elif tool == "browser_cookie_list":
        if bool(args.get("include_values")) and not _raw_cookie_authorized(args):
            raise PermissionError(
                "raw cookie values require BROWSER_ALLOW_RAW_COOKIE_VALUES=1, "
                "confirm_sensitive_data=true and an allowed domain"
            )
    elif tool == "browser_cookies":
        if args.get("redact", True) is False and not _raw_cookie_authorized(args):
            raise PermissionError("unredacted browser_cookies output is disabled by policy")
    elif tool in {"browser_localstorage", "browser_sessionstorage"}:
        if args.get("redact", True) is False:
            if not (_env_bool("BROWSER_ALLOW_RAW_STORAGE_VALUES", False) and bool(args.get("confirm_sensitive_data"))):
                raise PermissionError("unredacted web storage requires explicit operator enablement and confirmation")
    elif tool == "browser_login":
        if (args.get("username") is not None or args.get("password") is not None) and not _env_bool(
            "BROWSER_ALLOW_LEGACY_PLAINTEXT_LOGIN", False
        ):
            raise PermissionError("plaintext username/password MCP arguments are disabled; use credential_ref")
        if not args.get("credential_ref") and not _env_bool("BROWSER_ALLOW_LEGACY_PLAINTEXT_LOGIN", False):
            raise PermissionError("credential_ref is required")
        if args.get("redact", True) is False and not _raw_cookie_authorized(args):
            raise PermissionError("unredacted login cookies are disabled by policy")
    elif tool == "browser_batch":
        steps = args.get("steps") or []
        if not isinstance(steps, list) or len(steps) > 100:
            raise PermissionError("browser_batch steps must be a list with at most 100 entries")
        for step in steps:
            if not isinstance(step, dict):
                raise PermissionError("browser_batch step must be an object")
            nested_tool = str(step.get("tool") or "")
            nested_args = step.get("arguments") or {}
            if not nested_tool.startswith("browser_") or not isinstance(nested_args, MutableMapping):
                raise PermissionError("invalid browser_batch step")
            preflight(nested_tool, nested_args)
    elif tool == "browser_network_har":
        if bool(args.get("include_bodies")) and not (
            _env_bool("BROWSER_ALLOW_NETWORK_BODIES", False) and bool(args.get("confirm_sensitive_data"))
        ):
            raise PermissionError("HAR response bodies require operator enablement and explicit confirmation")
    elif tool == "browser_exec":
        expression = str(args.get("expression") or "")
        sensitive = bool(_SECRET_SOURCE_RE.search(expression))
        exfil = bool(_EXFIL_SINK_RE.search(expression))
        if sensitive and not (
            _env_bool("BROWSER_ALLOW_SENSITIVE_EXEC", False) and bool(args.get("confirm_sensitive_data"))
        ):
            raise PermissionError("browser_exec access to cookies/storage/password fields is disabled; use typed tools")
        if sensitive and exfil and not _env_bool("BROWSER_ALLOW_SENSITIVE_EXEC_EXFIL", False):
            raise PermissionError("browser_exec expression combines secret access with a network/data sink")


def _extract_invocation(fn: Callable[..., Any], pargs: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Tuple[str, MutableMapping[str, Any]]:
    name = ""
    arguments: Any = None
    try:
        bound = inspect.signature(fn).bind_partial(*pargs, **kwargs)
        values = dict(bound.arguments)
    except Exception:
        values = dict(kwargs)
    for key in ("name", "tool_name", "tool"):
        val = values.get(key)
        if isinstance(val, str) and val.startswith("browser_"):
            name = val
            break
    for key in ("arguments", "args", "params", "payload"):
        val = values.get(key)
        if isinstance(val, dict):
            if not name:
                nested_name = val.get("name") or val.get("tool_name")
                if isinstance(nested_name, str) and nested_name.startswith("browser_"):
                    name = nested_name
                    val = val.get("arguments") or val.get("args") or {}
            arguments = val
            break
    if not name:
        for val in pargs:
            if isinstance(val, str) and val.startswith("browser_"):
                name = val
                break
    if arguments is None:
        for val in pargs:
            if isinstance(val, dict):
                arguments = val
                break
    if not isinstance(arguments, MutableMapping):
        arguments = {}
    return name, arguments


def _sanitize_mcp_result(value: Any, *, redact_all_values: bool = False, depth: int = 0) -> Any:
    """Redact native results and JSON embedded in MCP text content."""
    if depth > 10:
        return "[truncated-depth]"
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if _SENSITIVE_KEY_RE.search(key_s) or (redact_all_values and key_s.lower() == "value"):
                out[key_s] = _redacted(item)
                continue
            if key_s == "text" and isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except Exception:
                    out[key_s] = item
                else:
                    out[key_s] = json.dumps(
                        _sanitize_mcp_result(parsed, redact_all_values=redact_all_values, depth=depth + 1),
                        ensure_ascii=False,
                    )
            else:
                out[key_s] = _sanitize_mcp_result(item, redact_all_values=redact_all_values, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_mcp_result(x, redact_all_values=redact_all_values, depth=depth + 1) for x in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return value
        return json.dumps(
            _sanitize_mcp_result(parsed, redact_all_values=redact_all_values, depth=depth + 1),
            ensure_ascii=False,
        )
    return value


def _postprocess(tool: str, args: Mapping[str, Any], result: Any) -> Any:
    raw_authorized = _raw_cookie_authorized(args)
    force_cookie_redaction = tool.startswith("browser_cookie") or tool in {"browser_cookies", "browser_login"}
    force_storage_redaction = tool in {"browser_localstorage", "browser_sessionstorage"} and not (
        _env_bool("BROWSER_ALLOW_RAW_STORAGE_VALUES", False) and bool(args.get("confirm_sensitive_data"))
    )
    if (force_cookie_redaction and not raw_authorized) or force_storage_redaction:
        return _sanitize_mcp_result(result, redact_all_values=True)
    return result


def _wrap_dispatch(fn: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*pargs: Any, **kwargs: Any) -> Any:
            tool, arguments = _extract_invocation(fn, pargs, kwargs)
            if tool:
                try:
                    preflight(tool, arguments)
                except Exception as exc:
                    _audit(tool, arguments, "deny", str(exc))
                    raise
                _audit(tool, arguments, "allow")
            result = await fn(*pargs, **kwargs)
            return _postprocess(tool, arguments, result) if tool else result
        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*pargs: Any, **kwargs: Any) -> Any:
        tool, arguments = _extract_invocation(fn, pargs, kwargs)
        if tool:
            try:
                preflight(tool, arguments)
            except Exception as exc:
                _audit(tool, arguments, "deny", str(exc))
                raise
            _audit(tool, arguments, "allow")
        result = fn(*pargs, **kwargs)
        return _postprocess(tool, arguments, result) if tool else result
    return wrapper


def _find_dispatch(namespace: Mapping[str, Any]) -> Optional[str]:
    preferred = (
        "execute_tool", "_execute_tool", "run_tool", "_run_tool", "dispatch_tool", "_dispatch_tool",
        "handle_tool_call", "_handle_tool_call", "call_tool", "_call_tool",
    )
    for name in preferred:
        fn = namespace.get(name)
        if callable(fn) and not getattr(fn, "__hba_security_wrapped__", False):
            return name
    for name, fn in namespace.items():
        if not callable(fn) or getattr(fn, "__hba_security_wrapped__", False):
            continue
        try:
            source = inspect.getsource(fn)
        except Exception:
            continue
        hits = sum(token in source for token in ("browser_exec", "browser_cookie_list", "browser_navigate", "browser_login"))
        if hits >= 3:
            return name
    return None


def _safe_page_capsule(tab_id: str, page_data: Mapping[str, Any]) -> Dict[str, Any]:
    data = _sanitize(dict(page_data))
    for forbidden in ("cookies", "cookie", "localStorage", "sessionStorage", "headers", "authorization", "password"):
        data.pop(forbidden, None)
    url = str(data.get("url") or "")[:4096]
    title = str(data.get("title") or "")[:500]
    text = str(data.get("text") or data.get("summary") or data.get("content") or "")[:12000]
    artifact_path = str(data.get("path") or data.get("artifact_path") or "")[:4096]
    captured_at = int(time.time())
    digest = hashlib.sha256((url + "\n" + title + "\n" + text).encode("utf-8", errors="replace")).hexdigest()
    event_id = "browser_" + digest[:16] + "_" + secrets.token_hex(4)
    return {
        "schema": "hermes.browser_memory_bridge.v1",
        "event_id": event_id,
        "captured_at": captured_at,
        "tab_id": str(tab_id or "")[:200],
        "url": url,
        "domain": _domain_from(url),
        "title": title,
        "summary": text,
        "artifact_path": artifact_path,
        "content_hash": digest,
        "source": "browser-automation-mcp",
    }


def persist_to_wiki(tab_id: str, page_data: Dict[str, Any]) -> Optional[str]:
    """Queue a redacted, durable page capsule for MemoryWikiProvider ingestion."""
    try:
        capsule = _safe_page_capsule(tab_id, page_data)
        path = _bridge_root() / "inbox" / f"{capsule['captured_at']}-{capsule['event_id']}.json"
        _atomic_json(path, capsule)
        _audit("memory_bridge.persist", {"url": capsule["url"], "event_id": capsule["event_id"]}, "allow")
        return str(capsule["event_id"])
    except Exception as exc:
        _audit("memory_bridge.persist", {}, "deny", type(exc).__name__)
        return None


def recall_from_wiki(url_pattern: str) -> Optional[Dict[str, Any]]:
    """Read the redacted bridge index emitted by the Memory-Wiki overlay."""
    needle = str(url_pattern or "").strip().lower()
    if not needle:
        return {"found": False, "results": []}
    index = _bridge_root() / "index.jsonl"
    if not index.exists() or index.is_symlink():
        return {"found": False, "results": []}
    results = []
    try:
        with index.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-2000:]
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except Exception:
                continue
            haystack = " ".join(str(item.get(k) or "") for k in ("url", "domain", "title", "summary")).lower()
            if needle in haystack:
                results.append(_sanitize(item))
                if len(results) >= 5:
                    break
        return {"found": bool(results), "results": results}
    except Exception as exc:
        return {"found": False, "results": [], "error": type(exc).__name__}


def _patch_tool_schemas(namespace: MutableMapping[str, Any]) -> None:
    tools = namespace.get("TOOLS")
    if not isinstance(tools, list):
        return
    for spec in tools:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        schema = spec.get("inputSchema") or {}
        props = schema.setdefault("properties", {}) if isinstance(schema, dict) else {}
        if name in {"browser_exec", "browser_cookies", "browser_localstorage", "browser_sessionstorage", "browser_network_har"}:
            props.setdefault("confirm_sensitive_data", {
                "type": "boolean", "default": False,
                "description": "Explicit confirmation for operator-enabled sensitive output",
            })
        if name == "browser_login":
            spec["description"] = (
                "Авторизация через credential_ref. Plaintext username/password отключены по умолчанию; "
                "cookies редактируются по умолчанию."
            )
        if name == "browser_recall":
            spec["description"] = "Поиск ранее сохранённых redacted browser-capsules в Memory Wiki bridge."


def install(namespace: MutableMapping[str, Any], *, strict: bool = True) -> Dict[str, Any]:
    """Install overlay into server.py globals after all functions are defined."""
    if namespace.get("_HBA_SECURITY_OVERLAY_INSTALLED"):
        return dict(namespace.get("_HBA_SECURITY_OVERLAY_STATUS") or {})
    namespace["_persist_to_wiki"] = persist_to_wiki
    namespace["_recall_from_wiki"] = recall_from_wiki
    _patch_tool_schemas(namespace)
    dispatch_name = _find_dispatch(namespace)
    if dispatch_name:
        wrapped = _wrap_dispatch(namespace[dispatch_name])
        setattr(wrapped, "__hba_security_wrapped__", True)
        namespace[dispatch_name] = wrapped
    status = {
        "version": OVERLAY_VERSION,
        "dispatch": dispatch_name or "",
        "bridge": str(_bridge_root()),
        "strict": bool(strict),
    }
    namespace["_HBA_SECURITY_OVERLAY_INSTALLED"] = True
    namespace["_HBA_SECURITY_OVERLAY_STATUS"] = status
    if strict and not dispatch_name:
        raise RuntimeError("browser security overlay could not locate MCP tool dispatcher")
    return status
