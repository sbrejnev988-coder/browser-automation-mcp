#!/usr/bin/env python3
"""
Browser Automation MCP Server v1.5.1 — P0 Hardened
===================================================
v1.5.1 fixes: CDP dispatcher, tab safety, cookie API, browser_login security, 
navigation policy, HAR redaction, WebSocket auth, artifact hardening.

Direct Chrome DevTools Protocol WebSocket client — no public BrowserMCP.
"""

import base64
import hashlib
import json
import os
import re
import sys
import time
import socket
import secrets
import subprocess
import threading
import itertools
import queue
import ssl
import ipaddress
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ====== CONFIG ======
CDP = os.environ.get("BROWSER_CDP_URL", "http://127.0.0.1:9223").rstrip("/")
TOKEN = os.environ.get("BROWSER_AUTH_TOKEN", os.environ.get("CODEX_DEBUG_TOKEN", ""))
TIMEOUT = int(os.environ.get("BROWSER_TIMEOUT", "30"))
HTTP_TIMEOUT = int(os.environ.get("BROWSER_HTTP_TIMEOUT", "10"))
DEFAULT_TAB_URL = "about:blank"
ARTIFACT_DIR = os.environ.get("BROWSER_ARTIFACT_DIR", os.path.expanduser("~/.hermes/cache/documents/browser-automation"))
AUTOSTART_CDP = os.environ.get("BROWSER_AUTOSTART_CDP", "1").lower() not in {"0", "false", "no", "off"}
CDP_START_CMD = os.environ.get("BROWSER_CDP_START_CMD", os.path.expanduser("~/.local/bin/browser-cdp-start"))
CDP_START_TIMEOUT = int(os.environ.get("BROWSER_CDP_START_TIMEOUT", "45"))
SERVER_VERSION = "1.5.1"

# P0: Resilience
HEARTBEAT_INTERVAL = int(os.environ.get("BROWSER_HEARTBEAT_SEC", "15"))
RECONNECT_BASE_DELAY = float(os.environ.get("BROWSER_RECONNECT_BASE_S", "1.0"))
RECONNECT_MAX_DELAY = float(os.environ.get("BROWSER_RECONNECT_MAX_S", "30.0"))
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("BROWSER_CB_THRESHOLD", "5"))
CIRCUIT_BREAKER_TIMEOUT_S = int(os.environ.get("BROWSER_CB_OPEN_S", "30"))

# P0: Navigation security
NAVIGATION_ALLOWED_HOSTS = set(
    h.strip() for h in os.environ.get("BROWSER_ALLOWED_HOSTS", "").split(",") if h.strip()
)

# P0: Observability
METRICS_ENABLED = os.environ.get("BROWSER_METRICS", "1").lower() not in {"0", "false", "no", "off"}

# P1: Memory-Wiki
MEMORY_WIKI_URL = os.environ.get("BROWSER_MEMORY_WIKI_URL", "http://127.0.0.1:8644")
MEMORY_WIKI_TIMEOUT = int(os.environ.get("BROWSER_MEMORY_WIKI_TIMEOUT", "5"))

# P1: Multi-Context
MAX_TABS = int(os.environ.get("BROWSER_MAX_TABS", "4"))

_cdp_start_attempted = False
# P0: Active tab tracking
_active_tab_id: Optional[str] = None

try:
    import websocket
except ImportError:
    print("[browser-mcp] FATAL: websocket-client not installed. Run: pip install websocket-client", file=sys.stderr)
    sys.exit(1)

# ====== OBSERVABILITY (P0) ======

class Metrics:
    """Thread-safe metrics collector with Prometheus-compatible output."""
    
    def __init__(self):
        self._lock = threading.Lock()
        self.counters: Dict[str, int] = {}
        self.gauges: Dict[str, float] = {}
        self.latencies: Dict[str, List[float]] = {}  # last 100 per key
        self._start_time = time.time()
    
    def incr(self, key: str, delta: int = 1):
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + delta
    
    def gauge(self, key: str, value: float):
        with self._lock:
            self.gauges[key] = value
    
    def observe(self, key: str, seconds: float):
        with self._lock:
            if key not in self.latencies:
                self.latencies[key] = []
            self.latencies[key].append(seconds)
            if len(self.latencies[key]) > 100:
                self.latencies[key] = self.latencies[key][-100:]
    
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            stats = {
                "uptime_seconds": time.time() - self._start_time,
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "latencies": {}
            }
            for key, vals in self.latencies.items():
                if vals:
                    sorted_vals = sorted(vals)
                    stats["latencies"][key] = {
                        "count": len(vals),
                        "p50": sorted_vals[len(vals)//2],
                        "p95": sorted_vals[int(len(vals)*0.95)],
                        "p99": sorted_vals[int(len(vals)*0.99)],
                        "avg": sum(vals) / len(vals),
                    }
            return stats

METRICS = Metrics()

# ====== STRUCTURED LOGGING (P0) ======

def _log(msg: str, level: str = "info", **extra) -> None:
    """JSON structured log to stderr."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int(time.time() % 1 * 1000):03d}Z",
        "level": level,
        "msg": msg,
        "source": "browser-mcp",
        "version": SERVER_VERSION,
    }
    entry.update(extra)
    print(json.dumps(entry, ensure_ascii=False), file=sys.stderr, flush=True)

# ====== CIRCUIT BREAKER (P0) ======

class CircuitBreaker:
    """Fail-fast pattern: OPEN after N failures, HALF_OPEN after timeout, CLOSED on success."""
    
    def __init__(self, name: str, threshold: int = CIRCUIT_BREAKER_THRESHOLD, 
                 open_timeout: int = CIRCUIT_BREAKER_TIMEOUT_S):
        self.name = name
        self.threshold = threshold
        self.open_timeout = open_timeout
        self._failures = 0
        self._last_failure = 0.0
        self._state = "CLOSED"  # CLOSED → OPEN → HALF_OPEN → CLOSED
        self._lock = threading.Lock()
    
    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "OPEN" and time.time() - self._last_failure >= self.open_timeout:
                self._state = "HALF_OPEN"
            return self._state
    
    def allow(self) -> bool:
        return self.state != "OPEN"
    
    def success(self):
        with self._lock:
            self._failures = 0
            self._state = "CLOSED"
            METRICS.gauge(f"cb_{self.name}_state", 0)
    
    def failure(self):
        with self._lock:
            self._failures += 1
            self._last_failure = time.time()
            if self._failures >= self.threshold:
                self._state = "OPEN"
            METRICS.gauge(f"cb_{self.name}_state", 1 if self._state == "OPEN" else 0.5)
            METRICS.incr(f"cb_{self.name}_failures")

CDP_CIRCUIT = CircuitBreaker("cdp")

# ====== HELPERS ======

class BrowserError(Exception):
    """Structured browser error with code and details."""
    def __init__(self, code: str, message: str, details: dict = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

def _send(data: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cdp_probe(timeout: int = 2) -> Optional[Dict[str, Any]]:
    """Return /json/version if CDP is reachable, otherwise None.
    Uses low-level HTTP to avoid recursion with _ensure_cdp_started."""
    url = f"{CDP}/json/version"
    try:
        req = urllib.request.Request(url, method="GET")
        if TOKEN:
            req.add_header("Authorization", f"Bearer {TOKEN}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except Exception:
        return None


def _ensure_cdp_started(reason: str = "request") -> bool:
    """Best-effort CDP autostart; never loops more than once per process."""
    global _cdp_start_attempted
    if _cdp_probe(timeout=2):
        METRICS.gauge("cdp_reachable", 1)
        return True
    METRICS.gauge("cdp_reachable", 0)
    if not AUTOSTART_CDP or _cdp_start_attempted:
        return False
    _cdp_start_attempted = True
    if not CDP_START_CMD:
        return False
    try:
        _log(f"CDP not reachable ({reason}); starting via: {CDP_START_CMD}")
        METRICS.incr("cdp_autostart_attempts")
        proc = subprocess.run(
            ["bash", "-lc", CDP_START_CMD],
            text=True,
            capture_output=True,
            timeout=CDP_START_TIMEOUT,
            env=os.environ.copy(),
        )
        if proc.returncode != 0:
            _log(f"CDP start failed rc={proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}", level="error")
            METRICS.incr("cdp_autostart_failures")
            return False
        deadline = time.time() + 10
        while time.time() < deadline:
            if _cdp_probe(timeout=2):
                METRICS.gauge("cdp_reachable", 1)
                METRICS.incr("cdp_autostart_successes")
                return True
            time.sleep(0.5)
    except Exception as e:
        _log(f"CDP start exception: {e}", level="error")
    return bool(_cdp_probe(timeout=2))


def _http(method: str, path: str, data: Any = None, raw: bool = False, 
          timeout: int = HTTP_TIMEOUT) -> Any:
    """HTTP request to CDP /json endpoints. Tolerates plain-text responses and autostarts CDP once."""
    url = f"{CDP}{path}"

    def make_request(req_method: str) -> urllib.request.Request:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, method=req_method)
        req.add_header("Content-Type", "application/json")
        if TOKEN:
            req.add_header("Authorization", f"Bearer {TOKEN}")
        return req

    def read(req_method: str) -> bytes:
        return urllib.request.urlopen(make_request(req_method), timeout=timeout).read()

    try:
        resp = read(method)
    except urllib.error.HTTPError as e:
        if method.upper() == "PUT" and e.code == 405 and path.startswith("/json/new"):
            resp = read("GET")
        else:
            raise
    except Exception:
        if _ensure_cdp_started(f"{method} {path}"):
            try:
                resp = read(method)
            except urllib.error.HTTPError as e:
                if method.upper() == "PUT" and e.code == 405 and path.startswith("/json/new"):
                    resp = read("GET")
                else:
                    raise
        else:
            raise
    if raw:
        return resp
    if not resp:
        return None
    try:
        return json.loads(resp)
    except json.JSONDecodeError:
        return {"raw": resp.decode("utf-8", errors="replace")}


def _json_arg(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _storage_json_expr(storage: str, key: str = "") -> str:
    if key:
        k = _json_arg(key)
        return f"JSON.stringify({{{k}: {storage}.getItem({k})}})"
    return f"JSON.stringify(Object.assign({{}}, {storage}))"


def _page_tabs() -> List[Dict[str, Any]]:
    tabs = _http("GET", "/json") or []
    return [t for t in tabs if t.get("type") == "page" and not (t.get("url") or "").startswith("devtools://")]

# ====== NAVIGATION POLICY (P0) ======

# Block dangerous URL schemes and networks
_BLOCKED_SCHEMES = {"file", "javascript", "data", "view-source", "about", "chrome", "chrome-extension"}
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

def _validate_url(url: str) -> Tuple[bool, str]:
    """Validate URL against navigation policy. Returns (ok, reason). Checks DNS resolution for SSRF."""
    if not url or not isinstance(url, str):
        return False, "empty url"
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    
    if scheme in _BLOCKED_SCHEMES:
        return False, f"blocked scheme: {scheme}"
    if scheme not in ("http", "https"):
        return False, f"unsupported scheme: {scheme}"
    
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "no hostname"
    
    if hostname in NAVIGATION_ALLOWED_HOSTS:
        return True, "allowlisted"
    
    # P0: Resolve DNS and check ALL addresses for private/loopback SSRF
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        for info in infos:
            addr_str = info[4][0]
            try:
                addr = ipaddress.ip_address(addr_str)
            except ValueError:
                continue
            if addr.is_loopback:
                return False, f"DNS resolves to loopback: {hostname} -> {addr_str}"
            if addr.is_private:
                return False, f"DNS resolves to private IP: {hostname} -> {addr_str}"
            if addr.is_link_local:
                return False, f"DNS resolves to link-local: {hostname} -> {addr_str}"
            if addr.is_multicast or addr.is_reserved:
                return False, f"DNS resolves to reserved: {hostname} -> {addr_str}"
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {hostname}"
    
    return True, "ok"



def _get_tab(args: Optional[Dict[str, Any]] = None, create: bool = True) -> Optional[Dict[str, Any]]:
    """Find a page target by tab_id/url_filter or return/create the first page."""
    global _active_tab_id
    args = args or {}
    tab_id = args.get("tab_id") or args.get("id")
    url_filter = args.get("url_filter")
    tabs = _http("GET", "/json") or []

    if tab_id:
        for t in tabs:
            if t.get("id") == tab_id:
                _active_tab_id = tab_id
                return t
        if not create:
            return None

    if url_filter:
        filt = str(url_filter).lower()
        for t in tabs:
            if t.get("type") == "page" and filt in (t.get("url", "") or "").lower():
                return t

    page_tabs = [t for t in tabs if t.get("type") == "page" and not (t.get("url", "") or "").startswith("devtools://")]
    
    # P0: Resolution order — tab_id > url_filter > active_tab_id > single_tab > AMBIGUOUS
    if not tab_id and not url_filter and _active_tab_id:
        for t in page_tabs:
            if t.get("id") == _active_tab_id:
                return t
    
    # P0: AMBIGUOUS_TARGET when multiple tabs and no explicit selection
    if len(page_tabs) > 1 and not tab_id and not url_filter:
        raise BrowserError(
            code="AMBIGUOUS_TARGET",
            message="Multiple tabs are open; pass tab_id or url_filter",
            details={"tabs": [_public_tab(t) for t in page_tabs]}
        )
    
    if page_tabs:
        tab = page_tabs[0]
        _active_tab_id = tab.get("id")
        return tab

    if create:
        tab = _http("PUT", f"/json/new?{urllib.parse.quote(DEFAULT_TAB_URL, safe='')}")
        if tab:
            _active_tab_id = tab.get("id")
        return tab
    return None


# ====== CONNECTION MANAGER (P0) ======


# ====== CONNECTION MANAGER — P0 REWRITE ======
# Fixes: heartbeat response corruption, ID collisions, missing lock, multiple recv()

# Global monotonic ID counter — shared across ALL connections to avoid ID collisions
_ws_counter = itertools.count(1)

CDP_DISCONNECTED = "CDP_DISCONNECTED"

class CDPConnection:
    """Single CDP connection with proper request/response multiplexing.
    Architecture:
      - One reader thread continuously recv()s from the WebSocket
      - Reader dispatches: 'id' in msg -> resolve pending[id] Future; 'method' -> event subscribers
      - call() is thread-safe via send_lock + per-request Future
      - subscribe() returns a Queue that the reader thread fills with events
    """
    def __init__(self, ws_url: str, timeout: int = TIMEOUT):
        self.ws_url = ws_url
        self.timeout = timeout
        self.send_lock = threading.Lock()
        self.next_id = _ws_counter  # global monotonic
        self.pending: Dict[int, concurrent.futures.Future] = {}
        self.pending_lock = threading.Lock()
        self.event_subscribers: Dict[str, list] = {}
        self.event_lock = threading.Lock()
        self._closed = threading.Event()
        self._enabled_domains: set = set()

        ws_kwargs: Dict[str, Any] = {"timeout": timeout}
        if TOKEN:
            ws_kwargs["header"] = [f"Authorization: Bearer {TOKEN}"]

        self.ws = websocket.create_connection(ws_url, **ws_kwargs)

        for domain in ["Page", "Network", "Runtime", "DOM"]:
            try:
                self._raw_send_and_wait(domain, f"{domain}.enable")
                self._enabled_domains.add(domain)
            except Exception:
                pass

        self.ws.settimeout(0.5)
        self.reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True,
            name=f"cdp-reader-{next(_ws_counter)}"
        )
        self.reader_thread.start()

    def call(self, method: str, params: dict = None, timeout: float = None) -> dict:
        if self._closed.is_set():
            raise RuntimeError(CDP_DISCONNECTED)
        timeout = timeout or self.timeout
        cid = next(self.next_id)
        fut = concurrent.futures.Future()
        with self.pending_lock:
            self.pending[cid] = fut
        message = json.dumps({"id": cid, "method": method, "params": params or {}})
        try:
            with self.send_lock:
                self.ws.send(message)
        except Exception as e:
            with self.pending_lock:
                self.pending.pop(cid, None)
            raise RuntimeError(f"CDP send failed [{method}]: {e}")
        try:
            msg = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            with self.pending_lock:
                self.pending.pop(cid, None)
            raise TimeoutError(f"CDP command '{method}' timed out after {timeout}s")
        if isinstance(msg, dict) and "error" in msg:
            raise RuntimeError(f"CDP error [{method}]: {msg['error'].get('message', str(msg['error']))}")
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
    def is_alive(self) -> bool:
        return not self._closed.is_set()

    def close(self):
        self._closed.set()
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
            except (websocket.WebSocketTimeoutException, TimeoutError):
                raise TimeoutError(f"CDP bootstrap command '{method}' timed out")
            msg = json.loads(raw)
            mid = msg.get("id")
            evt_method = msg.get("method")
            if mid == cid:
                self.ws.settimeout(0.5)
                if "error" in msg:
                    raise RuntimeError(f"CDP error [{method}]: {msg['error'].get('message', str(msg['error']))}")
                return msg.get("result", {})
            elif evt_method is not None:
                # Dispatch event during bootstrap — don't let events block the response
                with self.event_lock:
                    subs = list(self.event_subscribers.get(evt_method, []))
                for q in subs:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass
                continue
            # Unknown message — skip and continue waiting
        raise TimeoutError(f"CDP bootstrap command '{method}' timed out after {timeout}s")

    def _reader_loop(self):
        backoff = RECONNECT_BASE_DELAY
        while not self._closed.is_set():
            try:
                raw = self.ws.recv()
                if raw is None:
                    raise ConnectionError("WebSocket closed by remote")
                msg = json.loads(raw)
            except (websocket.WebSocketTimeoutException, TimeoutError):
                continue
            except (websocket.WebSocketConnectionClosedException, ConnectionError, OSError) as e:
                self._closed.set()  # P0: mark closed so is_alive returns False
                _log(f"CDP reader disconnected: {e}", level="warn")
                self._reject_all_pending(CDP_DISCONNECTED)
                # Attempt reconnect
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_DELAY)
                try:
                    ws_kwargs: Dict[str, Any] = {"timeout": self.timeout}
                    if TOKEN:
                        ws_kwargs["header"] = [f"Authorization: Bearer {TOKEN}"]
                    self.ws = websocket.create_connection(self.ws_url, **ws_kwargs)
                    self.ws.settimeout(0.5)
                    self._closed.clear()  # reconnect successful
                    backoff = RECONNECT_BASE_DELAY
                    METRICS.incr("cdp_reconnects")
                    for domain in list(self._enabled_domains):
                        try:
                            self._raw_send_and_wait(domain, f"{domain}.enable")
                        except Exception:
                            self._enabled_domains.discard(domain)
                except Exception:
                    pass
                    continue
                break
            except Exception:
                self._closed.set()  # P0: mark closed on ANY fatal error
                _log(f"CDP reader fatal error", level="error")
                self._reject_all_pending(CDP_DISCONNECTED)
                break

            backoff = RECONNECT_BASE_DELAY
            mid = msg.get("id")
            method = msg.get("method")
            if mid is not None:
                with self.pending_lock:
                    fut = self.pending.pop(mid, None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif method is not None:
                with self.event_lock:
                    subs = list(self.event_subscribers.get(method, []))
                for q in subs:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass
        self._reject_all_pending(CDP_DISCONNECTED)

    def _reject_all_pending(self, reason: str):
        with self.pending_lock:
            pending = dict(self.pending)
            self.pending.clear()
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))


class CDPConnectionManager:
    """Manages per-tab CDP connections with thread-safe lazy creation."""
    _connections: Dict[str, CDPConnection] = {}
    _lock = threading.Lock()
    _heartbeat_thread: Optional[threading.Thread] = None
    _heartbeat_running = False

    @classmethod
    def get_connection(cls, tab: Dict[str, Any], timeout: int = TIMEOUT) -> CDPConnection:
        tab_id = tab.get("id", "")
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Tab has no webSocketDebuggerUrl")
        
        # Phase 1: Check existing under lock
        with cls._lock:
            conn = cls._connections.get(tab_id)
            if conn and conn.is_alive:
                return conn
            if conn:
                del cls._connections[tab_id]
        # Close stale connection outside lock (P1: WebSocket close can block)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        
        # Phase 2: Create and verify — NO lock held
        delay = RECONNECT_BASE_DELAY
        last_error = None
        candidate = None
        for attempt in range(3):
            try:
                candidate = CDPConnection(ws_url, timeout)
                candidate.call("Browser.getVersion", timeout=5)
                # Phase 3: Store under lock — check for race
                with cls._lock:
                    existing = cls._connections.get(tab_id)
                    if existing and existing.is_alive:
                        candidate.close()  # another thread beat us
                        return existing
                    cls._connections[tab_id] = candidate
                cls._start_heartbeats()
                METRICS.incr("cdp_connections_established")
                METRICS.gauge("cdp_active_connections", len(cls._connections))
                return candidate
            except Exception as e:
                last_error = e
                try:
                    if candidate is not None:
                        candidate.close()
                except Exception:
                    pass
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)
        raise RuntimeError(f"Failed to connect to CDP after 3 attempts: {last_error}")

    @classmethod
    def _start_heartbeats(cls):
        if cls._heartbeat_running:
            return
        cls._heartbeat_running = True
        cls._heartbeat_thread = threading.Thread(
            target=cls._heartbeat_loop, daemon=True, name="cdp-heartbeat"
        )
        cls._heartbeat_thread.start()

    @classmethod
    def _heartbeat_loop(cls):
        while cls._heartbeat_running:
            time.sleep(HEARTBEAT_INTERVAL)
            # P0: Copy under lock, do network I/O OUTSIDE lock
            with cls._lock:
                connections = list(cls._connections.items())
            dead = []
            for tab_id, conn in connections:
                try:
                    conn.call("Browser.getVersion", timeout=min(5, HEARTBEAT_INTERVAL * 0.8))
                except Exception:
                    dead.append((tab_id, conn))
            for tab_id, conn in dead:
                METRICS.incr("cdp_heartbeat_failures")
                try:
                    conn.close()  # P0: close WebSocket + reader thread
                except Exception:
                    pass
                with cls._lock:
                    cls._connections.pop(tab_id, None)
            METRICS.gauge("cdp_active_connections", len(cls._connections))

    @classmethod
    def close_all(cls):
        cls._heartbeat_running = False
        with cls._lock:
            for conn in cls._connections.values():
                try:
                    conn.close()
                except Exception:
                    pass
            cls._connections.clear()
        METRICS.gauge("cdp_active_connections", 0)


def _ws_cmd(tab: Dict[str, Any], commands: List[Dict[str, Any]], timeout: int = TIMEOUT) -> Dict[str, Any]:
    """Execute CDP commands through the ConnectionManager with proper multiplexing."""
    if not CDP_CIRCUIT.allow():
        raise RuntimeError("CDP circuit breaker OPEN")
    start = time.time()
    try:
        conn = CDPConnectionManager.get_connection(tab, timeout)
    except Exception:
        METRICS.incr("cdp_connection_errors")
        CDP_CIRCUIT.failure()
        raise

    results: Dict[str, Any] = {}
    errors: List[str] = []
    try:
        for original in commands:
            cmd = dict(original)
            method = cmd["method"]
            params = cmd.get("params", {})
            key = cmd.get("_key", method)
            remaining = max(2, timeout - (time.time() - start))
            cmd_timeout = min(TIMEOUT, remaining)
            try:
                result = conn.call(method, params, timeout=cmd_timeout)
                results[key] = result
            except Exception as e:
                errors.append(f"{key}: {e}")
            wait = cmd.get("wait_after_send")
            if wait:
                time.sleep(float(wait) / 1000.0 if float(wait) >= 100 else float(wait))
        if errors:
            raise RuntimeError("; ".join(errors))
        CDP_CIRCUIT.success()
        METRICS.incr("cdp_commands_success")
        METRICS.observe("cdp_command_latency", time.time() - start)
        return results
    except Exception:
        METRICS.incr("cdp_commands_failed")
        CDP_CIRCUIT.failure()
        raise


def _safe_ws(tab: Dict[str, Any], commands: List[Dict[str, Any]], timeout: int = TIMEOUT, retries: int = 2) -> Dict[str, Any]:
    """Wrapper with retry after timeout/websocket failure. Skips retry for non-idempotent ops."""
    # P0: Non-idempotent methods — never retry (click/type/submit/navigate)
    non_idempotent = any(
        cmd.get("method") in ("Page.navigate", "Page.reload", "Runtime.evaluate",
                              "Input.dispatchMouseEvent", "Input.dispatchKeyEvent",
                              "DOM.setFileInputFiles")
        for cmd in commands
    )
    if non_idempotent:
        retries = 0
    
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return _ws_cmd(tab, commands, timeout)
        except Exception as e:
            last = e
            retryable = any(s in str(e).lower() for s in ["timeout", "websocket", "closed", "circuit", CDP_DISCONNECTED.lower()])
            if attempt >= retries or not retryable:
                break
            try:
                tabs = _http("GET", "/json") or []
                tab = next((t for t in tabs if t.get("id") == tab.get("id")), tab)
            except Exception:
                pass
            time.sleep(min(0.25 * (2 ** attempt), 2.0))
    raise last or RuntimeError("unknown websocket error")
def _eval(tab: Dict[str, Any], expression: str, *, await_promise: bool = False, 
          return_by_value: bool = True, timeout: int = TIMEOUT) -> Dict[str, Any]:
    r = _safe_ws(tab, [{
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": await_promise,
        },
        "_key": "eval"
    }], timeout=timeout)
    result = r.get("eval", {})
    if result.get("exceptionDetails"):
        text = result["exceptionDetails"].get("text", "JavaScript exception")
        raise RuntimeError(text)
    return result.get("result", {})


def _wait_ready(tab: Dict[str, Any], timeout_sec: float = 15.0) -> None:
    end = time.time() + timeout_sec
    while time.time() < end:
        try:
            res = _eval(tab, "document.readyState", timeout=5)
            if res.get("value") in ("interactive", "complete"):
                return
        except Exception:
            pass
        time.sleep(0.25)


# ====== SELF-HEALING LOCATORS (P2) ======

class SelectorPack:
    """
    Progressive selector resolution with fallback strategies.
    Accepts either a string (backward compat) or a dict:
    {primary, text, aria, xpath_fallback, label_text}
    """
    
    @staticmethod
    def normalize(selector) -> Dict[str, Any]:
        """Normalize selector input to a selector_pack dict."""
        if isinstance(selector, str):
            return {"primary": selector}
        if isinstance(selector, dict):
            return selector
        return {"primary": str(selector)}

    @staticmethod
    def resolve(tab: Dict[str, Any], selector_pack: dict, 
                timeout: int = TIMEOUT) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Try strategies in order until one matches. Returns (strategy_used, element_info).
        Element info: {found, text, tag, selector} or None if nothing found.
        """
        strategies = []
        
        if selector_pack.get("primary"):
            strategies.append(("primary", selector_pack["primary"]))
        if selector_pack.get("text"):
            sel = selector_pack.get("text")
            strategies.append(("text", f"//*[contains(normalize-space(), {_json_arg(sel)})]"))
        if selector_pack.get("aria"):
            sel = selector_pack.get("aria")
            strategies.append(("aria", f"[aria-label*={_json_arg(sel)}]"))
        if selector_pack.get("label_text"):
            sel = selector_pack.get("label_text")
            strategies.append(("label_text", f"//label[contains(., {_json_arg(sel)})]/following-sibling::*[1]"))
        if selector_pack.get("xpath_fallback"):
            strategies.append(("xpath_fallback", selector_pack["xpath_fallback"]))
        
        for strategy_name, strategy_sel in strategies:
            try:
                # Test if selector matches
                if strategy_name in ("text", "label_text", "xpath_fallback") and strategy_sel.startswith("/"):
                    # XPath — evaluate differently
                    expr = f"""
                    (function() {{
                        try {{
                            const result = document.evaluate(
                                {_json_arg(strategy_sel)}, document, null, 
                                XPathResult.FIRST_ORDERED_NODE_TYPE, null
                            );
                            const el = result.singleNodeValue;
                            if (!el) return {{found: false}};
                            return {{
                                found: true,
                                tag: el.tagName,
                                text: (el.innerText || el.value || '').slice(0, 100),
                                rect: (() => {{ const r = el.getBoundingClientRect(); return {{x: r.x, y: r.y, w: r.width, h: r.height}}; }})()
                            }};
                        }} catch(e) {{ return {{found: false, error: e.message}}; }}
                    }})()
                    """
                else:
                    expr = f"""
                    (function() {{
                        const el = document.querySelector({_json_arg(strategy_sel)});
                        if (!el) return {{found: false}};
                        return {{
                            found: true,
                            tag: el.tagName,
                            text: (el.innerText || el.value || '').slice(0, 100),
                            rect: (() => {{ const r = el.getBoundingClientRect(); return {{x: r.x, y: r.y, w: r.width, h: r.height}}; }})()
                        }};
                    }})()
                    """
                result = _eval(tab, expr, timeout=10).get("value") or {}
                if result.get("found"):
                    return strategy_name, result
            except Exception:
                continue
        
        # Levenshtein fallback — search all visible interactive elements
        if selector_pack.get("text"):
            wanted = str(selector_pack["text"]).lower()
            expr = f"""
            (() => {{
                const wanted = {_json_arg(wanted)};
                const dist = (a, b) => {{
                    if (!a.length) return b.length;
                    if (!b.length) return a.length;
                    const m = [];
                    for (let i = 0; i <= a.length; i++) {{ m[i] = [i]; }}
                    for (let j = 0; j <= b.length; j++) {{ m[0][j] = j; }}
                    for (let i = 1; i <= a.length; i++) {{
                        for (let j = 1; j <= b.length; j++) {{
                            m[i][j] = Math.min(
                                m[i-1][j] + 1, m[i][j-1] + 1,
                                m[i-1][j-1] + (a[i-1] === b[j-1] ? 0 : 1)
                            );
                        }}
                    }}
                    return m[a.length][b.length];
                }};
                const els = Array.from(document.querySelectorAll('a,button,input,textarea,select,[role]'));
                const visible = e => {{
                    const s = getComputedStyle(e), r = e.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                }};
                const candidates = els.filter(visible).map(e => ({{
                    el: e,
                    text: (e.innerText || e.value || e.getAttribute('aria-label') || '').toLowerCase().slice(0, 100),
                }})).filter(c => c.text);
                let best = null, bestDist = Infinity;
                for (const c of candidates) {{
                    const d = dist(wanted, c.text);
                    if (d < bestDist) {{ bestDist = d; best = c; }}
                }}
                if (!best || bestDist > wanted.length * 0.6) return {{found: false, levenshtein_tried: true}};
                const r = best.el.getBoundingClientRect();
                return {{found: true, tag: best.el.tagName, text: best.text, levenshtein_dist: bestDist,
                         rect: {{x: r.x, y: r.y, w: r.width, h: r.height}} }};
            }})()
            """
            try:
                result = _eval(tab, expr, timeout=15).get("value") or {}
                if result.get("found"):
                    return "levenshtein", result
            except Exception:
                pass
        
        return "none", None

    @staticmethod
    def click_element(tab: Dict[str, Any], selector_pack: dict, timeout: int = 10) -> Dict[str, Any]:
        """Resolve selector and click the matched element."""
        strategy, info = SelectorPack.resolve(tab, selector_pack, timeout)
        if not info or not info.get("found"):
            return {"ok": False, "error": "element not found with any strategy", 
                    "strategies_tried": list(selector_pack.keys())}
        
        # Click using rect coordinates for reliability
        rect = info.get("rect", {})
        x = int(rect.get("x", 0) + rect.get("w", 0) / 2)
        y = int(rect.get("y", 0) + rect.get("h", 0) / 2)
        
        expr = f"""
        (function() {{
            const el = document.elementFromPoint({x}, {y});
            if (!el) return {{ok: false, error: 'no element at point'}};
            el.scrollIntoView({{block: 'center', inline: 'center'}});
            el.click();
            return {{ok: true, clicked: true, tag: el.tagName, 
                    text: (el.innerText || el.value || '').slice(0, 100)}};
        }})()
        """
        result = _eval(tab, expr, timeout=timeout).get("value") or {}
        return {"ok": bool(result.get("ok")), "tab_id": tab.get("id"), 
                "strategy": strategy, **result, **info}


# ====== NETWORK INTERCEPTION — HAR (P2) ======

def _capture_network_har(tab: Dict[str, Any], duration_ms: int, 
                         include_response_bodies: bool = False) -> Dict[str, Any]:
    """Full HAR-like capture using CDPConnection dispatcher with Bearer auth."""
    duration_ms = max(250, min(int(duration_ms), 30000))
    entries: Dict[str, Dict[str, Any]] = {}
    
    conn = CDPConnectionManager.get_connection(tab, timeout=max(TIMEOUT, int(duration_ms / 1000) + 10))
    
    # Enable Network domain
    conn.call("Network.enable", {"maxTotalBufferSize": 10485760, "maxResourceBufferSize": 2097152}, timeout=5)
    
    # Subscribe to Network events
    q_req = conn.subscribe("Network.requestWillBeSent")
    q_resp = conn.subscribe("Network.responseReceived")
    q_fin = conn.subscribe("Network.loadingFinished")
    q_fail = conn.subscribe("Network.loadingFailed")
    
    end = time.time() + duration_ms / 1000.0
    
    try:
        while time.time() < end:
            remaining = max(0.05, min(0.5, end - time.time()))
            try:
                msg = q_req.get(timeout=remaining)
            except queue.Empty:
                try:
                    msg = q_resp.get(timeout=0.01)
                except queue.Empty:
                    try:
                        msg = q_fin.get(timeout=0.01)
                    except queue.Empty:
                        try:
                            msg = q_fail.get(timeout=0.01)
                        except queue.Empty:
                            continue
            
            method = msg.get("method")
            params = msg.get("params", {})
            rid = params.get("requestId")
            if not rid:
                continue
            
            entry = entries.setdefault(rid, {
                "request_id": rid,
                "startedDateTime": None,
                "request": {},
                "response": {},
                "timings": {},
            })
            
            if method == "Network.requestWillBeSent":
                req = params.get("request", {})
                entry["startedDateTime"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(params.get("timestamp", time.time())))
                # P0: Redact postData — block sensitive URL patterns
                post_url = req.get("url", "")
                post_data = req.get("postData", "")
                if post_data:
                    url_lower = post_url.lower()
                    sensitive = any(kw in url_lower for kw in 
                                   ("login", "oauth", "token", "payment", "checkout", "signin", "auth"))
                    if sensitive:
                        post_data = "[redacted — sensitive endpoint]"
                    else:
                        post_data = post_data[:500]
                entry["request"] = {
                    "url": post_url,
                    "method": req.get("method", "GET"),
                    "headers": {k: v for k, v in (req.get("headers", {}) or {}).items()
                               if k.lower() not in {"cookie", "authorization", "set-cookie"}},
                    "postData": post_data,
                }
                entry["resourceType"] = params.get("type", "")
                entry["initiator"] = (params.get("initiator") or {}).get("type", "")
            
            elif method == "Network.responseReceived":
                resp = params.get("response", {})
                entry["response"] = {
                    "status": resp.get("status"),
                    "statusText": resp.get("statusText", ""),
                    "mimeType": resp.get("mimeType", ""),
                    "headers": {k: v for k, v in (resp.get("headers", {}) or {}).items()
                               if k.lower() not in {"set-cookie"}},
                    "fromCache": bool(resp.get("fromDiskCache") or resp.get("fromPrefetchCache")),
                }
                entry["timings"]["receive"] = params.get("timestamp", time.time())
            
            elif method == "Network.loadingFinished":
                entry["timings"]["finished"] = params.get("timestamp", time.time())
                entry["encodedDataLength"] = params.get("encodedDataLength", 0)
                
                if include_response_bodies:
                    try:
                        body_result = conn.call("Network.getResponseBody", 
                                               {"requestId": rid}, timeout=3)
                        body = body_result.get("body", "") or ""
                        if body_result.get("base64Encoded"):
                            import base64 as _b64
                            body = _b64.b64decode(body).decode("utf-8", errors="replace")
                        entry["response"]["body"] = body[:2000]
                    except Exception:
                        entry["response"]["body"] = "[body fetch failed]"
            
            elif method == "Network.loadingFailed":
                entry["response"]["error"] = params.get("errorText", "")
                entry["response"]["status"] = 0
    
    finally:
        conn.unsubscribe("Network.requestWillBeSent", q_req)
        conn.unsubscribe("Network.responseReceived", q_resp)
        conn.unsubscribe("Network.loadingFinished", q_fin)
        conn.unsubscribe("Network.loadingFailed", q_fail)
    
    rows = list(entries.values())
    rows.sort(key=lambda x: x.get("startedDateTime") or "")
    
    return {
        "ok": True,
        "tab_id": tab.get("id"),
        "duration_ms": duration_ms,
        "total_requests": len(rows),
        "entries": rows,
    }

# ====== MEMORY-WIKI INTEGRATION (P1) ======

def _memory_wiki_post(endpoint: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST to memory-wiki API, returns JSON or None on failure."""
    try:
        url = f"{MEMORY_WIKI_URL}/{endpoint.lstrip('/')}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=MEMORY_WIKI_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception as e:
        _log(f"memory-wiki POST {endpoint} failed: {e}", level="warn")
        return None


def _persist_to_wiki(tab_id: str, page_data: Dict[str, Any]) -> Optional[str]:
    """Persist page data to memory-wiki. Returns capsule_id or None."""
    return None  # Stub — requires memory-wiki tool availability
    # Real implementation would call memory_wiki_add via MCP or direct API
    # try:
    #     result = _memory_wiki_post("api/claims", {
    #         "claim": f"Browser page: {page_data.get('url', '')}",
    #         "evidence": json.dumps(page_data, ensure_ascii=False),
    #         "type": "procedural",
    #         "confidence": 0.85,
    #         "source": "browser-automation-mcp",
    #     })
    #     return result.get("id") if result else None
    # except Exception as e:
    #     _log(f"persist_to_wiki error: {e}", level="error")
    #     return None


def _recall_from_wiki(url_pattern: str) -> Optional[Dict[str, Any]]:
    """Recall cached page data from memory-wiki."""
    return None  # Stub — requires memory-wiki search
    # try:
    #     result = _memory_wiki_post("api/search", {"query": url_pattern, "limit": 3})
    #     if result and result.get("results"):
    #         return {"found": True, "results": result["results"]}
    #     return {"found": False}
    # except Exception as e:
    #     return {"found": False, "error": str(e)}


# ====== UTILITY HELPERS (unchanged from v1.4) ======

def _redact_cookie(c: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(c)
    if "value" in out:
        out["value"] = f"[redacted len={len(str(out['value']))}]"
    return out


SENSITIVE_KEY_RE = re.compile(
    r"(cookie|session|token|secret|passwd|password|auth|bearer|jwt|csrf|xsrf|api[-_]?key|access[-_]?key)", re.I)


def _is_sensitive_key(key: Any) -> bool:
    return bool(SENSITIVE_KEY_RE.search(str(key or "")))


def _redact_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    return f"[redacted len={len(text)}]"


def _redact_mapping(data: Any) -> Any:
    if isinstance(data, dict):
        redacted: Dict[str, Any] = {}
        for key, value in data.items():
            if _is_sensitive_key(key):
                redacted[str(key)] = _redact_value(value)
            elif isinstance(value, (dict, list)):
                redacted[str(key)] = _redact_mapping(value)
            else:
                redacted[str(key)] = value
        return redacted
    if isinstance(data, list):
        return [_redact_mapping(item) for item in data]
    return data


def _safe_filename(prefix: str, ext: str) -> str:
    clean_prefix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", prefix or "browser")[:80].strip(".-") or "browser"
    clean_ext = re.sub(r"[^a-zA-Z0-9]+", "", ext or "bin")[:16] or "bin"
    # P0: Random suffix prevents collision + O_CREAT|O_EXCL safety
    return f"{clean_prefix}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(8)}.{clean_ext}"


def _artifact_path(prefix: str, ext: str) -> str:
    # P0: Secure directory permissions
    os.makedirs(ARTIFACT_DIR, mode=0o700, exist_ok=True)
    return os.path.join(ARTIFACT_DIR, _safe_filename(prefix, ext))


def _write_b64_artifact(prefix: str, ext: str, data_b64: str) -> Dict[str, Any]:
    raw = base64.b64decode(data_b64.encode("ascii"), validate=False) if data_b64 else b""
    path = _artifact_path(prefix, ext)
    # P0: O_CREAT|O_EXCL — fail if file exists, 0600 permissions, check not symlink
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        # Verify not a symlink before writing
        if os.path.islink(path):
            os.close(fd)
            os.unlink(path)
            raise BrowserError("SECURITY", "Artifact path is a symlink")
        os.write(fd, raw)
    finally:
        os.close(fd)
    return {"path": path, "bytes": len(raw), "media_hint": _media_hint(ext)}


def _write_text_artifact(prefix: str, ext: str, text: str) -> Dict[str, Any]:
    data = (text or "").encode("utf-8")
    path = _artifact_path(prefix, ext)
    # P0: Same secure pattern
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        if os.path.islink(path):
            os.close(fd)
            os.unlink(path)
            raise BrowserError("SECURITY", "Artifact path is a symlink")
        os.write(fd, data)
    finally:
        os.close(fd)
    return {"path": path, "bytes": len(data), "media_hint": _media_hint(ext)}


def _public_tab(t: Dict[str, Any], include_debug_url: bool = False) -> Dict[str, Any]:
    out = {"id": t.get("id"), "url": t.get("url"), "title": t.get("title"), "type": t.get("type")}
    if include_debug_url:
        out["webSocketDebuggerUrl"] = t.get("webSocketDebuggerUrl")
    return out


def _media_hint(ext: str) -> str:
    return {"png": "image/png", "pdf": "application/pdf", "html": "text/html", 
            "json": "application/json"}.get(ext, "application/octet-stream")


# ====== TOOLS ======

TAB_PROPS = {
    "tab_id": {"type": "string", "description": "Optional target tab id from browser_tabs"},
    "url_filter": {"type": "string", "description": "Optional substring to choose a tab by URL"},
}

# Selector pack schema for self-healing locators (P2)
SELECTOR_PACK_SCHEMA = {
    "type": "object",
    "description": "Self-healing selector pack: {primary: css, text: str, aria: str, xpath_fallback: str, label_text: str}",
    "properties": {
        "primary": {"type": "string", "description": "Primary CSS selector"},
        "text": {"type": "string", "description": "Text content to match (fuzzy)"},
        "aria": {"type": "string", "description": "aria-label substring"},
        "xpath_fallback": {"type": "string", "description": "Fallback XPath expression"},
        "label_text": {"type": "string", "description": "Label text for following-sibling lookup"},
    },
}

TOOLS = [
    # === v1.4 tools (unchanged) ===
    {"name": "browser_navigate", "description": "Перейти на URL. Возвращает title/url/tab_id. Опционально persist_to_wiki=true для сохранения в память.", 
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string"}, "tab_id": TAB_PROPS["tab_id"], 
         "wait_until_ready": {"type": "boolean", "default": True},
         "persist_to_wiki": {"type": "boolean", "default": False, "description": "Сохранить страницу в memory-wiki (P1)"},
     }, "required": ["url"]}},
    
    {"name": "browser_screenshot", "description": "Сделать скриншот. Возвращает base64 PNG. Для файлов: browser_screenshot_file.",
     "inputSchema": {"type": "object", "properties": {"full_page": {"type": "boolean", "default": False}, **TAB_PROPS}}},
    
    {"name": "browser_screenshot_file", "description": "Сделать PNG-скриншот в файл.",
     "inputSchema": {"type": "object", "properties": {
         "full_page": {"type": "boolean", "default": False}, 
         "filename_prefix": {"type": "string", "default": "browser-screenshot"},
         "persist_to_wiki": {"type": "boolean", "default": False}, **TAB_PROPS}}},
    
    {"name": "browser_cookies", "description": "Cookies, document.cookie и storage. Секреты редактируются.",
     "inputSchema": {"type": "object", "properties": {
         "url_filter": TAB_PROPS["url_filter"], "tab_id": TAB_PROPS["tab_id"],
         "redact": {"type": "boolean", "default": True}}}},
    # === v1.5 Cookie API ===
    {"name": "browser_cookie_list", "description": "Список cookies (current_page или browser_context). Значения скрыты по умолчанию.",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string", "enum": ["current_page", "browser_context"], "default": "current_page"},
         "browserContextId": {"type": "string", "description": "Browser context ID for incognito/isolated contexts"},
         "domain_filter": {"type": "string", "description": "Фильтр по домену"},
         "include_values": {"type": "boolean", "default": False},
         "confirm_sensitive_data": {"type": "boolean", "default": False},
         "tab_id": TAB_PROPS["tab_id"], "url_filter": TAB_PROPS["url_filter"]}}},
    
    {"name": "browser_cookie_set", "description": "Установить cookie. Используй url для host-only cookie, или domain для domain cookie.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "value": {"type": "string"},
         "url": {"type": "string", "description": "URL для host-only cookie (альтернатива domain)"},
         "domain": {"type": "string", "description": "Домен для domain cookie (альтернатива url)"},
         "path": {"type": "string", "default": "/"},
         "httpOnly": {"type": "boolean", "default": False}, "secure": {"type": "boolean", "default": True},
         "sameSite": {"type": "string", "enum": ["Strict", "Lax", "None"], "default": "Lax"},
         "expires": {"type": "number", "description": "Unix timestamp"},
         "priority": {"type": "string", "enum": ["Low", "Medium", "High"], "default": "Medium"},
         "sourceScheme": {"type": "string", "enum": ["Unset", "NonSecure", "Secure"], "default": "Secure"},
         "sourcePort": {"type": "integer", "description": "Source port (обычно 443 для HTTPS)"},
         "partitionKey": {"type": "object", "properties": {
             "topLevelSite": {"type": "string"}, "hasCrossSiteAncestor": {"type": "boolean", "default": False}
         }, "description": "Partition key для CHIPS/partitioned cookies"},
         "browserContextId": {"type": "string", "description": "Browser context ID для incognito"},
         "tab_id": TAB_PROPS["tab_id"]},
     "required": ["name", "value"],
     "oneOf": [{"required": ["url"]}, {"required": ["domain"]}]}},
    
    {"name": "browser_cookie_delete", "description": "Удалить cookies. Укажи url или domain для точного удаления.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "url": {"type": "string"},
         "domain": {"type": "string"}, "path": {"type": "string", "default": "/"},
         "tab_id": TAB_PROPS["tab_id"]},
     "required": ["name"],
     "oneOf": [{"required": ["url"]}, {"required": ["domain"]}]}},
    
    {"name": "browser_cookie_clear", "description": "Очистить все cookies. Требует подтверждения для browser_context (⚠️ разлогинит все сайты).",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string", "enum": ["current_page", "browser_context"], "default": "current_page"},
         "confirm_destructive": {"type": "boolean", "default": False, "description": "Обязательно true для browser_context"},
         "browserContextId": {"type": "string", "description": "Browser context ID for incognito/isolated contexts"},
         "tab_id": TAB_PROPS["tab_id"]}}},


    {"name": "browser_localstorage", "description": "localStorage сайта.",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string"}, "redact": {"type": "boolean", "default": True}, **TAB_PROPS}}},

    {"name": "browser_sessionstorage", "description": "sessionStorage сайта.",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string"}, "redact": {"type": "boolean", "default": True}, **TAB_PROPS}}},

    {"name": "browser_exec", "description": "Выполнить JavaScript на странице.",
     "inputSchema": {"type": "object", "properties": {
         "expression": {"type": "string"}, "await_promise": {"type": "boolean", "default": False},
         **TAB_PROPS}, "required": ["expression"]}},

    {"name": "browser_click", "description": "Кликнуть по CSS-селектору. Поддерживает selector_pack (P2).",
     "inputSchema": {"type": "object", "properties": {
         "selector": {"type": "string", "description": "CSS selector (или используй selector_pack)"},
         "selector_pack": SELECTOR_PACK_SCHEMA, **TAB_PROPS}}},

    {"name": "browser_type", "description": "Ввести текст в поле.",
     "inputSchema": {"type": "object", "properties": {
         "selector": {"type": "string"}, "selector_pack": SELECTOR_PACK_SCHEMA,
         "text": {"type": "string"}, "clear_first": {"type": "boolean", "default": True},
         **TAB_PROPS}, "required": ["text"]}},

    {"name": "browser_gettext", "description": "Получить innerText. Поддерживает selector_pack и persist_to_wiki.",
     "inputSchema": {"type": "object", "properties": {
         "selector": {"type": "string"}, "selector_pack": SELECTOR_PACK_SCHEMA,
         "all": {"type": "boolean", "default": False},
         "persist_to_wiki": {"type": "boolean", "default": False}, **TAB_PROPS}}},

    {"name": "browser_gethtml", "description": "Получить HTML элемента.",
     "inputSchema": {"type": "object", "properties": {
         "selector": {"type": "string"}, "selector_pack": SELECTOR_PACK_SCHEMA,
         "outer": {"type": "boolean", "default": False}, **TAB_PROPS}}},

    {"name": "browser_getvalue", "description": "Получить value поля.",
     "inputSchema": {"type": "object", "properties": {
         "selector": {"type": "string"}, "selector_pack": SELECTOR_PACK_SCHEMA, **TAB_PROPS}}},

    {"name": "browser_wait", "description": "Дождаться элемента. Поддерживает selector_pack.",
     "inputSchema": {"type": "object", "properties": {
         "selector": {"type": "string"}, "selector_pack": SELECTOR_PACK_SCHEMA,
         "timeout_ms": {"type": "integer", "default": 10000}, **TAB_PROPS}}},

    {"name": "browser_fill_form", "description": "Заполнить несколько полей разом.",
     "inputSchema": {"type": "object", "properties": {
         "fields": {"type": "object"}, "submit_selector": {"type": "string"}, **TAB_PROPS},
         "required": ["fields"]}},

    {"name": "browser_login", "description": "Авторизоваться и вернуть cookies (безопасный — пароль не в аргументах).",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string"}, "username_selector": {"type": "string"},
         "password_selector": {"type": "string"}, "submit_selector": {"type": "string"},
         "credential_ref": {"type": "string", "description": "Ссылка на креденшелы: vault://имя или env:VAR_NAME"},
         "username": {"type": "string", "description": "DEPRECATED: используй credential_ref"},
         "password": {"type": "string", "description": "DEPRECATED: используй credential_ref"},
         "extra_fields": {"type": "object"}, "redact": {"type": "boolean", "default": True},
         "tab_id": TAB_PROPS["tab_id"]},
         "required": ["url", "username_selector", "password_selector", "submit_selector"]}},

    {"name": "browser_tabs", "description": "Список вкладок.",
     "inputSchema": {"type": "object", "properties": {
         "include_non_page": {"type": "boolean", "default": False},
         "include_debug_url": {"type": "boolean", "default": False}}}},

    {"name": "browser_newtab", "description": "Открыть новую вкладку.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string"}, "wait_until_ready": {"type": "boolean", "default": True},
         "include_debug_url": {"type": "boolean", "default": False}}, "required": ["url"]}},

    {"name": "browser_closetab", "description": "Закрыть вкладку.",
     "inputSchema": {"type": "object", "properties": {
         "tab_id": {"type": "string"}, "url_filter": {"type": "string"}}}},

    {"name": "browser_scroll", "description": "Прокрутить страницу.",
     "inputSchema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
         "selector": {"type": "string"}, "amount": {"type": "integer", "default": 500}, **TAB_PROPS}}},

    {"name": "browser_pdf", "description": "Сохранить страницу как PDF base64.",
     "inputSchema": {"type": "object", "properties": {**TAB_PROPS}}},

    {"name": "browser_pdf_file", "description": "Сохранить страницу как PDF-файл.",
     "inputSchema": {"type": "object", "properties": {
         "filename_prefix": {"type": "string", "default": "browser-page"}, **TAB_PROPS}}},

    {"name": "browser_html_file", "description": "Сохранить DOM HTML файлом.",
     "inputSchema": {"type": "object", "properties": {
         "filename_prefix": {"type": "string", "default": "browser-page"}, **TAB_PROPS}}},

    {"name": "browser_health", "description": "Проверить CDP/браузер/версию/метрики (P0 enhanced).",
     "inputSchema": {"type": "object", "properties": {
         "autostart": {"type": "boolean", "default": True},
         "include_metrics": {"type": "boolean", "default": True}}}},

    {"name": "browser_page_summary", "description": "Структурная сводка: title/url/forms/links/buttons.",
     "inputSchema": {"type": "object", "properties": {
         "max_text": {"type": "integer", "default": 4000}, "max_items": {"type": "integer", "default": 30},
         **TAB_PROPS}}},

    {"name": "browser_snapshot", "description": "Снимок страницы: summary + elements + скриншот.",
     "inputSchema": {"type": "object", "properties": {
         "max_text": {"type": "integer", "default": 4000}, "max_items": {"type": "integer", "default": 40},
         "include_screenshot": {"type": "boolean", "default": True}, **TAB_PROPS}}},

    {"name": "browser_network_log", "description": "Лёгкий CDP Network capture (XHR/fetch/API).",
     "inputSchema": {"type": "object", "properties": {
         "duration_ms": {"type": "integer", "default": 3000}, "reload": {"type": "boolean", "default": False},
         "include_all": {"type": "boolean", "default": False}, "max_items": {"type": "integer", "default": 100},
         **TAB_PROPS}}},

    {"name": "browser_find_api_calls", "description": "Найти JSON/API вызовы страницы.",
     "inputSchema": {"type": "object", "properties": {
         "duration_ms": {"type": "integer", "default": 3000}, "reload": {"type": "boolean", "default": True},
         "max_items": {"type": "integer", "default": 60}, **TAB_PROPS}}},

    {"name": "browser_select", "description": "Выбрать option в select.",
     "inputSchema": {"type": "object", "properties": {
         "selector": {"type": "string"}, "value": {"type": "string"}, "label": {"type": "string"},
         "index": {"type": "integer"}, **TAB_PROPS}, "required": ["selector"]}},

    {"name": "browser_elements", "description": "Найти видимые элементы с CSS-селекторами.",
     "inputSchema": {"type": "object", "properties": {
         "selector": {"type": "string"}, "query": {"type": "string"},
         "kind": {"type": "string", "enum": ["all", "clickable", "input", "link", "button", "select"]},
         "max_items": {"type": "integer", "default": 80}, "include_hidden": {"type": "boolean", "default": False},
         **TAB_PROPS}}},

    {"name": "browser_click_text", "description": "Кликнуть по тексту без CSS-селектора.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "selector": {"type": "string"},
         "exact": {"type": "boolean", "default": False}, "case_sensitive": {"type": "boolean", "default": False},
         "index": {"type": "integer", "default": 0}, "wait_after_ms": {"type": "integer", "default": 300},
         **TAB_PROPS}, "required": ["text"]}},

    {"name": "browser_wait_text", "description": "Дождаться текста в DOM.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "selector": {"type": "string", "default": "body"},
         "exact": {"type": "boolean", "default": False}, "case_sensitive": {"type": "boolean", "default": False},
         "timeout_ms": {"type": "integer", "default": 10000}, **TAB_PROPS}, "required": ["text"]}},

    {"name": "browser_batch", "description": "Выполнить несколько действий последовательно.",
     "inputSchema": {"type": "object", "properties": {
         "steps": {"type": "array", "items": {"type": "object", "properties": {
             "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["tool"]}},
         "stop_on_error": {"type": "boolean", "default": True}}, "required": ["steps"]}},

    # === NEW v2.0 TOOLS ===

    # P2: Network HAR
    {"name": "browser_network_har", "description": "Полный HAR-подобный захват сетевых запросов через CDP Network (P2).",
     "inputSchema": {"type": "object", "properties": {
         "duration_ms": {"type": "integer", "default": 5000, "description": "Время захвата в мс (250-30000)"},
         "include_bodies": {"type": "boolean", "default": False, "description": "Включить тела ответов (первые 2000 символов)"},
         **TAB_PROPS}}},

    # P1: Memory-Wiki recall
    {"name": "browser_recall", "description": "Найти ранее сохранённую страницу в memory-wiki по URL-паттерну (P1).",
     "inputSchema": {"type": "object", "properties": {
         "url_pattern": {"type": "string", "description": "URL или фрагмент для поиска"}},
         "required": ["url_pattern"]}},

    # P1: Session management
    {"name": "browser_session_tabs", "description": "Управление сессионными вкладками: list/switch/close_all (P1).",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "switch", "close_all", "close_others"],
                   "description": "list=показать все, switch=переключить active (требуется tab_id), close_all=закрыть все кроме active, close_others=закрыть остальные"},
         "tab_id": TAB_PROPS["tab_id"]}}},

    # P3: Deep web crawl bridge
    {"name": "browser_crawl_extract", "description": "Глубокое извлечение: навигация + извлечение ссылок + рекурсивный сбор (P3).",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "Стартовый URL"},
         "extract_rules": {"type": "object", "description": "Правила извлечения: {text_selector, link_selector, data_selectors: {name: css}}"},
         "max_depth": {"type": "integer", "default": 1, "description": "Глубина рекурсии (1-3)"},
         "max_pages": {"type": "integer", "default": 10, "description": "Максимум страниц"},
         "same_domain_only": {"type": "boolean", "default": True}},
     "required": ["url"]}},

    # P2: Self-healing click
    {"name": "browser_click_heal", "description": "Self-healing click: пробует несколько стратегий поиска элемента (P2).",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Текст элемента для поиска"},
         "selector_pack": SELECTOR_PACK_SCHEMA, **TAB_PROPS},
         "required": ["text"]}},

    # P0: Metrics
    {"name": "browser_metrics", "description": "Метрики сервера: счётчики, latency, circuit breaker (P0).",
     "inputSchema": {"type": "object", "properties": {}}},

    # v2.1: File upload for reverse image search etc.
    {"name": "browser_upload_file", "description": "Загрузить файл в input[type=file] на странице. Используется для reverse image search: сначала выбрать file input, затем вызвать этот tool.",
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string", "description": "Локальный путь к файлу для загрузки"},
         "selector": {"type": "string", "description": "CSS-селектор input[type=file] (по умолчанию: input[type=file])"},
         "tab_id": TAB_PROPS["tab_id"], "url_filter": TAB_PROPS["url_filter"]},
     "required": ["file_path"]}},
]

# ====== TOOL HANDLERS ======

def handle_navigate(args):
    url = args["url"]
    # P0: Navigation policy validation
    ok, reason = _validate_url(url)
    if not ok:
        return {"ok": False, "error": f"Navigation blocked: {reason}", "url": url}
    tab = _get_tab(args)
    r = _safe_ws(tab, [{"method": "Page.navigate", "params": {"url": url}, "_key": "nav"}], timeout=TIMEOUT)
    if args.get("wait_until_ready", True):
        time.sleep(0.5)
        tab = _get_tab({"tab_id": tab.get("id")}) or tab
        _wait_ready(tab, min(TIMEOUT, 20))
    title = _eval(tab, "document.title", timeout=10).get("value", "")
    final_url = _eval(tab, "window.location.href", timeout=10).get("value", url)
    result = {"ok": True, "tab_id": tab.get("id"), "url": final_url, "title": title, 
              "navigation": r.get("nav", {})}
    
    # P1: persist to memory-wiki
    if args.get("persist_to_wiki"):
        wiki_result = _persist_to_wiki(tab.get("id"), {
            "url": final_url, "title": title, "type": "navigation", "timestamp": time.time()
        })
        if wiki_result:
            result["wiki_capsule_id"] = wiki_result
    
    return result


def handle_screenshot(args):
    tab = _get_tab(args)
    full = bool(args.get("full_page", False))
    params: Dict[str, Any] = {"format": "png", "captureBeyondViewport": True}
    if full:
        try:
            metrics = _safe_ws(tab, [{"method": "Page.getLayoutMetrics", "_key": "m"}], timeout=10).get("m", {})
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            width, height = int(size.get("width", 1280)), int(size.get("height", 720))
            params["clip"] = {"x": 0, "y": 0, "width": max(width, 1), "height": max(height, 1), "scale": 1}
        except Exception:
            pass
    r = _safe_ws(tab, [{"method": "Page.captureScreenshot", "params": params, "_key": "shot"}], timeout=TIMEOUT)
    data = r.get("shot", {}).get("data", "")
    return {"ok": True, "tab_id": tab.get("id"), "screenshot_base64": data, 
            "bytes_estimate": len(data) * 3 // 4, "format": "png"}


def handle_screenshot_file(args):
    shot = handle_screenshot(args)
    saved = _write_b64_artifact(args.get("filename_prefix", "browser-screenshot"), "png", 
                                 shot.get("screenshot_base64", ""))
    result = {
        "ok": True, "tab_id": shot.get("tab_id"), "path": saved["path"], 
        "bytes": saved["bytes"], "media_hint": _media_hint("png"),
        "full_page": bool(args.get("full_page", False)),
    }
    if args.get("persist_to_wiki"):
        _persist_to_wiki(shot.get("tab_id"), {"type": "screenshot", "path": saved["path"], 
                                               "bytes": saved["bytes"]})
    return result


def handle_cookies(args):
    tab = _get_tab(args)
    filt = args.get("url_filter", "")
    redact = bool(args.get("redact", True))
    r = _safe_ws(tab, [
        {"method": "Network.getCookies", "_key": "cookies"},
        {"method": "Runtime.evaluate", "params": {"expression": "document.cookie", "returnByValue": True}, "_key": "doc_cookie"},
        {"method": "Runtime.evaluate", "params": {"expression": "window.location.href", "returnByValue": True}, "_key": "url"},
        {"method": "Runtime.evaluate", "params": {"expression": "document.title", "returnByValue": True}, "_key": "title"},
        {"method": "Runtime.evaluate", "params": {"expression": _storage_json_expr("localStorage"), "returnByValue": True}, "_key": "ls"},
        {"method": "Runtime.evaluate", "params": {"expression": _storage_json_expr("sessionStorage"), "returnByValue": True}, "_key": "ss"},
    ])
    cookies = r.get("cookies", {}).get("cookies", [])
    if filt:
        cookies = [c for c in cookies if filt.lower() in (c.get("domain", "") or "").lower()]
    doc_cookie = r.get("doc_cookie", {}).get("result", {}).get("value", "")
    local_storage = json.loads(r.get("ls", {}).get("result", {}).get("value", "{}") or "{}")
    session_storage = json.loads(r.get("ss", {}).get("result", {}).get("value", "{}") or "{}")
    if redact:
        cookies = [_redact_cookie(c) for c in cookies]
        doc_cookie = f"[redacted len={len(doc_cookie)}]"
        local_storage = _redact_mapping(local_storage)
        session_storage = _redact_mapping(session_storage)
    return {
        "ok": True, "tab_id": tab.get("id"), "cookies": cookies, "document_cookie": doc_cookie,
        "url": r.get("url", {}).get("result", {}).get("value", ""),
        "title": r.get("title", {}).get("result", {}).get("value", ""),
        "localStorage": local_storage, "sessionStorage": session_storage,
    }


def handle_localstorage(args):
    tab = _get_tab(args)
    val = _eval(tab, _storage_json_expr("localStorage", args.get("key", "")), timeout=10).get("value", "{}")
    data = json.loads(val or "{}")
    redacted = bool(args.get("redact", True))
    if redacted:
        data = _redact_mapping(data)
    return {"ok": True, "tab_id": tab.get("id"), "redacted": redacted, "data": data}


def handle_sessionstorage(args):
    tab = _get_tab(args)
    val = _eval(tab, _storage_json_expr("sessionStorage", args.get("key", "")), timeout=10).get("value", "{}")
    data = json.loads(val or "{}")
    redacted = bool(args.get("redact", True))
    if redacted:
        data = _redact_mapping(data)
    return {"ok": True, "tab_id": tab.get("id"), "redacted": redacted, "data": data}


def handle_exec(args):
    tab = _get_tab(args)
    expr = args["expression"]
    res = _eval(tab, expr, await_promise=bool(args.get("await_promise", False)), 
                return_by_value=True, timeout=TIMEOUT)
    return {"ok": True, "tab_id": tab.get("id"), "result": res.get("value"), 
            "type": res.get("type", "unknown"), "description": res.get("description")}


def _resolve_selector(args) -> str:
    """Resolve selector — supports both 'selector' string and 'selector_pack' dict (P2)."""
    if args.get("selector_pack"):
        strategy, info = SelectorPack.resolve(_get_tab(args), args["selector_pack"], timeout=10)
        if info and info.get("found"):
            return args["selector_pack"].get("primary", args["selector_pack"].get("text", ""))
        if args.get("selector"):
            return args["selector"]  # fallback to explicit
        raise RuntimeError(f"selector_pack resolution failed (tried all strategies)")
    return args.get("selector", "")


def handle_click(args):
    tab = _get_tab(args)
    
    # P2: try selector_pack first
    if args.get("selector_pack"):
        return SelectorPack.click_element(tab, args["selector_pack"], timeout=10)
    
    sel = args.get("selector", "")
    if not sel:
        return {"ok": False, "error": "selector or selector_pack required"}
    
    expr = f"""
    (function(sel) {{
        const el = document.querySelector(sel);
        if (!el) return {{ok:false, error:"element not found", selector: sel}};
        el.scrollIntoView({{block: "center", inline: "center"}});
        el.click();
        return {{ok:true, clicked:true, tag: el.tagName, text: (el.innerText || el.value || "").slice(0,100)}};
    }})({_json_arg(sel)})
    """
    result = _eval(tab, expr, timeout=10).get("value") or {}
    return {"ok": bool(result.get("ok")), "tab_id": tab.get("id"), **result}


def handle_type(args):
    tab = _get_tab(args)
    sel = _resolve_selector(args)
    if not sel and not args.get("selector_pack"):
        return {"ok": False, "error": "selector or selector_pack required"}
    
    text = args["text"]
    clear = bool(args.get("clear_first", True))
    
    if args.get("selector_pack") and not sel:
        # Fallback: use the primary from selector_pack
        sel = args["selector_pack"].get("primary", "")
        if not sel:
            return {"ok": False, "error": "selector_pack without primary — resolution not supported for type"}
    
    expr = f"""
    (function(sel, text, clearFirst) {{
        const el = document.querySelector(sel);
        if (!el) return {{ok:false, error:"element not found", selector: sel}};
        el.scrollIntoView({{block: "center", inline: "center"}});
        el.focus();
        const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, "value");
        const setValue = (v) => desc && desc.set ? desc.set.call(el, v) : (el.value = v);
        if (clearFirst) setValue("");
        setValue(text);
        el.dispatchEvent(new InputEvent("input", {{bubbles: true, inputType: "insertText", data: text}}));
        el.dispatchEvent(new Event("change", {{bubbles: true}}));
        return {{ok:true, typed:true, value: (el.value || "").slice(0,100)}};
    }})({_json_arg(sel)}, {_json_arg(text)}, {_json_arg(clear)})
    """
    result = _eval(tab, expr, timeout=10).get("value") or {}
    return {"ok": bool(result.get("ok")), "tab_id": tab.get("id"), **result}


def handle_gettext(args):
    tab = _get_tab(args)
    sel = _resolve_selector(args)
    if not sel:
        return {"ok": False, "error": "selector or selector_pack required"}
    
    all_el = bool(args.get("all", False))
    if all_el:
        expr = f"Array.from(document.querySelectorAll({_json_arg(sel)})).map(el => el.innerText || '')"
    else:
        expr = f"(document.querySelector({_json_arg(sel)}) || {{}}).innerText || ''"
    
    result = {"ok": True, "tab_id": tab.get("id"), 
              "text": _eval(tab, expr, timeout=10).get("value", [] if all_el else "")}
    
    if args.get("persist_to_wiki"):
        _persist_to_wiki(tab.get("id"), {"type": "gettext", "selector": sel, 
                                          "text_len": len(str(result.get("text", "")))})
    
    return result


def handle_gethtml(args):
    tab = _get_tab(args)
    sel = _resolve_selector(args)
    if not sel:
        return {"ok": False, "error": "selector or selector_pack required"}
    
    prop = "outerHTML" if args.get("outer") else "innerHTML"
    expr = f"(document.querySelector({_json_arg(sel)}) || {{}}).{prop} || ''"
    return {"ok": True, "tab_id": tab.get("id"), 
            "html": _eval(tab, expr, timeout=10).get("value", "")}


def handle_getvalue(args):
    tab = _get_tab(args)
    sel = _resolve_selector(args)
    if not sel:
        return {"ok": False, "error": "selector or selector_pack required"}
    
    expr = f"(document.querySelector({_json_arg(sel)}) || {{}}).value || ''"
    return {"ok": True, "tab_id": tab.get("id"), 
            "value": _eval(tab, expr, timeout=10).get("value", "")}


def handle_wait(args):
    tab = _get_tab(args)
    sel = _resolve_selector(args)
    if not sel:
        return {"ok": False, "error": "selector or selector_pack required"}
    
    timeout_ms = int(args.get("timeout_ms", 10000))
    expr = f"""
    new Promise((resolve) => {{
        const sel = {_json_arg(sel)};
        const deadline = Date.now() + {timeout_ms};
        const check = () => {{
            if (document.querySelector(sel)) return resolve(true);
            if (Date.now() >= deadline) return resolve(false);
            setTimeout(check, 200);
        }};
        check();
    }})
    """
    found = bool(_eval(tab, expr, await_promise=True, 
                       timeout=max(int(timeout_ms / 1000) + 5, 10)).get("value", False))
    return {"ok": True, "tab_id": tab.get("id"), "found": found, "selector": sel}


def handle_fill_form(args):
    tab = _get_tab(args)
    fields = args["fields"]
    submit = args.get("submit_selector", "")
    results = {}
    for sel, val in fields.items():
        results[sel] = handle_type({"tab_id": tab.get("id"), "selector": sel, 
                                     "text": str(val), "clear_first": True})
    submitted = False
    if submit:
        results["_submit"] = handle_click({"tab_id": tab.get("id"), "selector": submit})
        submitted = bool(results["_submit"].get("ok"))
    ok = all(v.get("ok") for v in results.values()) if results else True
    return {"ok": ok, "tab_id": tab.get("id"), "fields_filled": len(fields), 
            "submitted": submitted, "details": results}


def handle_login(args):
    # P0: credential_ref takes priority over plaintext username/password
    username = args.get("username", "")
    password = args.get("password", "")
    
    cred_ref = args.get("credential_ref", "")
    if cred_ref:
        if cred_ref.startswith("env:"):
            env_var = cred_ref[4:]
            val = os.environ.get(env_var, "")
            if ":" in val:
                parts = val.split(":", 1)
                username = username or parts[0]
                password = password or parts[1]
            else:
                username = username or val
        elif cred_ref.startswith("vault://"):
            # Vault integration: read from BROWSER_VAULT_* env vars or memory-wiki
            vault_key = cred_ref[8:]
            vault_user = os.environ.get(f"BROWSER_VAULT_{vault_key}_USER", "")
            vault_pass = os.environ.get(f"BROWSER_VAULT_{vault_key}_PASS", "")
            username = username or vault_user
            password = password or vault_pass
    
    if not username or not password:
        return {"ok": False, "error": "No credentials: use credential_ref or provide username/password"}
    
    nav = handle_navigate({"url": args["url"], "tab_id": args.get("tab_id"), "wait_until_ready": True})
    tab_id = nav.get("tab_id")
    w = handle_wait({"tab_id": tab_id, "selector": args["username_selector"], "timeout_ms": 15000})
    if not w.get("found"):
        return {"ok": False, "tab_id": tab_id, "error": "username selector not found", "url": nav.get("url")}
    fields = {args["username_selector"]: username, args["password_selector"]: password}
    if args.get("extra_fields"):
        fields.update(args["extra_fields"])
    filled = handle_fill_form({"tab_id": tab_id, "fields": fields, "submit_selector": args["submit_selector"]})
    time.sleep(3)
    cookie_result = handle_cookie_list({"tab_id": tab_id, "scope": "current_page"})
    return {"ok": bool(filled.get("ok")), "tab_id": tab_id, "login_completed": bool(filled.get("ok")), 
            "cookie_count": cookie_result.get("count", 0), "cookie_names": [c["name"] for c in cookie_result.get("cookies", [])],
            "sensitive_values_returned": False, "url": nav.get("url")}


def handle_tabs(args):
    tabs = _http("GET", "/json") or []
    include_non_page = bool(args.get("include_non_page", False))
    include_debug_url = bool(args.get("include_debug_url", False))
    selected = tabs if include_non_page else [t for t in tabs if t.get("type") == "page"]
    return {"ok": True, "tabs": [_public_tab(t, include_debug_url) for t in selected], 
            "count": len(selected), "total_targets": len(tabs), "debug_urls_included": include_debug_url}


def handle_newtab(args):
    url = args["url"]
    # P0: Navigation policy
    ok, reason = _validate_url(url)
    if not ok:
        return {"ok": False, "error": f"Navigation blocked: {reason}", "url": url}
    tab = _http("PUT", f"/json/new?{urllib.parse.quote(url, safe='')}")
    if args.get("wait_until_ready", True):
        time.sleep(0.5)
        tab = _get_tab({"tab_id": tab.get("id")}) or tab
        _wait_ready(tab, min(TIMEOUT, 20))
    result = {"ok": True, "tab_id": tab.get("id"), "url": tab.get("url"), "title": tab.get("title")}
    if bool(args.get("include_debug_url", False)):
        result["webSocketDebuggerUrl"] = tab.get("webSocketDebuggerUrl")
    return result


def handle_closetab(args):
    tab_id = args.get("tab_id")
    url_filter = args.get("url_filter")
    if url_filter:
        for t in _page_tabs():
            if url_filter in (t.get("url", "") or ""):
                tab_id = t.get("id")
                break
    if tab_id:
        resp = _http("GET", f"/json/close/{urllib.parse.quote(tab_id, safe='')}", raw=True)
        return {"ok": True, "closed": tab_id, 
                "response": resp.decode("utf-8", errors="replace") if isinstance(resp, bytes) else str(resp)}
    return {"ok": False, "error": "No tab to close"}


def handle_scroll(args):
    tab = _get_tab(args)
    direction = args.get("direction", "down")
    sel = args.get("selector")
    amount = int(args.get("amount", 500))
    if sel:
        expr = f"(document.querySelector({_json_arg(sel)})?.scrollIntoView({{block:'center'}}), 'scrolled to {sel}')"
    elif direction == "down":
        expr = f"window.scrollBy(0, {amount}); 'scrolled down {amount}px'"
    elif direction == "up":
        expr = f"window.scrollBy(0, {-amount}); 'scrolled up {amount}px'"
    elif direction == "top":
        expr = "window.scrollTo(0, 0); 'scrolled to top'"
    elif direction == "bottom":
        expr = "window.scrollTo(0, document.body.scrollHeight); 'scrolled to bottom'"
    else:
        expr = f"window.scrollBy(0, {amount}); 'scrolled'"
    return {"ok": True, "tab_id": tab.get("id"), 
            "result": _eval(tab, expr, timeout=10).get("value", "done")}


def handle_pdf(args):
    tab = _get_tab(args)
    r = _safe_ws(tab, [{"method": "Page.printToPDF", 
                        "params": {"format": "A4", "printBackground": True}, "_key": "pdf"}], timeout=TIMEOUT)
    data = r.get("pdf", {}).get("data", "")
    return {"ok": True, "tab_id": tab.get("id"), "pdf_base64": data, 
            "bytes_estimate": len(data) * 3 // 4, "format": "pdf"}


def handle_pdf_file(args):
    pdf = handle_pdf(args)
    saved = _write_b64_artifact(args.get("filename_prefix", "browser-page"), "pdf", 
                                 pdf.get("pdf_base64", ""))
    return {"ok": True, "tab_id": pdf.get("tab_id"), "path": saved["path"], 
            "bytes": saved["bytes"], "media_hint": _media_hint("pdf")}


def handle_html_file(args):
    tab = _get_tab(args)
    html = _eval(tab, "document.documentElement ? document.documentElement.outerHTML : ''", 
                 timeout=15).get("value", "")
    saved = _write_text_artifact(args.get("filename_prefix", "browser-page"), "html", html)
    title = _eval(tab, "document.title", timeout=10).get("value", "")
    url = _eval(tab, "window.location.href", timeout=10).get("value", "")
    return {"ok": True, "tab_id": tab.get("id"), "path": saved["path"], "bytes": saved["bytes"], 
            "media_hint": _media_hint("html"), "url": url, "title": title}


def _capture_network(tab: Dict[str, Any], duration_ms: int, reload_page: bool, 
                     include_all: bool, max_items: int) -> Dict[str, Any]:
    """Original lightweight network capture (v1.4 compatible)."""
    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("Tab has no webSocketDebuggerUrl")
    duration_ms = max(250, min(int(duration_ms), 30000))
    max_items = max(1, min(int(max_items), 1000))
    requests: Dict[str, Dict[str, Any]] = {}
    ws = websocket.create_connection(ws_url, timeout=max(TIMEOUT, int(duration_ms / 1000) + 5))
    try:
        cid = 1
        ws.send(json.dumps({"id": cid, "method": "Network.enable", 
                           "params": {"maxTotalBufferSize": 5242880, "maxResourceBufferSize": 1048576}}))
        cid += 1
        if reload_page:
            ws.send(json.dumps({"id": cid, "method": "Page.reload", "params": {"ignoreCache": True}}))
            cid += 1
        end = time.time() + duration_ms / 1000.0
        while time.time() < end:
            ws.settimeout(max(0.05, min(0.5, end - time.time())))
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            method = msg.get("method")
            params = msg.get("params", {})
            rid = params.get("requestId")
            if not rid:
                continue
            item = requests.setdefault(rid, {"request_id": rid})
            if method == "Network.requestWillBeSent":
                req = params.get("request", {})
                item.update({
                    "url": req.get("url", ""), "method": req.get("method", "GET"),
                    "resource_type": params.get("type", ""),
                    "initiator_type": (params.get("initiator") or {}).get("type", ""),
                })
            elif method == "Network.responseReceived":
                resp = params.get("response", {})
                item.update({
                    "url": resp.get("url", item.get("url", "")),
                    "resource_type": params.get("type", item.get("resource_type", "")),
                    "status": resp.get("status"), "mime_type": resp.get("mimeType", ""),
                    "from_cache": bool(resp.get("fromDiskCache") or resp.get("fromPrefetchCache")),
                })
            elif method == "Network.loadingFailed":
                item.update({"failed": True, "error_text": params.get("errorText", ""), 
                            "resource_type": params.get("type", item.get("resource_type", ""))})
    finally:
        try:
            ws.close()
        except Exception:
            pass
    rows = list(requests.values())
    if not include_all:
        rows = [r for r in rows if r.get("resource_type") in {"XHR", "Fetch"} or 
                "json" in str(r.get("mime_type", "")).lower() or 
                re.search(r"/api/|graphql|\.json(\?|$)", str(r.get("url", "")), re.I)]
    rows = rows[:max_items]
    return {"ok": True, "tab_id": tab.get("id"), "duration_ms": duration_ms, 
            "count": len(rows), "requests": rows, "redacted": True}


def handle_network_log(args):
    tab = _get_tab(args)
    return _capture_network(tab, int(args.get("duration_ms", 3000)), 
                           bool(args.get("reload", False)), bool(args.get("include_all", False)), 
                           int(args.get("max_items", 100)))


def handle_find_api_calls(args):
    tab = _get_tab(args)
    data = _capture_network(tab, int(args.get("duration_ms", 3000)), 
                           bool(args.get("reload", True)), False, int(args.get("max_items", 60)))
    api_calls = []
    for item in data.get("requests", []):
        url = str(item.get("url", ""))
        score = 0
        if item.get("resource_type") in {"XHR", "Fetch"}: score += 2
        if "json" in str(item.get("mime_type", "")).lower(): score += 2
        if re.search(r"/api/|graphql|\.json(\?|$)", url, re.I): score += 3
        api_calls.append({**item, "api_score": score})
    api_calls.sort(key=lambda x: x.get("api_score", 0), reverse=True)
    data["api_calls"] = api_calls
    return data


def handle_health(args):
    if args.get("autostart", True):
        _ensure_cdp_started("health")
    version = _cdp_probe(timeout=3)
    tabs: List[Dict[str, Any]] = []
    err = ""
    if version:
        try:
            tabs = _page_tabs()
        except Exception as e:
            err = str(e)[:300]
    
    result = {
        "ok": bool(version),
        "mcp": {"name": "browser-automation-mcp", "version": SERVER_VERSION, 
                "tool_count": len(TOOLS)},
        "cdp": CDP,
        "browser": (version or {}).get("Browser", ""),
        "protocol": (version or {}).get("Protocol-Version", ""),
        "tabs_count": len(tabs),
        "tabs": [{"id": t.get("id"), "url": t.get("url"), "title": t.get("title"), "type": t.get("type")} 
                 for t in tabs[:20]],
        "autostart_enabled": AUTOSTART_CDP,
        "start_cmd": CDP_START_CMD,
        "error": err,
        # P0 additions
        "circuit_breaker": CDP_CIRCUIT.state,
        "active_connections": len(CDPConnectionManager._connections),
    }
    
    if args.get("include_metrics", True) and METRICS_ENABLED:
        result["metrics"] = METRICS.snapshot()
    
    return result


def handle_page_summary(args):
    tab = _get_tab(args)
    max_text = int(args.get("max_text", 4000))
    max_items = int(args.get("max_items", 30))
    expr = f"""
    (() => {{
      const lim = {max_items};
      const crop = (s, n=240) => String(s || '').replace(/\\s+/g, ' ').trim().slice(0, n);
      const attrs = (el, names) => Object.fromEntries(names.map(n => [n, el.getAttribute(n)]).filter(x => x[1] !== null && x[1] !== ''));
      return {{
        title: document.title, url: location.href,
        text: crop(document.body ? document.body.innerText : '', {max_text}),
        headings: Array.from(document.querySelectorAll('h1,h2,h3')).slice(0, lim).map(el => ({{tag: el.tagName, text: crop(el.innerText, 180)}})),
        links: Array.from(document.links).slice(0, lim).map(a => ({{text: crop(a.innerText || a.title, 160), href: a.href}})),
        inputs: Array.from(document.querySelectorAll('input,textarea,select')).slice(0, lim).map(el => ({{tag: el.tagName, type: el.type || '', name: el.name || '', id: el.id || '', placeholder: el.placeholder || '', value: crop(el.value, 80), attrs: attrs(el, ['aria-label','autocomplete','role'])}})),
        buttons: Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]')).slice(0, lim).map(el => ({{tag: el.tagName, text: crop(el.innerText || el.value || el.getAttribute('aria-label'), 160), id: el.id || '', name: el.name || ''}})),
        forms: Array.from(document.forms).slice(0, lim).map(f => ({{id: f.id || '', name: f.name || '', action: f.action || '', method: f.method || ''}}))
      }};
    }})()
    """
    value = _eval(tab, expr, timeout=15).get("value", {})
    return {"ok": True, "tab_id": tab.get("id"), **(value or {})}


def handle_snapshot(args):
    tab = _get_tab(args)
    base_args = {"tab_id": tab.get("id"), "max_text": int(args.get("max_text", 4000)), 
                 "max_items": int(args.get("max_items", 40))}
    summary = handle_page_summary(base_args)
    elements = handle_elements({"tab_id": tab.get("id"), "max_items": int(args.get("max_items", 40)), 
                                "kind": "all"})
    result: Dict[str, Any] = {
        "ok": True, "tab_id": tab.get("id"),
        "url": summary.get("url"), "title": summary.get("title"),
        "text": summary.get("text", ""), "headings": summary.get("headings", []),
        "links": summary.get("links", []), "buttons": summary.get("buttons", []),
        "inputs": summary.get("inputs", []), "forms": summary.get("forms", []),
        "elements": elements.get("elements", []), "elements_count": elements.get("count", 0),
    }
    if bool(args.get("include_screenshot", True)):
        try:
            shot = handle_screenshot_file({"tab_id": tab.get("id"), 
                                          "filename_prefix": "browser-snapshot", "full_page": False})
            result["screenshot_path"] = shot.get("path")
            result["screenshot_bytes"] = shot.get("bytes")
            result["screenshot_media_hint"] = shot.get("media_hint")
        except Exception as e:
            result["screenshot_error"] = str(e)[:300]
    return result


def handle_select(args):
    tab = _get_tab(args)
    sel = args["selector"]
    value = args.get("value")
    label = args.get("label")
    index = args.get("index")
    expr = f"""
    (function(sel, value, label, index) {{
      const el = document.querySelector(sel);
      if (!el) return {{ok:false, error:'select not found', selector: sel}};
      if (el.tagName !== 'SELECT') return {{ok:false, error:'element is not select', tag: el.tagName}};
      const opts = Array.from(el.options);
      let opt = null;
      if (value !== null && value !== undefined && value !== '') opt = opts.find(o => o.value == value);
      if (!opt && label) opt = opts.find(o => (o.textContent || '').trim().includes(label));
      if (!opt && Number.isInteger(index) && index >= 0 && index < opts.length) opt = opts[index];
      if (!opt) return {{ok:false, error:'option not found', options: opts.slice(0,20).map(o => ({{value:o.value, text:o.textContent.trim()}}))}};
      el.value = opt.value;
      opt.selected = true;
      el.dispatchEvent(new Event('input', {{bubbles:true}}));
      el.dispatchEvent(new Event('change', {{bubbles:true}}));
      return {{ok:true, value: el.value, text: opt.textContent.trim(), selectedIndex: el.selectedIndex}};
    }})({_json_arg(sel)}, {_json_arg(value)}, {_json_arg(label)}, {_json_arg(index)})
    """
    result = _eval(tab, expr, timeout=10).get("value") or {}
    return {"ok": bool(result.get("ok")), "tab_id": tab.get("id"), **result}


def handle_elements(args):
    tab = _get_tab(args)
    selector = args.get("selector") or "a,button,input,textarea,select,label,summary,[role='button'],[role='link'],[onclick],[tabindex]"
    query = str(args.get("query") or "")
    kind = str(args.get("kind") or "all").lower()
    max_items = max(1, min(int(args.get("max_items", 80)), 500))
    include_hidden = bool(args.get("include_hidden", False))
    expr = r"""
    (() => {
      const selector = %(selector)s;
      const queryRaw = %(query)s;
      const kind = %(kind)s;
      const maxItems = %(max_items)s;
      const includeHidden = %(include_hidden)s;
      const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
      const crop = (s, n=180) => norm(s).slice(0, n);
      const fold = (s) => norm(s).toLowerCase();
      const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(String(s)) : String(s).replace(/["\\#.:\[\]>~+*^$|=]/g, '\\$&');
      const quoteAttr = (s) => String(s || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
      const visible = (el) => {
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity || 1) > 0 && r.width > 0 && r.height > 0;
      };
      const textOf = (el) => norm(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || el.placeholder || '');
      const classify = (el) => {
        const tag = el.tagName.toLowerCase();
        const role = (el.getAttribute('role') || '').toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (tag === 'a' || role === 'link') return 'link';
        if (tag === 'select') return 'select';
        if (tag === 'input' || tag === 'textarea') return 'input';
        if (tag === 'button' || role === 'button' || ['button','submit','reset'].includes(type)) return 'button';
        return 'clickable';
      };
      const cssPath = (node) => {
        if (!node || node.nodeType !== 1) return '';
        if (node.id) return '#' + esc(node.id);
        const name = node.getAttribute('name');
        if (name) {
          const byName = `${node.tagName.toLowerCase()}[name="${quoteAttr(name)}"]`;
          try { if (document.querySelectorAll(byName).length === 1) return byName; } catch (e) {}
        }
        const parts = [];
        let el = node;
        while (el && el.nodeType === 1 && el !== document.body && parts.length < 6) {
          let part = el.tagName.toLowerCase();
          const classes = Array.from(el.classList || []).filter(Boolean).slice(0, 2);
          if (classes.length) part += '.' + classes.map(esc).join('.');
          const parent = el.parentElement;
          if (!parent) break;
          const siblings = Array.from(parent.children).filter(x => x.tagName === el.tagName);
          if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(el) + 1})`;
          parts.unshift(part);
          const candidate = parts.join(' > ');
          try { if (document.querySelectorAll(candidate).length === 1) return candidate; } catch (e) {}
          el = parent;
        }
        return parts.join(' > ');
      };
      const query = fold(queryRaw);
      const nodes = Array.from(document.querySelectorAll(selector));
      const filtered = nodes.filter(el => {
        if (!includeHidden && !visible(el)) return false;
        const itemKind = classify(el);
        if (kind !== 'all' && kind !== itemKind && !(kind === 'clickable' && ['link','button','clickable'].includes(itemKind))) return false;
        if (!query) return true;
        const hay = fold([textOf(el), el.id, el.name, el.placeholder, el.getAttribute('aria-label'), el.getAttribute('title'), el.href].join(' '));
        return hay.includes(query);
      });
      const elements = filtered.slice(0, maxItems).map((el, i) => {
        const r = el.getBoundingClientRect();
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();
        const sensitive = /password|passwd|token|secret|auth|bearer|session|cookie/i.test([type, el.name, el.id, el.placeholder, el.autocomplete].join(' '));
        return {
          index: i, kind: classify(el), selector: cssPath(el), tag, type,
          text: crop(textOf(el), 220), id: el.id || '', name: el.name || '',
          placeholder: crop(el.placeholder || '', 160),
          value: sensitive ? (el.value ? '[redacted]' : '') : crop(el.value || '', 120),
          role: el.getAttribute('role') || '', href: el.href || '', visible: visible(el),
          rect: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}
        };
      });
      return {title: document.title, url: location.href, count: filtered.length, returned: elements.length, elements};
    })()
    """ % {
        "selector": _json_arg(selector),
        "query": _json_arg(query),
        "kind": _json_arg(kind),
        "max_items": max_items,
        "include_hidden": _json_arg(include_hidden),
    }
    value = _eval(tab, expr, timeout=15).get("value", {}) or {}
    return {"ok": True, "tab_id": tab.get("id"), **value}


def handle_click_text(args):
    tab = _get_tab(args)
    text = str(args["text"])
    selector = args.get("selector") or "button,a,[role='button'],[role='link'],input[type='button'],input[type='submit'],label,summary,[onclick]"
    exact = bool(args.get("exact", False))
    case_sensitive = bool(args.get("case_sensitive", False))
    index = max(0, int(args.get("index", 0)))
    wait_after_ms = max(0, min(int(args.get("wait_after_ms", 300)), 10000))
    expr = r"""
    ((selector, wanted, exact, caseSensitive, index) => {
      const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
      const cmp = (s) => caseSensitive ? norm(s) : norm(s).toLowerCase();
      const needle = cmp(wanted);
      const visible = (el) => {
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity || 1) > 0 && r.width > 0 && r.height > 0;
      };
      const textOf = (el) => norm(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || el.placeholder || '');
      const matches = Array.from(document.querySelectorAll(selector)).filter(el => {
        if (!visible(el)) return false;
        const hay = cmp(textOf(el));
        return exact ? hay === needle : hay.includes(needle);
      });
      const candidates = matches.slice(0, 20).map((el, i) => ({index: i, tag: el.tagName, text: textOf(el).slice(0, 180), href: el.href || '', id: el.id || '', name: el.name || ''}));
      if (!matches.length) return {ok: false, error: 'text not found', text: wanted, selector, candidates};
      const el = matches[Math.min(index, matches.length - 1)];
      const r = el.getBoundingClientRect();
      el.scrollIntoView({block: 'center', inline: 'center'});
      try { el.focus({preventScroll: true}); } catch (e) { try { el.focus(); } catch (_) {} }
      el.click();
      return {ok: true, clicked: true, text: textOf(el).slice(0, 220), tag: el.tagName, href: el.href || '', id: el.id || '', name: el.name || '', matches_count: matches.length, rect: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}};
    })(%(selector)s, %(text)s, %(exact)s, %(case_sensitive)s, %(index)s)
    """ % {"selector": _json_arg(selector), "text": _json_arg(text), "exact": _json_arg(exact),
           "case_sensitive": _json_arg(case_sensitive), "index": index}
    result = _eval(tab, expr, timeout=10).get("value") or {}
    if result.get("ok") and wait_after_ms:
        time.sleep(wait_after_ms / 1000.0)
    return {"ok": bool(result.get("ok")), "tab_id": tab.get("id"), **result}


def handle_wait_text(args):
    tab = _get_tab(args)
    text = str(args["text"])
    selector = args.get("selector") or "body"
    exact = bool(args.get("exact", False))
    case_sensitive = bool(args.get("case_sensitive", False))
    timeout_ms = max(0, int(args.get("timeout_ms", 10000)))
    expr = r"""
    new Promise((resolve) => {
      const selector = %(selector)s;
      const wanted = %(text)s;
      const exact = %(exact)s;
      const caseSensitive = %(case_sensitive)s;
      const timeoutMs = %(timeout_ms)s;
      const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
      const cmp = (s) => caseSensitive ? norm(s) : norm(s).toLowerCase();
      const needle = cmp(wanted);
      const deadline = Date.now() + timeoutMs;
      const check = () => {
        try {
          const nodes = Array.from(document.querySelectorAll(selector));
          const match = nodes.find(el => {
            const hay = cmp(el.innerText || el.textContent || '');
            return exact ? hay === needle : hay.includes(needle);
          });
          if (match) return resolve({found: true, selector, text: norm((match.innerText || match.textContent || '')).slice(0, 220)});
          if (Date.now() >= deadline) return resolve({found: false, selector, text: wanted});
          setTimeout(check, 200);
        } catch (e) { return resolve({found: false, selector, text: wanted, error: String(e)}); }
      };
      check();
    })
    """ % {"selector": _json_arg(selector), "text": _json_arg(text), "exact": _json_arg(exact),
           "case_sensitive": _json_arg(case_sensitive), "timeout_ms": timeout_ms}
    result = _eval(tab, expr, await_promise=True, timeout=max(int(timeout_ms / 1000) + 5, 10)).get("value") or {}
    return {"ok": True, "tab_id": tab.get("id"), **result}


def handle_batch(args):
    """P1 enhanced: parallel execution of independent steps with dependency analysis."""
    steps = args.get("steps") or []
    stop = bool(args.get("stop_on_error", True))
    
    # P1: detect parallelizable steps (no shared tab dependency)
    # For now, sequential with stop_on_error — parallel mode opt-in via parallel=true
    parallel = bool(args.get("parallel", False))
    
    results = []
    if parallel:
        # Execute independent steps in threads (simplified — real impl would use asyncio)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(steps), 4)) as executor:
            futures = {}
            for i, step in enumerate(steps):
                name = step.get("tool") or step.get("name")
                handler = HANDLERS.get(name)
                if handler:
                    futures[executor.submit(handler, step.get("arguments") or {})] = (i, name)
            for future in concurrent.futures.as_completed(futures):
                i, name = futures[future]
                try:
                    results.append({"index": i, "tool": name, "result": future.result(timeout=60)})
                except Exception as e:
                    results.append({"index": i, "tool": name, "result": {"ok": False, "error": str(e)[:500]}})
        results.sort(key=lambda r: r["index"])
    else:
        for i, step in enumerate(steps):
            name = step.get("tool") or step.get("name")
            if name == "browser_batch":
                item = {"ok": False, "error": "nested browser_batch is not allowed"}
            else:
                handler = HANDLERS.get(name)
                if not handler:
                    item = {"ok": False, "error": f"unknown tool: {name}"}
                else:
                    try:
                        item = handler(step.get("arguments") or {})
                    except Exception as e:
                        item = {"ok": False, "error": str(e)[:500]}
            results.append({"index": i, "tool": name, "result": item})
            if stop and isinstance(item, dict) and item.get("ok") is False:
                break
    
    return {"ok": all((r.get("result") or {}).get("ok", True) for r in results), "results": results}


# ====== NEW v2.0 HANDLERS ======

def handle_network_har(args):
    """P2: Full HAR-like network capture."""
    tab = _get_tab(args)
    duration_ms = int(args.get("duration_ms", 5000))
    include_bodies = bool(args.get("include_bodies", False))
    return _capture_network_har(tab, duration_ms, include_bodies)


def handle_recall(args):
    """P1: Recall cached page data from memory-wiki."""
    url_pattern = args.get("url_pattern", "")
    result = _recall_from_wiki(url_pattern)
    if result is None:
        return {"ok": True, "found": False, "note": "memory-wiki integration not configured (stub)"}
    return {"ok": True, **result}


def handle_session_tabs(args):
    """P1: Session tab management — list/switch/close_all/close_others."""
    action = args.get("action", "list")
    tabs = _http("GET", "/json") or []
    page_tabs = [t for t in tabs if t.get("type") == "page" and 
                 not (t.get("url") or "").startswith("devtools://")]
    
    if action == "list":
        return {"ok": True, "tabs": [_public_tab(t) for t in page_tabs], "count": len(page_tabs)}
    
    elif action == "switch":
        # Activate first page tab
        if page_tabs:
            try:
                _http("GET", f"/json/activate/{urllib.parse.quote(page_tabs[0]['id'], safe='')}")
            except Exception:
                pass
            return {"ok": True, "active": _public_tab(page_tabs[0])}
        return {"ok": False, "error": "No page tabs to switch to"}
    
    elif action == "close_all":
        closed = []
        for t in page_tabs:
            try:
                _http("GET", f"/json/close/{urllib.parse.quote(t['id'], safe='')}", raw=True)
                closed.append(t["id"])
            except Exception:
                pass
        return {"ok": True, "closed": closed, "count": len(closed)}
    
    elif action == "close_others":
        if len(page_tabs) <= 1:
            return {"ok": True, "closed": [], "count": 0}
        active = page_tabs[0]
        closed = []
        for t in page_tabs[1:]:
            try:
                _http("GET", f"/json/close/{urllib.parse.quote(t['id'], safe='')}", raw=True)
                closed.append(t["id"])
            except Exception:
                pass
        return {"ok": True, "active": _public_tab(active), "closed": closed, "count": len(closed)}


def handle_crawl_extract(args):
    """P3: Deep crawl — navigate + extract links + shallow recursive collection."""
    url = args["url"]
    rules = args.get("extract_rules", {}) or {}
    max_depth = min(int(args.get("max_depth", 1)), 3)
    max_pages = min(int(args.get("max_pages", 10)), 50)
    same_domain_only = bool(args.get("same_domain_only", True))
    
    text_sel = rules.get("text_selector", "body")
    link_sel = rules.get("link_selector", "a[href]")
    data_selectors = rules.get("data_selectors", {})
    
    visited = set()
    results = []
    queue = [(url, 0)]
    base_domain = urllib.parse.urlparse(url).netloc
    
    while queue and len(visited) < max_pages:
        current_url, depth = queue.pop(0)
        if current_url in visited:
            continue
        visited.add(current_url)
        
        try:
            tab = _get_tab({"url_filter": ""}, create=True)
            r = _safe_ws(tab, [{"method": "Page.navigate", "params": {"url": current_url}, "_key": "nav"}], timeout=20)
            time.sleep(1.5)
            _wait_ready(tab, 10)
            
            page_data = {
                "url": current_url,
                "depth": depth,
                "title": _eval(tab, "document.title", timeout=5).get("value", ""),
            }
            
            # Extract text
            if text_sel:
                page_data["text"] = _eval(tab, 
                    f"(document.querySelector({_json_arg(text_sel)}) || {{}}).innerText || ''", 
                    timeout=10).get("value", "")[:5000]
            
            # Extract custom data selectors
            if data_selectors:
                extracted = {}
                for name, css in data_selectors.items():
                    val = _eval(tab, 
                        f"(document.querySelector({_json_arg(css)}) || {{}}).innerText || ''", 
                        timeout=5).get("value", "")[:2000]
                    extracted[name] = val
                page_data["data"] = extracted
            
            results.append(page_data)
            
            # Extract links for next depth
            if depth < max_depth and link_sel:
                links_js = f"""
                Array.from(document.querySelectorAll({_json_arg(link_sel)}))
                    .map(a => a.href).filter(h => h && h.startsWith('http'))
                """
                links = _eval(tab, links_js, timeout=10).get("value", []) or []
                for link in links[:20]:
                    link_domain = urllib.parse.urlparse(link).netloc
                    if same_domain_only and link_domain != base_domain:
                        continue
                    if link not in visited:
                        queue.append((link, depth + 1))
        
        except Exception as e:
            results.append({"url": current_url, "depth": depth, "error": str(e)[:300]})
    
    return {
        "ok": True,
        "start_url": url,
        "pages_crawled": len(results),
        "max_depth": max_depth,
        "same_domain_only": same_domain_only,
        "results": results,
    }


def handle_click_heal(args):
    """P2: Self-healing click with multiple resolution strategies."""
    tab = _get_tab(args)
    text = str(args.get("text", ""))
    
    # Build selector pack from args
    pack = args.get("selector_pack") or {}
    if text and not pack.get("text"):
        pack["text"] = text
    
    if not pack:
        return {"ok": False, "error": "text or selector_pack required"}
    
    return SelectorPack.click_element(tab, pack)


def handle_metrics(args):
    """P0: Return server metrics snapshot."""
    return {
        "ok": True,
        "metrics": METRICS.snapshot(),
        "circuit_breaker": {
            "name": CDP_CIRCUIT.name,
            "state": CDP_CIRCUIT.state,
            "failures": CDP_CIRCUIT._failures,
        },
        "connections": {
            "active": len(CDPConnectionManager._connections),
        },
    }


# ====== HANDLER REGISTRY ======

# v2.1: File upload via CDP DOM.setFileInputFiles
def handle_upload_file(args):
    """Upload file to input[type=file] on page via CDP."""
    import base64 as _b64
    file_path = str(args.get("file_path", "")).strip()
    if not file_path:
        return {"ok": False, "error": "file_path is required"}
    p = os.path.expanduser(file_path)
    if not os.path.isfile(p):
        return {"ok": False, "error": f"File not found: {p}"}

    tab = _get_tab(args)
    if not tab:
        return {"ok": False, "error": "No active tab — navigate first"}

    selector = str(args.get("selector", "input[type=file]")).strip()

    # Step 1: DOM.querySelector to find the file input
    doc_cmd = [{"method": "DOM.getDocument", "params": {"depth": 0}}]
    doc = _safe_ws(tab, doc_cmd)
    if not doc or doc[0].get("error"):
        return {"ok": False, "error": f"CDP DOM.getDocument failed"}
    root_id = doc[0]["result"]["root"]["nodeId"]

    qr = _safe_ws(tab, [{"method": "DOM.querySelector", "params": {"nodeId": root_id, "selector": selector}}])
    if not qr or qr[0].get("error"):
        return {"ok": False, "error": f"Selector '{selector}' not found"}
    node_id = qr[0]["result"].get("nodeId")
    if not node_id or node_id == 0:
        return {"ok": False, "error": f"File input '{selector}' not found on page"}

    # Step 2: resolve -> requestNode -> backendNodeId
    rn = _safe_ws(tab, [{"method": "DOM.resolveNode", "params": {"nodeId": node_id}}])
    if not rn or rn[0].get("error"):
        return {"ok": False, "error": "DOM.resolveNode failed"}
    oid = rn[0]["result"]["object"].get("objectId")
    if not oid:
        return {"ok": False, "error": "No objectId from DOM.resolveNode"}

    nr = _safe_ws(tab, [{"method": "DOM.requestNode", "params": {"objectId": oid}}])
    if not nr or nr[0].get("error"):
        return {"ok": False, "error": "DOM.requestNode failed"}
    bnid = nr[0]["result"].get("nodeId")
    if not bnid:
        return {"ok": False, "error": "No backendNodeId from DOM.requestNode"}

    # Step 3: read file and upload
    try:
        with open(p, "rb") as f:
            data = f.read()
    except Exception as e:
        return {"ok": False, "error": f"Cannot read file: {e}"}

    file_b64 = _b64.b64encode(data).decode("ascii")
    sf = _safe_ws(tab, [{"method": "DOM.setFileInputFiles", "params": {"files": [file_b64], "backendNodeId": bnid}}])
    if not sf or sf[0].get("error"):
        return {"ok": False, "error": "DOM.setFileInputFiles failed"}

    # Step 4: trigger change event so page detects the upload
    esc = selector.replace(chr(39), chr(92) + chr(39))
    trigger_js = "(()=>{const e=document.querySelector('" + esc + "');if(e){e.dispatchEvent(new Event('change',{bubbles:true}));e.dispatchEvent(new Event('input',{bubbles:true}));return 1;}return 0;})()"
    _eval(tab, trigger_js)

    return {"ok": True, "tab_id": tab.get("id"), "file": os.path.basename(p), "size": len(data), "selector": selector}

# ====== v1.5 COOKIE API (P0) ======

def handle_cookie_list(args):
    """List cookies with scope: current_page | browser_context."""
    tab = _get_tab(args)
    scope = str(args.get("scope", "current_page"))
    include_values = bool(args.get("include_values", False))
    confirm_sensitive = bool(args.get("confirm_sensitive_data", False))
    domain_filter = str(args.get("domain_filter", "")).lower()
    
    if scope == "browser_context":
        r = _safe_ws(tab, [{"method": "Storage.getCookies", "_key": "cookies"}], timeout=10)
        cookies = r.get("cookies", {}).get("cookies", [])
    else:
        url = _eval(tab, "window.location.href", timeout=5).get("value", "")
        r = _safe_ws(tab, [{"method": "Network.getCookies", "params": {"urls": [url]} if url else {}, "_key": "cookies"}], timeout=10)
        cookies = r.get("cookies", {}).get("cookies", [])
    
    if domain_filter:
        cookies = [c for c in cookies if domain_filter in (c.get("domain", "") or "").lower()]
    
    # Structure: hide values unless explicitly requested
    result_cookies = []
    for c in cookies:
        entry = {
            "name": c.get("name", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": c.get("sameSite", "Lax"),
            "expires": c.get("expires", 0),
            "value_present": bool(c.get("value")),
        }
        if include_values and confirm_sensitive:
            entry["value"] = c.get("value")
            entry["value_sha256"] = hashlib.sha256((c.get("value") or "").encode()).hexdigest()
        elif include_values:
            entry["value"] = f"[redacted len={len(c.get('value', '') or '')}]"
        result_cookies.append(entry)
    
    return {"ok": True, "tab_id": tab.get("id"), "scope": scope, "count": len(result_cookies), 
            "cookies": result_cookies, "sensitive_values_returned": include_values and confirm_sensitive}


def handle_cookie_set(args):
    """Set a cookie via Storage.setCookies."""
    tab = _get_tab(args)
    cookie = {
        "name": str(args["name"]),
        "value": str(args["value"]),
        "domain": str(args.get("domain", "")),
        "path": str(args.get("path", "/")),
        "httpOnly": bool(args.get("httpOnly", False)),
        "secure": bool(args.get("secure", True)),
        "sameSite": str(args.get("sameSite", "Lax")),
    }
    if args.get("expires"):
        cookie["expires"] = float(args["expires"])
    
    r = _safe_ws(tab, [{"method": "Storage.setCookies", "params": {"cookies": [cookie]}, "_key": "result"}], timeout=10)
    err = r.get("result", {})
    return {"ok": True, "tab_id": tab.get("id"), "set": cookie["name"], 
            "domain": cookie["domain"], "error": err.get("error")}


def handle_cookie_delete(args):
    """Delete cookies by name/domain/path."""
    tab = _get_tab(args)
    name = str(args.get("name", ""))
    domain = str(args.get("domain", ""))
    path = str(args.get("path", "/"))
    
    # Get current cookies, filter, delete matching
    r = _safe_ws(tab, [{"method": "Network.getCookies", "_key": "cookies"}], timeout=10)
    cookies = r.get("cookies", {}).get("cookies", [])
    
    deleted = 0
    for c in cookies:
        match = True
        if name and c.get("name") != name:
            match = False
        if domain and c.get("domain") != domain:
            match = False
        if match:
            # Set with expired time to delete
            del_cookie = {**c, "expires": 1}
            _safe_ws(tab, [{"method": "Network.deleteCookies", "params": {
                "name": c["name"], "domain": c.get("domain", ""), "path": c.get("path", "/")
            }, "_key": "del"}], timeout=5)
            deleted += 1
    
    return {"ok": True, "tab_id": tab.get("id"), "deleted": deleted}


def handle_cookie_clear(args):
    """Clear all cookies for scope."""
    tab = _get_tab(args)
    scope = str(args.get("scope", "current_page"))
    
    if scope == "browser_context":
        r = _safe_ws(tab, [{"method": "Storage.clearCookies", "_key": "result"}], timeout=10)
    else:
        r = _safe_ws(tab, [{"method": "Network.clearBrowserCookies", "_key": "result"}], timeout=10)
    
    return {"ok": True, "tab_id": tab.get("id"), "scope": scope, "result": r.get("result", {})}


HANDLERS = {
    # v1.4
    "browser_navigate": handle_navigate,
    "browser_screenshot": handle_screenshot,
    "browser_screenshot_file": handle_screenshot_file,
    "browser_cookies": handle_cookies,
    "browser_localstorage": handle_localstorage,
    "browser_sessionstorage": handle_sessionstorage,
    "browser_exec": handle_exec,
    "browser_click": handle_click,
    "browser_type": handle_type,
    "browser_gettext": handle_gettext,
    "browser_gethtml": handle_gethtml,
    "browser_getvalue": handle_getvalue,
    "browser_wait": handle_wait,
    "browser_fill_form": handle_fill_form,
    "browser_login": handle_login,
    "browser_tabs": handle_tabs,
    "browser_newtab": handle_newtab,
    "browser_closetab": handle_closetab,
    "browser_scroll": handle_scroll,
    "browser_pdf": handle_pdf,
    "browser_pdf_file": handle_pdf_file,
    "browser_html_file": handle_html_file,
    "browser_health": handle_health,
    "browser_page_summary": handle_page_summary,
    "browser_snapshot": handle_snapshot,
    "browser_network_log": handle_network_log,
    "browser_find_api_calls": handle_find_api_calls,
    "browser_select": handle_select,
    "browser_elements": handle_elements,
    "browser_click_text": handle_click_text,
    "browser_wait_text": handle_wait_text,
    "browser_batch": handle_batch,
    # v2.0
    "browser_network_har": handle_network_har,
    "browser_recall": handle_recall,
    "browser_session_tabs": handle_session_tabs,
    "browser_crawl_extract": handle_crawl_extract,
    "browser_click_heal": handle_click_heal,
    "browser_metrics": handle_metrics,
    # v2.1
    "browser_upload_file": handle_upload_file,
}



# ====== MCP MAIN ======

def handle_request(msg):
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "browser-automation-mcp", "version": SERVER_VERSION}
        }})
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        tool_name = msg.get("params", {}).get("name")
        args = msg.get("params", {}).get("arguments", {}) or {}
        start = time.time()
        try:
            handler = HANDLERS.get(tool_name)
            if not handler:
                raise ValueError(f"Unknown tool: {tool_name}")
            result = handler(args)
            METRICS.incr(f"tool_{tool_name}")
            METRICS.observe(f"tool_{tool_name}_latency", time.time() - start)
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
            }})
        except Exception as e:
            _log(f"Tool error [{tool_name}]: {e}", level="error", tool=tool_name)
            METRICS.incr(f"tool_{tool_name}_errors")
            payload = {"ok": False, "error": f"{tool_name}: {str(e)[:500]}"}
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "isError": True
            }})
    elif method == "notifications/initialized":
        return
    else:
        _send({"jsonrpc": "2.0", "id": msg_id, "error": {
            "code": -32601, "message": f"Unknown method: {method}"
        }})


def main():
    _log(f"Browser Automation MCP Server v{SERVER_VERSION} started (OmniCouncil-hardened)")
    _log(f"CDP: {CDP} | Tabs: {MAX_TABS} | Heartbeat: {HEARTBEAT_INTERVAL}s | CB: {CIRCUIT_BREAKER_THRESHOLD} fails")
    try:
        _ensure_cdp_started("startup")
        version = _http("GET", "/json/version")
        _log(f"Browser: {version.get('Browser', 'unknown')}")
    except Exception as e:
        _log(f"WARNING: CDP not reachable: {e}", level="warn")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle_request(json.loads(line))
        except Exception as e:
            _log(f"Parse error: {e}", level="error")


if __name__ == "__main__":
    main()
