#!/usr/bin/env python3
"""Apply the Browser Automation MCP hardening + Memory Wiki bridge overlay.

The overlay deliberately leaves hermes-memory-wiki/__init__.py monolithic and
unchanged. Browser Automation talks to the existing memory-wiki MCP wrapper.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import os
from pathlib import Path
import py_compile
import re
import shutil
import shlex
import stat
import sys
from typing import Iterable

MARKER = "HERMES_BROWSER_OVERLAY_20260725"


class PatchError(RuntimeError):
    pass


def _bool_env_expr(name: str, default: str = "0") -> str:
    return (
        f'os.environ.get("{name}", "{default}").lower() '
        'in {"1", "true", "yes", "on"}'
    )


def _replace_once(
    text: str,
    pattern: str,
    replacement: str,
    *,
    label: str,
    flags: int = 0,
    literal: bool = False,
) -> str:
    repl = (lambda _match: replacement) if literal else replacement
    new, count = re.subn(pattern, repl, text, count=1, flags=flags)
    if count != 1:
        raise PatchError(f"{label}: expected exactly one match, got {count}")
    return new


def _insert_after_import(text: str, import_line: str, extra: str) -> str:
    if extra.strip() in text:
        return text
    needle = import_line + "\n"
    if needle not in text:
        raise PatchError(f"Cannot find import anchor: {import_line}")
    return text.replace(needle, needle + extra + "\n", 1)


def _replace_cdp_connection(text: str) -> str:
    replacement = r'''class CDPConnection:
    """Thread-safe CDP connection with one reader and bounded reconnect.

    Invariants:
    - only the reader thread calls recv() after bootstrap;
    - one response resolves only its own Future;
    - pending calls are rejected only on disconnect/close;
    - reconnect state is distinct from permanent close.
    """

    def __init__(self, ws_url: str, timeout: int = TIMEOUT):
        self.ws_url = ws_url
        self.timeout = timeout
        self.send_lock = threading.Lock()
        self.next_id = _ws_counter
        self.pending: Dict[int, concurrent.futures.Future] = {}
        self.pending_lock = threading.Lock()
        self.event_subscribers: Dict[str, list] = {}
        self.event_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._connected = threading.Event()
        self._reconnecting = threading.Event()
        self._permanently_closed = threading.Event()
        self._enabled_domains: set = set()
        self.ws = self._open_socket()

        # Bootstrap before the reader starts; therefore this is the only recv().
        for domain in ["Page", "Network", "Runtime", "DOM"]:
            try:
                self._raw_send_and_wait(domain, f"{domain}.enable")
                self._enabled_domains.add(domain)
            except Exception:
                pass

        self.ws.settimeout(0.5)
        self._connected.set()
        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name=f"cdp-reader-{next(_ws_counter)}",
        )
        self.reader_thread.start()

    def _open_socket(self):
        ws_kwargs: Dict[str, Any] = {"timeout": self.timeout}
        if TOKEN:
            ws_kwargs["header"] = [f"Authorization: Bearer {TOKEN}"]
        return websocket.create_connection(self.ws_url, **ws_kwargs)

    def call(self, method: str, params: dict = None, timeout: float = None) -> dict:
        timeout = timeout or self.timeout
        if self._stop_requested.is_set() or self._permanently_closed.is_set():
            raise RuntimeError(CDP_DISCONNECTED)

        # During a short reconnect, wait for the same connection object rather
        # than letting the manager create a competing WebSocket.
        if not self._connected.is_set():
            wait_for = min(float(timeout), max(1.0, RECONNECT_MAX_DELAY))
            if not self._connected.wait(wait_for):
                raise RuntimeError(CDP_DISCONNECTED)

        cid = next(self.next_id)
        fut = concurrent.futures.Future()
        with self.pending_lock:
            self.pending[cid] = fut

        message = json.dumps({"id": cid, "method": method, "params": params or {}})
        try:
            with self.send_lock:
                if not self._connected.is_set():
                    raise RuntimeError(CDP_DISCONNECTED)
                self.ws.send(message)
        except Exception as exc:
            with self.pending_lock:
                self.pending.pop(cid, None)
            raise RuntimeError(f"CDP send failed [{method}]: {exc}") from exc

        try:
            msg = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            with self.pending_lock:
                self.pending.pop(cid, None)
            raise TimeoutError(f"CDP command '{method}' timed out after {timeout}s") from exc

        if isinstance(msg, dict) and "error" in msg:
            err = msg["error"]
            raise RuntimeError(f"CDP error [{method}]: {err.get('message', str(err))}")
        return msg.get("result", msg) if isinstance(msg, dict) else msg

    def subscribe(self, method: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.event_lock:
            self.event_subscribers.setdefault(method, []).append(q)
        return q

    def unsubscribe(self, method: str, q: queue.Queue):
        with self.event_lock:
            subs = self.event_subscribers.get(method, [])
            if q in subs:
                subs.remove(q)

    @property
    def is_reconnecting(self) -> bool:
        return self._reconnecting.is_set() and not self._stop_requested.is_set()

    @property
    def is_alive(self) -> bool:
        return (
            not self._stop_requested.is_set()
            and not self._permanently_closed.is_set()
            and (self._connected.is_set() or self._reconnecting.is_set())
        )

    def close(self):
        self._permanently_closed.set()
        self._stop_requested.set()
        self._connected.clear()
        self._reconnecting.clear()
        self._reject_all_pending(CDP_DISCONNECTED)
        try:
            self.ws.close()
        except Exception:
            pass

    def _raw_send_and_wait(self, _key: str, method: str, params: dict = None, timeout: float = 5) -> dict:
        cid = next(self.next_id)
        with self.send_lock:
            self.ws.send(json.dumps({"id": cid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            self.ws.settimeout(remaining)
            try:
                raw = self.ws.recv()
            except (websocket.WebSocketTimeoutException, TimeoutError) as exc:
                raise TimeoutError(f"CDP bootstrap command '{method}' timed out") from exc
            msg = json.loads(raw)
            mid = msg.get("id")
            evt_method = msg.get("method")
            if mid == cid:
                self.ws.settimeout(0.5)
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(f"CDP error [{method}]: {err.get('message', str(err))}")
                return msg.get("result", {})
            if evt_method is not None:
                self._dispatch_event(evt_method, msg)
        raise TimeoutError(f"CDP bootstrap command '{method}' timed out after {timeout}s")

    def _dispatch_event(self, method: str, msg: dict) -> None:
        with self.event_lock:
            subs = list(self.event_subscribers.get(method, []))
        for subscriber in subs:
            try:
                subscriber.put_nowait(msg)
            except queue.Full:
                METRICS.incr("cdp_event_queue_drops")

    def _reconnect(self) -> bool:
        attempts = max(1, int(os.environ.get("BROWSER_RECONNECT_ATTEMPTS", "5")))
        delay = RECONNECT_BASE_DELAY
        self._reconnecting.set()
        self._connected.clear()
        try:
            for attempt in range(attempts):
                if self._stop_requested.is_set():
                    return False
                if attempt:
                    time.sleep(delay)
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
                try:
                    new_ws = self._open_socket()
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = new_ws
                    for domain in list(self._enabled_domains):
                        self._raw_send_and_wait(domain, f"{domain}.enable")
                    self.ws.settimeout(0.5)
                    self._connected.set()
                    METRICS.incr("cdp_reconnects")
                    return True
                except Exception as exc:
                    _log(
                        f"CDP reconnect attempt {attempt + 1}/{attempts} failed: {exc}",
                        level="warn",
                    )
            return False
        finally:
            self._reconnecting.clear()

    def _reader_loop(self):
        while not self._stop_requested.is_set():
            try:
                raw = self.ws.recv()
                if raw is None:
                    raise ConnectionError("WebSocket closed by remote")
                msg = json.loads(raw)
            except (websocket.WebSocketTimeoutException, TimeoutError):
                continue
            except json.JSONDecodeError as exc:
                _log(f"CDP returned invalid JSON: {exc}", level="warn")
                continue
            except (websocket.WebSocketConnectionClosedException, ConnectionError, OSError) as exc:
                if self._stop_requested.is_set():
                    break
                _log(f"CDP reader disconnected: {exc}", level="warn")
                self._connected.clear()
                self._reject_all_pending(CDP_DISCONNECTED)
                if self._reconnect():
                    continue
                self._permanently_closed.set()
                self._stop_requested.set()
                break
            except Exception as exc:
                _log(f"CDP reader fatal error: {exc}", level="error")
                self._connected.clear()
                self._reject_all_pending(CDP_DISCONNECTED)
                self._permanently_closed.set()
                self._stop_requested.set()
                break

            mid = msg.get("id")
            method = msg.get("method")
            if mid is not None:
                with self.pending_lock:
                    fut = self.pending.pop(mid, None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif method is not None:
                self._dispatch_event(method, msg)

        self._connected.clear()
        self._reject_all_pending(CDP_DISCONNECTED)

    def _reject_all_pending(self, reason: str):
        with self.pending_lock:
            pending = dict(self.pending)
            self.pending.clear()
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))

class CDPConnectionManager:'''
    return _replace_once(
        text,
        r"class CDPConnection:\n.*?\nclass CDPConnectionManager:",
        replacement,
        label="replace CDPConnection",
        flags=re.S,
        literal=True,
    )


def _memory_bridge_block() -> str:
    return r'''# ====== MEMORY-WIKI INTEGRATION (P1) ======
# HERMES_BROWSER_OVERLAY_20260725: use the repository's existing MCP wrapper,
# not a fictional HTTP /api/claims endpoint.
class _MemoryWikiMCPClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._next_id = itertools.count(1)
        self._initialized = False

    def _command(self) -> List[str]:
        configured = os.environ.get("BROWSER_MEMORY_WIKI_MCP_CMD", "").strip()
        if configured:
            return shlex.split(configured)
        hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        wrapper = os.path.join(hermes_home, "plugins", "memory-wiki", "mcp-wrapper", "server.py")
        return [sys.executable, wrapper]

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        self._initialized = False
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        command = self._command()
        if not command:
            raise RuntimeError("empty memory-wiki MCP command")
        script_args = [arg for arg in command[1:] if str(arg).endswith(".py")]
        if script_args and not os.path.exists(script_args[0]):
            raise RuntimeError(
                "memory-wiki MCP wrapper not found; set BROWSER_MEMORY_WIKI_MCP_CMD"
            )
        env = os.environ.copy()
        plugin_path = env.get("BROWSER_MEMORY_WIKI_PLUGIN_PATH", "").strip()
        if plugin_path:
            env["MW_PLUGIN_PATH"] = plugin_path
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        self._initialized = False

    def _readline_with_timeout(self, timeout: float) -> str:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("memory-wiki MCP process is not running")
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def reader():
            try:
                result_q.put((True, self._proc.stdout.readline()))
            except Exception as exc:
                result_q.put((False, exc))

        threading.Thread(target=reader, daemon=True).start()
        try:
            ok, value = result_q.get(timeout=timeout)
        except queue.Empty as exc:
            self._stop()
            raise TimeoutError("memory-wiki MCP response timed out") from exc
        if not ok:
            raise RuntimeError(str(value))
        if not value:
            self._stop()
            raise RuntimeError("memory-wiki MCP process closed stdout")
        return value

    def _request(self, method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self._ensure_started()
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("memory-wiki MCP stdin unavailable")
        request_id = next(self._next_id)
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        response = json.loads(self._readline_with_timeout(timeout))
        if response.get("id") != request_id:
            raise RuntimeError("memory-wiki MCP response id mismatch")
        if "error" in response:
            raise RuntimeError(response["error"].get("message", str(response["error"])))
        return response.get("result", {})

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        timeout = max(1.0, float(MEMORY_WIKI_TIMEOUT))
        with self._lock:
            if not self._initialized:
                self._request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "browser-automation-mcp", "version": SERVER_VERSION},
                    },
                    timeout,
                )
                self._initialized = True
            result = self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
                timeout,
            )
        content = result.get("content") or []
        if not content:
            return result
        text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
        try:
            return json.loads(text)
        except Exception:
            return {"text": text}


_MEMORY_WIKI_MCP = _MemoryWikiMCPClient()
atexit.register(_MEMORY_WIKI_MCP._stop)
_WIKI_SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/-]+=*|eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{5,})"
)


def _sanitize_for_wiki(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated-depth]"
    normalized_key = str(key).lower().replace("-", "_")
    explicit_secret_keys = {
        "authorization", "proxy_authorization", "cookie", "cookies",
        "set_cookie", "password", "passwd", "secret", "token",
        "access_token", "refresh_token", "id_token", "api_key",
        "client_secret", "csrf", "xsrf",
    }
    if normalized_key in explicit_secret_keys or _is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        cleaned = {}
        for child_key, child_value in value.items():
            child_name = str(child_key)
            if child_name.lower() in {"data", "base64", "body", "postdata", "requestheaders"}:
                if isinstance(child_value, str) and len(child_value) > 2048:
                    cleaned[child_name] = f"[omitted len={len(child_value)}]"
                    continue
            cleaned[child_name] = _sanitize_for_wiki(child_value, child_name, depth + 1)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_for_wiki(item, key, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        text = _WIKI_SECRET_VALUE_RE.sub("[redacted-secret]", value)
        return text[:MAX_MEMORY_WIKI_TEXT]
    return value


def _persist_to_wiki(tab_id: str, page_data: Dict[str, Any]) -> Optional[str]:
    """Persist a sanitized browser snapshot through mw_add_claim."""
    try:
        safe = _sanitize_for_wiki(page_data)
        url = str(safe.get("url") or safe.get("final_url") or "")
        title = str(safe.get("title") or "")[:300]
        captured_at = int(time.time())
        canonical = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        evidence = {
            "schema": "browser_snapshot.v1",
            "tab_id": tab_id,
            "url": url,
            "title": title,
            "captured_at": captured_at,
            "content_hash": content_hash,
            "snapshot": safe,
        }
        claim = f"Browser snapshot [{content_hash[:16]}]: {title or url or 'untitled'}"
        result = _MEMORY_WIKI_MCP.call_tool(
            "mw_add_claim",
            {
                "claim": claim,
                "topic": "browser",
                "evidence": json.dumps(evidence, ensure_ascii=False, default=str),
                "source": "browser-automation-mcp",
                "confidence": 0.85,
                "salience": 0.65,
            },
        )
        if isinstance(result, dict):
            return result.get("id") or result.get("claim_id")
        return None
    except Exception as exc:
        _log(f"persist_to_wiki failed: {exc}", level="warn")
        return None


def _recall_from_wiki(url_pattern: str) -> Optional[Dict[str, Any]]:
    """Recall previously stored browser material through mw_query."""
    try:
        result = _MEMORY_WIKI_MCP.call_tool(
            "mw_query",
            {"query": str(url_pattern), "limit": 5},
        )
        if isinstance(result, dict):
            rows = result.get("results") or result.get("claims") or result.get("items") or []
            return {"found": bool(rows), "results": rows, "raw": result}
        return {"found": False, "raw": result}
    except Exception as exc:
        _log(f"recall_from_wiki failed: {exc}", level="warn")
        return {"found": False, "error": str(exc)}

# ====== UTILITY HELPERS (unchanged from v1.4) ======'''


def _replace_memory_bridge(text: str) -> str:
    return _replace_once(
        text,
        r"# ====== MEMORY-WIKI INTEGRATION \(P1\) ======.*?# ====== UTILITY HELPERS \(unchanged from v1\.4\) ======",
        _memory_bridge_block(),
        label="replace memory-wiki bridge",
        flags=re.S,
        literal=True,
    )


def _replace_artifact_writers(text: str) -> str:
    replacement = r'''def _write_b64_artifact(prefix: str, ext: str, data_b64: str) -> Dict[str, Any]:
    raw = base64.b64decode(data_b64.encode("ascii"), validate=False) if data_b64 else b""
    return _write_artifact_bytes(prefix, ext, raw)


def _write_text_artifact(prefix: str, ext: str, text: str) -> Dict[str, Any]:
    return _write_artifact_bytes(prefix, ext, (text or "").encode("utf-8"))


def _write_artifact_bytes(prefix: str, ext: str, data: bytes) -> Dict[str, Any]:
    path = _artifact_path(prefix, ext)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    remove_on_error = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            remove_on_error = True
            raise BrowserError("SECURITY", "Artifact target is not a regular file")
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                remove_on_error = True
                raise OSError("short artifact write")
            written += count
        os.fsync(fd)
    except Exception:
        remove_on_error = True
        raise
    finally:
        os.close(fd)
        if remove_on_error:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    return {"path": path, "bytes": len(data), "media_hint": _media_hint(ext)}


def _public_tab'''
    return _replace_once(
        text,
        r"def _write_b64_artifact\(.*?\n\s*def _public_tab",
        replacement,
        label="replace artifact writers",
        flags=re.S,
        literal=True,
    )


def _patch_allowlist_dns(text: str) -> str:
    # Allowlisting bypasses hostname policy, not the private-address check.
    pattern = (
        r'(?m)^(\s*)if hostname in NAVIGATION_ALLOWED_HOSTS:\n'
        r'\1\s+return True, "allowlisted"'
    )
    replacement = r'\1allowlisted = hostname in NAVIGATION_ALLOWED_HOSTS'
    if re.search(pattern, text):
        text = re.sub(pattern, replacement, text, count=1)
        text = _replace_once(
            text,
            r'(?m)^(\s*)return True, "ok"\s*$',
            r'\1return True, "allowlisted" if allowlisted else "ok"',
            label="preserve allowlisted status after DNS checks",
        )
    elif "allowlisted = hostname in NAVIGATION_ALLOWED_HOSTS" not in text:
        raise PatchError("navigation allowlist block not found")
    return text


def _remove_plaintext_login_schema(text: str) -> str:
    for field in ("username", "password"):
        pattern = (
            rf'(?ms)^\s*"{field}":\s*\{{[^\n]*DEPRECATED[^\n]*\}},?\s*\n'
        )
        text, count = re.subn(pattern, "", text, count=1)
        if count == 0:
            # Tolerate an already-patched schema.
            login_start = text.find('{"name": "browser_login"')
            login_end = text.find('{"name":', login_start + 5)
            segment = text[login_start:login_end if login_end > 0 else None]
            if f'"{field}"' in segment and "DEPRECATED" in segment:
                raise PatchError(f"could not remove deprecated {field} field")
    return text


def _insert_config(text: str) -> str:
    anchor = 'MEMORY_WIKI_TIMEOUT = int(os.environ.get("BROWSER_MEMORY_WIKI_TIMEOUT", "5"))\n'
    block = f'''MEMORY_WIKI_TIMEOUT = int(os.environ.get("BROWSER_MEMORY_WIKI_TIMEOUT", "8"))
MAX_MEMORY_WIKI_TEXT = int(os.environ.get("BROWSER_MEMORY_WIKI_MAX_TEXT", "12000"))
ALLOW_BROWSER_EXEC = {_bool_env_expr("BROWSER_ALLOW_EXEC")}
ALLOW_RAW_CDP = {_bool_env_expr("BROWSER_ALLOW_RAW_CDP")}
# {MARKER}
'''
    if MARKER in text:
        return text
    if anchor not in text:
        raise PatchError("memory-wiki config anchor not found")
    return text.replace(anchor, block, 1)


def _insert_cookie_validator(text: str) -> str:
    if "def _validate_cookie_arguments" in text:
        return text
    marker = "# ====== TOOLS ======"
    if marker not in text:
        raise PatchError("TOOLS marker not found")
    helper = r'''def _validate_cookie_arguments(args: Dict[str, Any]) -> None:
    same_site = str(args.get("sameSite", "Lax"))
    secure = bool(args.get("secure", True))
    partition_key = args.get("partitionKey")
    if same_site == "None" and not secure:
        raise BrowserError("INVALID_COOKIE", "SameSite=None requires secure=true")
    if partition_key and not secure:
        raise BrowserError("INVALID_COOKIE", "Partitioned cookies require secure=true")
    url = str(args.get("url") or "")
    if url:
        ok, reason = _validate_url(url)
        if not ok:
            raise BrowserError("NAVIGATION_BLOCKED", f"Cookie URL blocked: {reason}")


'''
    return text.replace(marker, helper + marker, 1)


def _tools_assignment_end(text: str) -> int:
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: Iterable[ast.expr]
            if isinstance(node, ast.Assign):
                targets = node.targets
            else:
                targets = [node.target]
            if any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in targets):
                lines = text.splitlines(keepends=True)
                return sum(len(line) for line in lines[: node.end_lineno])
    raise PatchError("TOOLS assignment not found")


def _filter_privileged_tools(text: str) -> str:
    marker = "# HERMES_BROWSER_PRIVILEGED_TOOL_FILTER"
    if marker in text:
        return text
    offset = _tools_assignment_end(text)
    block = '''\n# HERMES_BROWSER_PRIVILEGED_TOOL_FILTER
if not ALLOW_BROWSER_EXEC:
    TOOLS = [tool for tool in TOOLS if tool.get("name") != "browser_exec"]
if not ALLOW_RAW_CDP:
    TOOLS = [tool for tool in TOOLS if tool.get("name") != "browser_cdp"]
'''
    return text[:offset] + block + text[offset:]


def _find_dispatch_function(text: str):
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        comparisons = []
        for child in ast.walk(node):
            if isinstance(child, ast.Compare) and len(child.ops) == 1 and len(child.comparators) == 1:
                right = child.comparators[0]
                if isinstance(right, ast.Constant) and right.value in {"browser_exec", "browser_cdp", "browser_cookie_set"}:
                    comparisons.append(child)
        values = {
            c.comparators[0].value
            for c in comparisons
            if isinstance(c.comparators[0], ast.Constant)
        }
        if {"browser_exec", "browser_cdp"}.issubset(values):
            name_expr = None
            for comp in comparisons:
                if isinstance(comp.left, ast.Name):
                    name_expr = comp.left.id
                    break
            if not name_expr:
                continue
            params = [arg.arg for arg in node.args.args if arg.arg != "self"]
            arg_expr = next((p for p in params if p != name_expr and p in {"args", "arguments", "tool_args"}), None)
            if arg_expr is None:
                arg_expr = next((p for p in params if p != name_expr), "args")
            return node, name_expr, arg_expr
    return None


def _insert_runtime_guards(text: str) -> str:
    marker = "HERMES_BROWSER_RUNTIME_GUARDS"
    if marker in text:
        return text
    found = _find_dispatch_function(text)
    if not found:
        raise PatchError("tool dispatch function not found; runtime guards were not installed")
    node, tool_name, tool_args = found
    lines = text.splitlines(keepends=True)
    first_body_line = node.body[0].lineno
    offset = sum(len(line) for line in lines[: first_body_line - 1])
    indent = " " * (node.col_offset + 4)
    guard = (
        f'{indent}# {marker}\n'
        f'{indent}if {tool_name} == "browser_exec" and not ALLOW_BROWSER_EXEC:\n'
        f'{indent}    raise BrowserError("CAPABILITY_DISABLED", "browser_exec is disabled; set BROWSER_ALLOW_EXEC=1")\n'
        f'{indent}if {tool_name} == "browser_cdp" and not ALLOW_RAW_CDP:\n'
        f'{indent}    raise BrowserError("CAPABILITY_DISABLED", "browser_cdp is disabled; set BROWSER_ALLOW_RAW_CDP=1")\n'
        f'{indent}if {tool_name} == "browser_cookie_set":\n'
        f'{indent}    _validate_cookie_arguments({tool_args})\n'
    )
    return text[:offset] + guard + text[offset:]


def _patch_heartbeat(text: str) -> str:
    if "if conn.is_reconnecting:" in text:
        return text
    pattern = r'(?m)^(\s*)for tab_id, conn in connections:\n\1\s+try:'
    replacement = (
        r'\1for tab_id, conn in connections:\n'
        r'\1    if conn.is_reconnecting:\n'
        r'\1        continue\n'
        r'\1    try:'
    )
    new, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise PatchError(f"heartbeat loop: expected one match, got {count}")
    return new


def patch_browser_server(text: str) -> str:
    overlay_markers = {
        MARKER,
        "HERMES_BROWSER_PRIVILEGED_TOOL_FILTER",
        "HERMES_BROWSER_RUNTIME_GUARDS",
        "class _MemoryWikiMCPClient",
        "def _validate_cookie_arguments",
    }
    present = {marker for marker in overlay_markers if marker in text}
    if present == overlay_markers:
        return text
    if present:
        missing = ", ".join(sorted(overlay_markers - present))
        raise PatchError(f"partial overlay detected; missing markers: {missing}")
    text = _insert_after_import(text, "import subprocess", "import atexit\nimport shlex\nimport stat")
    text = _insert_config(text)
    text = _patch_allowlist_dns(text)
    text = _replace_cdp_connection(text)
    text = _patch_heartbeat(text)
    text = _replace_memory_bridge(text)
    text = _replace_artifact_writers(text)
    text = _insert_cookie_validator(text)
    text = _remove_plaintext_login_schema(text)
    text = _filter_privileged_tools(text)
    text = _insert_runtime_guards(text)
    ast.parse(text)
    return text


def _discover_repo(explicit: str | None, candidates: list[Path], required: Path) -> Path:
    if explicit:
        repo = Path(explicit).expanduser().resolve()
        if not (repo / required).exists():
            raise PatchError(f"Required file not found: {repo / required}")
        return repo
    for candidate in candidates:
        candidate = candidate.expanduser().resolve()
        if (candidate / required).exists():
            return candidate
    raise PatchError(f"Repository not found; pass an explicit path containing {required}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-repo", help="Path to browser-automation-mcp")
    parser.add_argument("--memory-repo", help="Path to hermes-memory-wiki")
    parser.add_argument("--check", action="store_true", help="Validate only; do not write")
    args = parser.parse_args()

    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    browser_repo = _discover_repo(
        args.browser_repo,
        [
            Path.cwd() / "browser-automation-mcp",
            home / "plugins" / "browser-automation-mcp",
            home / "mcp" / "browser-automation-mcp",
            Path.home() / "browser-automation-mcp",
        ],
        Path("server.py"),
    )
    memory_repo = _discover_repo(
        args.memory_repo,
        [
            Path.cwd() / "hermes-memory-wiki",
            home / "plugins" / "memory-wiki",
            Path.home() / "hermes-memory-wiki",
        ],
        Path("__init__.py"),
    )

    wrapper = memory_repo / "mcp-wrapper" / "server.py"
    schemas = memory_repo / "mcp-wrapper" / "tool_schemas.json"
    if not wrapper.exists() or not schemas.exists():
        raise PatchError("memory-wiki mcp-wrapper/server.py or tool_schemas.json is missing")

    server_file = browser_repo / "server.py"
    original = server_file.read_text(encoding="utf-8")
    patched = patch_browser_server(original)

    if args.check:
        print(f"OK: overlay applicable to {server_file}")
        print(f"OK: memory-wiki __init__.py remains untouched: {memory_repo / '__init__.py'}")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = browser_repo / ".overlay-backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(server_file, backup_dir / "server.py")

    # Validate a temporary file first, then atomically replace server.py.
    original_mode = stat.S_IMODE(server_file.stat().st_mode)
    temp_file = browser_repo / f".server.py.overlay.{os.getpid()}.tmp"
    try:
        temp_file.write_text(patched, encoding="utf-8")
        os.chmod(temp_file, original_mode)
        py_compile.compile(str(temp_file), doraise=True)
        os.replace(temp_file, server_file)
    finally:
        try:
            temp_file.unlink()
        except FileNotFoundError:
            pass

    env_file = browser_repo / "browser-memory-wiki-overlay.env.example"
    env_file.write_text(
        "\n".join(
            [
                "# Privileged tools are disabled by default",
                "BROWSER_ALLOW_EXEC=0",
                "BROWSER_ALLOW_RAW_CDP=0",
                "BROWSER_RECONNECT_ATTEMPTS=5",
                "BROWSER_MEMORY_WIKI_TIMEOUT=8",
                "BROWSER_MEMORY_WIKI_MAX_TEXT=12000",
                f"BROWSER_MEMORY_WIKI_MCP_CMD={shlex.quote(sys.executable + ' ' + str(wrapper))}",
                f"BROWSER_MEMORY_WIKI_PLUGIN_PATH={shlex.quote(str(memory_repo / '__init__.py'))}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"PATCHED: {server_file}")
    print(f"BACKUP:  {backup_dir / 'server.py'}")
    print(f"ENV:     {env_file}")
    print(f"UNCHANGED: {memory_repo / '__init__.py'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
