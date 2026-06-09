#!/usr/bin/env python3
"""
Universal Browser Automation MCP Server v1.3
Direct Chrome DevTools Protocol WebSocket client.

Keeps the original browser-control tools and adds operational health/autostart,
page summarization, element discovery, text-click/text-wait helpers, select controls,
and batch execution for fewer MCP round-trips.
Still uses direct Chrome DevTools Protocol HTTP/WebSocket and no public BrowserMCP.
"""

import json
import os
import sys
import time
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# ====== CONFIG ======
CDP = os.environ.get("BROWSER_CDP_URL", "http://127.0.0.1:9223").rstrip("/")
TOKEN = os.environ.get("CODEX_DEBUG_TOKEN", os.environ.get("BROWSER_AUTH_TOKEN", ""))
TIMEOUT = int(os.environ.get("BROWSER_TIMEOUT", "30"))
HTTP_TIMEOUT = int(os.environ.get("BROWSER_HTTP_TIMEOUT", "10"))
DEFAULT_TAB_URL = "about:blank"
AUTOSTART_CDP = os.environ.get("BROWSER_AUTOSTART_CDP", "1").lower() not in {"0", "false", "no", "off"}
CDP_START_CMD = os.environ.get("BROWSER_CDP_START_CMD", os.path.expanduser("~/.local/bin/browser-cdp-start"))
CDP_START_TIMEOUT = int(os.environ.get("BROWSER_CDP_START_TIMEOUT", "45"))
SERVER_VERSION = "1.3.0"
_cdp_start_attempted = False

try:
    import websocket
except ImportError:
    print("[browser-mcp] FATAL: websocket-client not installed. Run: pip install websocket-client", file=sys.stderr)
    sys.exit(1)

# ====== HELPERS ======

def _log(msg: str) -> None:
    print(f"[browser-mcp] {msg}", file=sys.stderr, flush=True)


def _send(data: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()



def _cdp_probe(timeout: int = 2) -> Optional[Dict[str, Any]]:
    """Return /json/version if CDP is reachable, otherwise None."""
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except Exception:
        return None


def _ensure_cdp_started(reason: str = "request") -> bool:
    """Best-effort CDP autostart; never loops more than once per process."""
    global _cdp_start_attempted
    if _cdp_probe(timeout=2):
        return True
    if not AUTOSTART_CDP or _cdp_start_attempted:
        return False
    _cdp_start_attempted = True
    if not CDP_START_CMD:
        return False
    try:
        _log(f"CDP not reachable ({reason}); starting via: {CDP_START_CMD}")
        proc = subprocess.run(
            ["bash", "-lc", CDP_START_CMD],
            text=True,
            capture_output=True,
            timeout=CDP_START_TIMEOUT,
            env=os.environ.copy(),
        )
        if proc.returncode != 0:
            _log(f"CDP start failed rc={proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}")
            return False
        deadline = time.time() + 10
        while time.time() < deadline:
            if _cdp_probe(timeout=2):
                return True
            time.sleep(0.5)
    except Exception as e:
        _log(f"CDP start exception: {e}")
    return bool(_cdp_probe(timeout=2))


def _http(method: str, path: str, data: Any = None, raw: bool = False, timeout: int = HTTP_TIMEOUT) -> Any:
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
        # Newer Chrome disallows PUT /json/new. Fall back to GET for compatibility.
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


def _get_tab(args: Optional[Dict[str, Any]] = None, create: bool = True) -> Optional[Dict[str, Any]]:
    """Find a page target by tab_id/url_filter or return/create the first page."""
    args = args or {}
    tab_id = args.get("tab_id") or args.get("id")
    url_filter = args.get("url_filter")
    tabs = _http("GET", "/json") or []

    if tab_id:
        for t in tabs:
            if t.get("id") == tab_id:
                return t
        if not create:
            return None

    if url_filter:
        filt = str(url_filter).lower()
        for t in tabs:
            if t.get("type") == "page" and filt in (t.get("url", "") or "").lower():
                return t

    for t in tabs:
        if t.get("type") == "page" and not (t.get("url", "") or "").startswith("devtools://"):
            return t

    if create:
        return _http("PUT", f"/json/new?{urllib.parse.quote(DEFAULT_TAB_URL, safe='')}")
    return None


def _ws_cmd(tab: Dict[str, Any], commands: List[Dict[str, Any]], timeout: int = TIMEOUT) -> Dict[str, Any]:
    """Execute CDP commands through one short-lived WebSocket connection."""
    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("Tab has no webSocketDebuggerUrl")

    ws = websocket.create_connection(ws_url, timeout=timeout)
    results: Dict[str, Any] = {}
    errors: List[str] = []
    next_id = 1
    pending: Dict[int, Dict[str, Any]] = {}

    try:
        # Enable domains. Ignore enable errors but drain their direct responses.
        enable_ids = []
        for domain in ["Page", "Network", "Runtime", "DOM"]:
            cid = next_id; next_id += 1
            enable_ids.append(cid)
            ws.send(json.dumps({"id": cid, "method": f"{domain}.enable"}))
        end = time.time() + min(3, max(1, timeout / 4))
        seen_enable = set()
        while len(seen_enable) < len(enable_ids) and time.time() < end:
            ws.settimeout(max(0.1, end - time.time()))
            try:
                msg = json.loads(ws.recv())
            except Exception:
                break
            if msg.get("id") in enable_ids:
                seen_enable.add(msg.get("id"))

        for original in commands:
            cmd = dict(original)  # do not mutate caller data
            cid = next_id; next_id += 1
            pending[cid] = cmd
            ws.send(json.dumps({
                "id": cid,
                "method": cmd["method"],
                "params": cmd.get("params", {})
            }))
            wait = cmd.get("wait_after_send")
            if wait:
                time.sleep(float(wait) / 1000.0 if float(wait) >= 100 else float(wait))

        end = time.time() + timeout
        while pending and time.time() < end:
            ws.settimeout(max(0.1, end - time.time()))
            try:
                d = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                break
            except Exception as e:
                errors.append(f"ws_recv: {e}")
                break
            mid = d.get("id")
            if mid not in pending:
                continue
            cmd = pending.pop(mid)
            key = cmd.get("_key", cmd.get("method", str(mid)))
            if "error" in d:
                err = d.get("error", {})
                errors.append(f"{key}: {err.get('message', str(err))}")
            else:
                results[key] = d.get("result", {})

        if pending:
            errors.append("timeout waiting for: " + ", ".join(c.get("_key", c.get("method", "?")) for c in pending.values()))
        if errors:
            # Preserve partial results for debugging but fail loudly.
            raise RuntimeError("; ".join(errors))
        return results
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _safe_ws(tab: Dict[str, Any], commands: List[Dict[str, Any]], timeout: int = TIMEOUT, retries: int = 1) -> Dict[str, Any]:
    """Wrapper with actual retry after timeout/websocket failure."""
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return _ws_cmd(tab, commands, timeout)
        except Exception as e:
            last = e
            retryable = any(s in str(e).lower() for s in ["timeout", "websocket", "ws_", "connection", "closed"])
            if attempt >= retries or not retryable:
                break
            try:
                tabs = _http("GET", "/json") or []
                tab = next((t for t in tabs if t.get("id") == tab.get("id")), tab)
            except Exception:
                pass
            time.sleep(0.25)
    raise last or RuntimeError("unknown websocket error")


def _eval(tab: Dict[str, Any], expression: str, *, await_promise: bool = False, return_by_value: bool = True, timeout: int = TIMEOUT) -> Dict[str, Any]:
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


def _redact_cookie(c: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(c)
    if "value" in out:
        out["value"] = f"[redacted len={len(str(out['value']))}]"
    return out

# ====== TOOLS ======

TAB_PROPS = {
    "tab_id": {"type": "string", "description": "Optional target tab id from browser_tabs"},
    "url_filter": {"type": "string", "description": "Optional substring to choose a tab by URL"},
}

TOOLS = [
    {"name": "browser_navigate", "description": "Перейти на указанный URL в браузере. Возвращает заголовок, URL и tab_id.", "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "description": "URL для перехода"}, "tab_id": TAB_PROPS["tab_id"], "wait_until_ready": {"type": "boolean", "default": True}}, "required": ["url"]}},
    {"name": "browser_screenshot", "description": "Сделать скриншот текущей страницы. Возвращает base64 PNG.", "inputSchema": {"type": "object", "properties": {"full_page": {"type": "boolean", "default": False}, **TAB_PROPS}}},
    {"name": "browser_cookies", "description": "Извлечь cookies, document.cookie и storage сайта.", "inputSchema": {"type": "object", "properties": {"url_filter": TAB_PROPS["url_filter"], "tab_id": TAB_PROPS["tab_id"], "redact": {"type": "boolean", "default": False}}}},
    {"name": "browser_localstorage", "description": "Получить localStorage сайта.", "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}, **TAB_PROPS}}},
    {"name": "browser_sessionstorage", "description": "Получить sessionStorage сайта.", "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}, **TAB_PROPS}}},
    {"name": "browser_exec", "description": "Выполнить JavaScript на странице и вернуть результат.", "inputSchema": {"type": "object", "properties": {"expression": {"type": "string"}, "await_promise": {"type": "boolean", "default": False}, **TAB_PROPS}, "required": ["expression"]}},
    {"name": "browser_click", "description": "Кликнуть по элементу по CSS-селектору.", "inputSchema": {"type": "object", "properties": {"selector": {"type": "string"}, **TAB_PROPS}, "required": ["selector"]}},
    {"name": "browser_type", "description": "Ввести текст в поле ввода.", "inputSchema": {"type": "object", "properties": {"selector": {"type": "string"}, "text": {"type": "string"}, "clear_first": {"type": "boolean", "default": True}, **TAB_PROPS}, "required": ["selector", "text"]}},
    {"name": "browser_gettext", "description": "Получить innerText элемента.", "inputSchema": {"type": "object", "properties": {"selector": {"type": "string"}, "all": {"type": "boolean", "default": False}, **TAB_PROPS}, "required": ["selector"]}},
    {"name": "browser_gethtml", "description": "Получить HTML элемента.", "inputSchema": {"type": "object", "properties": {"selector": {"type": "string"}, "outer": {"type": "boolean", "default": False}, **TAB_PROPS}, "required": ["selector"]}},
    {"name": "browser_getvalue", "description": "Получить value поля ввода.", "inputSchema": {"type": "object", "properties": {"selector": {"type": "string"}, **TAB_PROPS}, "required": ["selector"]}},
    {"name": "browser_wait", "description": "Дождаться появления элемента.", "inputSchema": {"type": "object", "properties": {"selector": {"type": "string"}, "timeout_ms": {"type": "integer", "default": 10000}, **TAB_PROPS}, "required": ["selector"]}},
    {"name": "browser_fill_form", "description": "Заполнить несколько полей формы разом.", "inputSchema": {"type": "object", "properties": {"fields": {"type": "object"}, "submit_selector": {"type": "string"}, **TAB_PROPS}, "required": ["fields"]}},
    {"name": "browser_login", "description": "Авторизоваться на сайте и вернуть cookies после логина.", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "username_selector": {"type": "string"}, "password_selector": {"type": "string"}, "submit_selector": {"type": "string"}, "username": {"type": "string"}, "password": {"type": "string"}, "extra_fields": {"type": "object"}, "redact": {"type": "boolean", "default": False}, "tab_id": TAB_PROPS["tab_id"]}, "required": ["url", "username_selector", "password_selector", "submit_selector", "username", "password"]}},
    {"name": "browser_tabs", "description": "Получить список всех открытых targets/вкладок.", "inputSchema": {"type": "object", "properties": {"include_non_page": {"type": "boolean", "default": False}}}},
    {"name": "browser_newtab", "description": "Открыть новую вкладку с указанным URL.", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "wait_until_ready": {"type": "boolean", "default": True}}, "required": ["url"]}},
    {"name": "browser_closetab", "description": "Закрыть вкладку по ID или URL-фильтру.", "inputSchema": {"type": "object", "properties": {"tab_id": {"type": "string"}, "url_filter": {"type": "string"}}}},
    {"name": "browser_scroll", "description": "Прокрутить страницу.", "inputSchema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]}, "selector": {"type": "string"}, "amount": {"type": "integer", "default": 500}, **TAB_PROPS}}},
    {"name": "browser_pdf", "description": "Сохранить текущую страницу как PDF base64.", "inputSchema": {"type": "object", "properties": {**TAB_PROPS}}},
    {"name": "browser_health", "description": "Проверить CDP/браузер, версию MCP, вкладки и доступность автозапуска CDP.", "inputSchema": {"type": "object", "properties": {"autostart": {"type": "boolean", "default": True}}}},
    {"name": "browser_page_summary", "description": "Краткая структурная сводка страницы: title/url/text snippet/forms/links/buttons/inputs.", "inputSchema": {"type": "object", "properties": {"max_text": {"type": "integer", "default": 4000}, "max_items": {"type": "integer", "default": 30}, **TAB_PROPS}}},
    {"name": "browser_select", "description": "Выбрать option в select по value/label/index.", "inputSchema": {"type": "object", "properties": {"selector": {"type": "string"}, "value": {"type": "string"}, "label": {"type": "string"}, "index": {"type": "integer"}, **TAB_PROPS}, "required": ["selector"]}},
    {"name": "browser_elements", "description": "Найти видимые/интерактивные элементы и вернуть устойчивые CSS-селекторы, текст, роли и координаты.", "inputSchema": {"type": "object", "properties": {"selector": {"type": "string", "description": "CSS selector кандидатов; по умолчанию интерактивные элементы"}, "query": {"type": "string", "description": "Фильтр по тексту/placeholder/name/id"}, "kind": {"type": "string", "enum": ["all", "clickable", "input", "link", "button", "select"]}, "max_items": {"type": "integer", "default": 80}, "include_hidden": {"type": "boolean", "default": False}, **TAB_PROPS}}},
    {"name": "browser_click_text", "description": "Кликнуть по видимому элементу, найденному по тексту/aria-label/value/title без ручного CSS-селектора.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "selector": {"type": "string", "description": "CSS selector кандидатов"}, "exact": {"type": "boolean", "default": False}, "case_sensitive": {"type": "boolean", "default": False}, "index": {"type": "integer", "default": 0}, "wait_after_ms": {"type": "integer", "default": 300}, **TAB_PROPS}, "required": ["text"]}},
    {"name": "browser_wait_text", "description": "Дождаться появления текста внутри body или указанного CSS-селектора.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "selector": {"type": "string", "default": "body"}, "exact": {"type": "boolean", "default": False}, "case_sensitive": {"type": "boolean", "default": False}, "timeout_ms": {"type": "integer", "default": 10000}, **TAB_PROPS}, "required": ["text"]}},
    {"name": "browser_batch", "description": "Выполнить несколько browser_* действий последовательно в одном MCP вызове.", "inputSchema": {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "object", "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["tool"]}}, "stop_on_error": {"type": "boolean", "default": True}}, "required": ["steps"]}},
]

# ====== TOOL HANDLERS ======

def handle_navigate(args):
    url = args["url"]
    tab = _get_tab(args)
    r = _safe_ws(tab, [{"method": "Page.navigate", "params": {"url": url}, "_key": "nav"}], timeout=TIMEOUT)
    if args.get("wait_until_ready", True):
        time.sleep(0.5)
        tab = _get_tab({"tab_id": tab.get("id")}) or tab
        _wait_ready(tab, min(TIMEOUT, 20))
    title = _eval(tab, "document.title", timeout=10).get("value", "")
    final_url = _eval(tab, "window.location.href", timeout=10).get("value", url)
    return {"ok": True, "tab_id": tab.get("id"), "url": final_url, "title": title, "navigation": r.get("nav", {})}


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
            # Fall back to viewport screenshot.
            pass
    r = _safe_ws(tab, [{"method": "Page.captureScreenshot", "params": params, "_key": "shot"}], timeout=TIMEOUT)
    data = r.get("shot", {}).get("data", "")
    return {"ok": True, "tab_id": tab.get("id"), "screenshot_base64": data, "bytes_estimate": len(data) * 3 // 4, "format": "png"}


def handle_cookies(args):
    tab = _get_tab(args)
    filt = args.get("url_filter", "")
    redact = bool(args.get("redact", False))
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
    if redact:
        cookies = [_redact_cookie(c) for c in cookies]
        doc_cookie = f"[redacted len={len(doc_cookie)}]"
    return {
        "ok": True,
        "tab_id": tab.get("id"),
        "cookies": cookies,
        "document_cookie": doc_cookie,
        "url": r.get("url", {}).get("result", {}).get("value", ""),
        "title": r.get("title", {}).get("result", {}).get("value", ""),
        "localStorage": json.loads(r.get("ls", {}).get("result", {}).get("value", "{}") or "{}"),
        "sessionStorage": json.loads(r.get("ss", {}).get("result", {}).get("value", "{}") or "{}"),
    }


def handle_localstorage(args):
    tab = _get_tab(args)
    val = _eval(tab, _storage_json_expr("localStorage", args.get("key", "")), timeout=10).get("value", "{}")
    return {"ok": True, "tab_id": tab.get("id"), "data": json.loads(val or "{}")}


def handle_sessionstorage(args):
    tab = _get_tab(args)
    val = _eval(tab, _storage_json_expr("sessionStorage", args.get("key", "")), timeout=10).get("value", "{}")
    return {"ok": True, "tab_id": tab.get("id"), "data": json.loads(val or "{}")}


def handle_exec(args):
    tab = _get_tab(args)
    expr = args["expression"]
    res = _eval(tab, expr, await_promise=bool(args.get("await_promise", False)), return_by_value=True, timeout=TIMEOUT)
    return {"ok": True, "tab_id": tab.get("id"), "result": res.get("value"), "type": res.get("type", "unknown"), "description": res.get("description")}


def handle_click(args):
    tab = _get_tab(args)
    sel = args["selector"]
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
    sel, text = args["selector"], args["text"]
    clear = bool(args.get("clear_first", True))
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
    sel = args["selector"]
    all_el = bool(args.get("all", False))
    if all_el:
        expr = f"Array.from(document.querySelectorAll({_json_arg(sel)})).map(el => el.innerText || '')"
    else:
        expr = f"(document.querySelector({_json_arg(sel)}) || {{}}).innerText || ''"
    return {"ok": True, "tab_id": tab.get("id"), "text": _eval(tab, expr, timeout=10).get("value", [] if all_el else "")}


def handle_gethtml(args):
    tab = _get_tab(args)
    prop = "outerHTML" if args.get("outer") else "innerHTML"
    expr = f"(document.querySelector({_json_arg(args['selector'])}) || {{}}).{prop} || ''"
    return {"ok": True, "tab_id": tab.get("id"), "html": _eval(tab, expr, timeout=10).get("value", "")}


def handle_getvalue(args):
    tab = _get_tab(args)
    expr = f"(document.querySelector({_json_arg(args['selector'])}) || {{}}).value || ''"
    return {"ok": True, "tab_id": tab.get("id"), "value": _eval(tab, expr, timeout=10).get("value", "")}


def handle_wait(args):
    tab = _get_tab(args)
    sel = args["selector"]
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
    found = bool(_eval(tab, expr, await_promise=True, timeout=max(int(timeout_ms / 1000) + 5, 10)).get("value", False))
    return {"ok": True, "tab_id": tab.get("id"), "found": found, "selector": sel}


def handle_fill_form(args):
    tab = _get_tab(args)
    fields = args["fields"]
    submit = args.get("submit_selector", "")
    results = {}
    for sel, val in fields.items():
        results[sel] = handle_type({"tab_id": tab.get("id"), "selector": sel, "text": str(val), "clear_first": True})
    submitted = False
    if submit:
        results["_submit"] = handle_click({"tab_id": tab.get("id"), "selector": submit})
        submitted = bool(results["_submit"].get("ok"))
    ok = all(v.get("ok") for v in results.values()) if results else True
    return {"ok": ok, "tab_id": tab.get("id"), "fields_filled": len(fields), "submitted": submitted, "details": results}


def handle_login(args):
    nav = handle_navigate({"url": args["url"], "tab_id": args.get("tab_id"), "wait_until_ready": True})
    tab_id = nav.get("tab_id")
    w = handle_wait({"tab_id": tab_id, "selector": args["username_selector"], "timeout_ms": 15000})
    if not w.get("found"):
        return {"ok": False, "tab_id": tab_id, "error": "username selector not found", "url": nav.get("url")}
    fields = {args["username_selector"]: args["username"], args["password_selector"]: args["password"]}
    if args.get("extra_fields"):
        fields.update(args["extra_fields"])
    filled = handle_fill_form({"tab_id": tab_id, "fields": fields, "submit_selector": args["submit_selector"]})
    time.sleep(3)
    cookies = handle_cookies({"tab_id": tab_id, "redact": bool(args.get("redact", False))})
    return {"ok": bool(filled.get("ok")), "tab_id": tab_id, "login_completed": bool(filled.get("ok")), "cookies": cookies.get("cookies", []), "url": cookies.get("url", "")}


def handle_tabs(args):
    tabs = _http("GET", "/json") or []
    include_non_page = bool(args.get("include_non_page", False))
    selected = tabs if include_non_page else [t for t in tabs if t.get("type") == "page"]
    return {"ok": True, "tabs": [{"id": t.get("id"), "url": t.get("url"), "title": t.get("title"), "type": t.get("type"), "webSocketDebuggerUrl": t.get("webSocketDebuggerUrl")} for t in selected], "count": len(selected), "total_targets": len(tabs)}


def handle_newtab(args):
    url = args["url"]
    tab = _http("PUT", f"/json/new?{urllib.parse.quote(url, safe='')}")
    if args.get("wait_until_ready", True):
        time.sleep(0.5)
        tab = _get_tab({"tab_id": tab.get("id")}) or tab
        _wait_ready(tab, min(TIMEOUT, 20))
    return {"ok": True, "tab_id": tab.get("id"), "url": tab.get("url"), "title": tab.get("title"), "webSocketDebuggerUrl": tab.get("webSocketDebuggerUrl")}


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
        return {"ok": True, "closed": tab_id, "response": resp.decode("utf-8", errors="replace") if isinstance(resp, bytes) else str(resp)}
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
    return {"ok": True, "tab_id": tab.get("id"), "result": _eval(tab, expr, timeout=10).get("value", "done")}


def handle_pdf(args):
    tab = _get_tab(args)
    r = _safe_ws(tab, [{"method": "Page.printToPDF", "params": {"format": "A4", "printBackground": True}, "_key": "pdf"}], timeout=TIMEOUT)
    data = r.get("pdf", {}).get("data", "")
    return {"ok": True, "tab_id": tab.get("id"), "pdf_base64": data, "bytes_estimate": len(data) * 3 // 4, "format": "pdf"}


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
    return {
        "ok": bool(version),
        "mcp": {"name": "browser-automation-mcp", "version": SERVER_VERSION, "tool_count": len(TOOLS)},
        "cdp": CDP,
        "browser": (version or {}).get("Browser", ""),
        "protocol": (version or {}).get("Protocol-Version", ""),
        "tabs_count": len(tabs),
        "tabs": [{"id": t.get("id"), "url": t.get("url"), "title": t.get("title"), "type": t.get("type")} for t in tabs[:20]],
        "autostart_enabled": AUTOSTART_CDP,
        "start_cmd": CDP_START_CMD,
        "error": err,
    }


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
        title: document.title,
        url: location.href,
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
    # Агенту нужны не просто тексты, а кликабельная карта страницы с устойчивыми CSS-селекторами.
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
          index: i,
          kind: classify(el),
          selector: cssPath(el),
          tag,
          type,
          text: crop(textOf(el), 220),
          id: el.id || '',
          name: el.name || '',
          placeholder: crop(el.placeholder || '', 160),
          value: sensitive ? (el.value ? '[redacted]' : '') : crop(el.value || '', 120),
          role: el.getAttribute('role') || '',
          href: el.href || '',
          visible: visible(el),
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
    # Текстовый клик нужен для живых UI, где CSS-селектор каждый раз меняется.
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
    """ % {
        "selector": _json_arg(selector),
        "text": _json_arg(text),
        "exact": _json_arg(exact),
        "case_sensitive": _json_arg(case_sensitive),
        "index": index,
    }
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
        } catch (e) {
          return resolve({found: false, selector, text: wanted, error: String(e)});
        }
      };
      check();
    })
    """ % {
        "selector": _json_arg(selector),
        "text": _json_arg(text),
        "exact": _json_arg(exact),
        "case_sensitive": _json_arg(case_sensitive),
        "timeout_ms": timeout_ms,
    }
    result = _eval(tab, expr, await_promise=True, timeout=max(int(timeout_ms / 1000) + 5, 10)).get("value") or {}
    return {"ok": True, "tab_id": tab.get("id"), **result}


def handle_batch(args):
    steps = args.get("steps") or []
    stop = bool(args.get("stop_on_error", True))
    results = []
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


HANDLERS = {
    "browser_navigate": handle_navigate,
    "browser_screenshot": handle_screenshot,
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
    "browser_health": handle_health,
    "browser_page_summary": handle_page_summary,
    "browser_select": handle_select,
    "browser_elements": handle_elements,
    "browser_click_text": handle_click_text,
    "browser_wait_text": handle_wait_text,
    "browser_batch": handle_batch,
}

# ====== MCP MAIN ======

def handle_request(msg):
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "browser-automation-mcp", "version": SERVER_VERSION}}})
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        tool_name = msg.get("params", {}).get("name")
        args = msg.get("params", {}).get("arguments", {}) or {}
        try:
            handler = HANDLERS.get(tool_name)
            if not handler:
                raise ValueError(f"Unknown tool: {tool_name}")
            result = handler(args)
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}})
        except Exception as e:
            _log(f"Tool error [{tool_name}]: {e}")
            payload = {"ok": False, "error": f"{tool_name}: {str(e)[:500]}"}
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}], "isError": True}})
    elif method == "notifications/initialized":
        return
    else:
        _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}})


def main():
    _log(f"Browser Automation MCP Server v{SERVER_VERSION} started")
    _log(f"CDP: {CDP}")
    try:
        _ensure_cdp_started("startup")
        version = _http("GET", "/json/version")
        _log(f"Browser: {version.get('Browser', 'unknown')}")
    except Exception as e:
        _log(f"WARNING: CDP not reachable: {e}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle_request(json.loads(line))
        except Exception as e:
            _log(f"Parse error: {e}")


if __name__ == "__main__":
    main()
