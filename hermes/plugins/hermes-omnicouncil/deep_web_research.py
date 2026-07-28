"""Deep web research subsystem for hermes-omnicouncil.

Self-contained stdlib crawler/research tool with persistent SQLite source DB,
HTTP cache, progress tracking, multi-engine discovery, source scoring, and
Markdown/HTML/JSON exports. It is intentionally dependency-free so Hermes plugin
startup remains reliable.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import socket
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

VERSION = "1.3.1-ssrf-safe"
BASE_DIR = Path.home() / ".hermes" / "cache" / "hermes-omnicouncil" / "research"
DB_PATH = BASE_DIR / "research.db"
REPORT_DIR = BASE_DIR / "reports"
CACHE_DIR = BASE_DIR / "http-cache"

# ── P0 #5: SSRF protection (pre-flight check before every fetch) ──
_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

def _is_private_host(hostname: str) -> bool:
    """Проверить, резолвится ли hostname в приватный IP. Блокировать если да."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # Не IP — резолвим через DNS
        try:
            addrs = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True  # не можем разрешить → блокируем
        for _, _, _, _, sockaddr in addrs:
            ip_str = sockaddr[0]
            try:
                ip_obj = ipaddress.ip_address(ip_str)
                if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
                    ip_obj = ip_obj.ipv4_mapped
                for net in _PRIVATE_NETS:
                    if ip_obj in net:
                        return True
            except ValueError:
                return True
        return False
    # Прямой IP
    for net in _PRIVATE_NETS:
        if ip in net:
            return True
    return False

def _validate_fetch_url(url: str) -> str:
    """Проверить URL перед fetch. Блокирует private IP, опасные схемы.
    Возвращает нормализованный URL или raise ValueError."""
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"blocked_scheme: {scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("missing_hostname")
    if _is_private_host(hostname):
        raise ValueError(f"private_host_blocked: {hostname}")
    return url
USER_AGENT = "HermesDeepWebResearch/1.3 (+https://local.hermes; respects robots.txt)"
MAX_FETCH_BYTES = 2_000_000
DOMAIN_LAST_FETCH: dict[str, float] = {}
ROBOTS_CACHE: dict[str, tuple[float, urllib.robotparser.RobotFileParser]] = {}
DEFAULT_RATE_LIMIT_SECONDS = 1.0

PRESETS = {
    "fast": {"max_pages": 8, "max_depth": 1, "timeout": 8, "external_link_budget": 2},
    "balanced": {"max_pages": 25, "max_depth": 2, "timeout": 12, "external_link_budget": 6},
    "deep": {"max_pages": 80, "max_depth": 3, "timeout": 18, "external_link_budget": 16},
    "max": {"max_pages": 200, "max_depth": 4, "timeout": 25, "external_link_budget": 40},
}

SEARCH_ENGINES = ["duckduckgo", "bing", "wikipedia", "google_news"]

SCHEMA = {
    "name": "deep_web_crawl",
    "description": (
        "Professional deep web research crawler: accepts a query and/or seed URLs, performs "
        "multi-engine discovery, crawls internal/external links with persistent SQLite source DB, "
        "HTTP cache, progress events, relevance/credibility scoring, and exports Markdown/HTML/JSON "
        "research reports with methodology, ranked findings, citations and limitations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Research query / investigation question."},
            "urls": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Seed URLs."},
            "preset": {"type": "string", "enum": list(PRESETS), "default": "balanced"},
            "max_pages": {"type": "integer", "default": 0, "description": "Override preset max pages when >0."},
            "max_depth": {"type": "integer", "default": -1, "description": "Override preset max depth when >=0."},
            "search": {"type": "boolean", "default": True, "description": "Discover seed URLs when possible."},
            "search_engines": {"type": "array", "items": {"type": "string", "enum": SEARCH_ENGINES}, "default": SEARCH_ENGINES, "description": "Discovery backends to try; all are keyless and failure-tolerant."},
            "follow_external": {"type": "boolean", "default": True},
            "external_link_budget": {"type": "integer", "default": -1},
            "same_domain_only": {"type": "boolean", "default": False},
            "cache_ttl_seconds": {"type": "integer", "default": 86400},
            "force_refresh": {"type": "boolean", "default": False},
            "respect_robots": {"type": "boolean", "default": True, "description": "Check robots.txt before fetching non-cache pages."},
            "rate_limit_seconds": {"type": "number", "default": 1.0, "description": "Minimum delay between live fetches to the same domain."},
            "timeout": {"type": "integer", "default": 0},
            "export_formats": {"type": "array", "items": {"type": "string", "enum": ["markdown", "html", "json"]}, "default": ["markdown", "html"]},
            "include_raw": {"type": "boolean", "default": False, "description": "Include crawled page excerpts in tool response JSON."},
            "export_raw": {"type": "boolean", "default": False, "description": "Include full page text in exported JSON report. Default false."},
            "allow_private_urls": {"type": "boolean", "default": False, "description": "Allow loopback/private/link-local URLs. Default false to prevent local-network crawling."},
            "allowed_domains": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Optional allowlist of domains/hosts."},
            "blocked_domains": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Optional blocklist of domains/hosts."},
            "memory_wiki_task_capsule": {"type": "boolean", "default": False, "description": "Return a sanitized optional memory-wiki task capsule payload without raw page text."},
            "job_id": {"type": "string", "default": "", "description": "Resume/update an existing job id; empty creates deterministic id from query+urls+preset."},
        },
        "required": ["query"],
    },
}


MANAGEMENT_SCHEMAS = [
    {
        "name": "deep_web_status",
        "handler": "handle_deep_web_status",
        "description": "Return deep_web_crawl job status, progress and recent events.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": ["job_id"]},
    },
    {
        "name": "deep_web_open_report",
        "handler": "handle_deep_web_open_report",
        "description": "Return report file paths for a deep_web_crawl job.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
    {
        "name": "deep_web_query_sources",
        "handler": "handle_deep_web_query_sources",
        "description": "Search collected sources for a job without returning raw page text.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}, "query": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 20}}, "required": ["job_id"]},
    },
    {
        "name": "deep_web_delete_job",
        "handler": "handle_deep_web_delete_job",
        "description": "Delete a deep_web_crawl job rows and optionally generated report files.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}, "delete_files": {"type": "boolean", "default": False}}, "required": ["job_id"]},
    },
    {
        "name": "deep_web_export",
        "handler": "handle_deep_web_export",
        "description": "Re-export an existing deep_web_crawl job to markdown/html/json.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}, "export_formats": {"type": "array", "items": {"type": "string", "enum": ["markdown", "html", "json"]}, "default": ["markdown", "html"]}, "export_raw": {"type": "boolean", "default": False}}, "required": ["job_id"]},
    },
    {
        "name": "deep_web_resume",
        "handler": "handle_deep_web_resume",
        "description": "Resume/re-run a deep_web_crawl job using saved query/preset and optional overrides.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}, "max_pages": {"type": "integer", "default": 0}, "max_depth": {"type": "integer", "default": -1}, "force_refresh": {"type": "boolean", "default": False}}, "required": ["job_id"]},
    },
]

SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)['\"]?[^\s'\"]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
STOPWORDS = {
    "about", "after", "also", "from", "have", "into", "that", "their", "there", "this", "with",
    "what", "when", "where", "which", "while", "would", "для", "как", "или", "что", "это", "при",
    "про", "над", "под", "после", "через", "если", "the", "and", "for", "are", "was", "were",
}


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _redact(text: str) -> str:
    out = text or ""
    for pat in SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", out)
    return out


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on", "да"}:
            return True
        if v in {"0", "false", "no", "n", "off", "нет"}:
            return False
    return default


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except Exception:
        n = default
    return max(lo, min(hi, n))


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[,\n]", value) if x.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _now() -> float:
    return time.time()


def _slug(text: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip())[:max_len].strip("-._")
    return slug or "research"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _clean_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit((url or "").strip())
        if p.scheme not in {"http", "https"} or not p.netloc:
            return ""
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
        q = [(k, v) for k, v in q if not k.lower().startswith(("utm_", "fbclid", "gclid", "yclid"))]
        return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", urllib.parse.urlencode(q), ""))
    except Exception:
        return ""


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return ""



def _domain_matches(host: str, patterns: list[str]) -> bool:
    host = (host or "").lower().strip(".")
    for pat in patterns or []:
        pat = (pat or "").lower().strip(".")
        if not pat:
            continue
        if host == pat or host.endswith("." + pat):
            return True
    return False


def _host_is_private(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return False
    for info in infos[:8]:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


def _url_allowed_by_policy(url: str, allow_private_urls: bool = False, allowed_domains: list[str] | None = None, blocked_domains: list[str] | None = None) -> tuple[bool, str]:
    clean = _clean_url(url)
    if not clean:
        return False, "invalid_url"
    host = urllib.parse.urlsplit(clean).hostname or ""
    if _domain_matches(host, blocked_domains or []):
        return False, "blocked_domain"
    if allowed_domains and not _domain_matches(host, allowed_domains):
        return False, "not_in_allowed_domains"
    if not allow_private_urls and _host_is_private(host):
        return False, "private_url_blocked"
    return True, "ok"

def _terms(query: str) -> list[str]:
    terms = [t.lower() for t in re.findall(r"[\w\-]{3,}", query or "")]
    return list(dict.fromkeys([t for t in terms if t not in STOPWORDS]))[:24]


def _db() -> sqlite3.Connection:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs(
            job_id TEXT PRIMARY KEY, query TEXT, preset TEXT, status TEXT,
            created_at REAL, updated_at REAL, progress REAL, pages_done INTEGER,
            pages_target INTEGER, markdown_path TEXT, html_path TEXT, json_path TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS sources(
            source_id TEXT PRIMARY KEY, url TEXT UNIQUE, domain TEXT, first_seen REAL,
            last_seen REAL, title TEXT, status INTEGER, content_type TEXT, credibility REAL,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS pages(
            page_id TEXT PRIMARY KEY, job_id TEXT, source_id TEXT, url TEXT,
            depth INTEGER, fetched_at REAL, status INTEGER, title TEXT, text TEXT,
            summary TEXT, links_json TEXT, error TEXT,
            relevance REAL DEFAULT 0.0, credibility REAL DEFAULT 0.0, canonical_url TEXT, content_hash TEXT, citations_json TEXT DEFAULT '[]', word_count INTEGER DEFAULT 0,
            UNIQUE(job_id, url)
        );
        CREATE TABLE IF NOT EXISTS http_cache(
            url_hash TEXT PRIMARY KEY, url TEXT, fetched_at REAL, status INTEGER,
            content_type TEXT, body TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS progress_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, ts REAL, event TEXT,
            detail TEXT, done INTEGER, total INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_pages_job ON pages(job_id);
        CREATE INDEX IF NOT EXISTS idx_pages_relevance ON pages(job_id,relevance);
        CREATE INDEX IF NOT EXISTS idx_sources_domain ON sources(domain);
        """
    )
    for ddl in [
        "ALTER TABLE pages ADD COLUMN relevance REAL DEFAULT 0.0",
        "ALTER TABLE pages ADD COLUMN word_count INTEGER DEFAULT 0",
        "ALTER TABLE pages ADD COLUMN credibility REAL DEFAULT 0.0",
        "ALTER TABLE pages ADD COLUMN canonical_url TEXT",
        "ALTER TABLE pages ADD COLUMN content_hash TEXT",
        "ALTER TABLE pages ADD COLUMN citations_json TEXT DEFAULT '[]'",
    ]:
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass
    return con


class PageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        t = tag.lower()
        if t == "title":
            self._in_title = True
        if t in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip += 1
        if t == "a":
            href = dict(attrs).get("href")
            if href:
                clean = _clean_url(urllib.parse.urljoin(self.base_url, href))
                if clean:
                    self.links.append(clean)

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t == "title":
            self._in_title = False
        if t in {"script", "style", "noscript", "svg", "canvas"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str):
        txt = re.sub(r"\s+", " ", data or "").strip()
        if not txt:
            return
        if self._in_title:
            self.title_parts.append(txt)
        elif not self._skip:
            self.text_parts.append(txt)

    @property
    def title(self) -> str:
        return _redact(" ".join(self.title_parts)[:300])

    @property
    def text(self) -> str:
        return _redact(re.sub(r"\s+", " ", " ".join(self.text_parts)).strip())


def _canonical_url(url: str) -> str:
    """Normalize a URL for source deduplication without changing fetch URL."""
    clean = _clean_url(url)
    if not clean:
        return ""
    p = urllib.parse.urlsplit(clean)
    path = re.sub(r"/index\.(html?|php)$", "/", p.path or "/", flags=re.I)
    if path != "/":
        path = path.rstrip("/")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False) if k.lower() not in {"replytocom", "share"}]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), path or "/", urllib.parse.urlencode(q), ""))


def _content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()[:100_000]
    return _hash(normalized) if normalized else ""


def _is_probably_duplicate(con: sqlite3.Connection, job_id: str, canonical_url: str, content_hash: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM pages WHERE job_id=? AND (canonical_url=? OR (content_hash<>'' AND content_hash=?)) LIMIT 1",
        (job_id, canonical_url, content_hash),
    ).fetchone()
    return bool(row)


def _rate_limit(url: str, seconds: float) -> None:
    if seconds <= 0:
        return
    dom = _domain(url)
    last = DOMAIN_LAST_FETCH.get(dom, 0.0)
    wait = seconds - (_now() - last)
    if wait > 0:
        time.sleep(min(wait, 10.0))
    DOMAIN_LAST_FETCH[dom] = _now()



def _robots_allowed(url: str, timeout: int, cache_ttl_seconds: int, con: sqlite3.Connection, allow_private_urls: bool = False, allowed_domains: list[str] | None = None, blocked_domains: list[str] | None = None) -> tuple[bool, str]:
    clean = _clean_url(url)
    if not clean:
        return False, "invalid_url"
    p = urllib.parse.urlsplit(clean)
    robots_url = urllib.parse.urlunsplit((p.scheme, p.netloc, "/robots.txt", "", ""))
    now = _now()
    cached = ROBOTS_CACHE.get(robots_url)
    if cached and now - cached[0] <= cache_ttl_seconds:
        rp = cached[1]
    else:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            fetched = _fetch(robots_url, max(2, min(timeout, 10)), max(cache_ttl_seconds, 3600), False, con, respect_robots=False, rate_limit_seconds=0, allow_private_urls=allow_private_urls, allowed_domains=allowed_domains, blocked_domains=blocked_domains)
            rp.parse((fetched.get("body") or "").splitlines())
        except Exception:
            rp.parse([])
        ROBOTS_CACHE[robots_url] = (now, rp)
    try:
        allowed = rp.can_fetch(USER_AGENT, clean)
    except Exception:
        allowed = True
    return bool(allowed), robots_url

def _extract_citations(text: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    terms = _terms(query)
    out: list[dict[str, Any]] = []
    sentences = re.split(r"(?<=[.!?])\s+", (text or "")[:50_000])
    for s in sentences[:260]:
        ss = s.strip()
        if len(ss) < 50:
            continue
        score = sum(1 for t in terms if t in ss.lower())
        if score <= 0:
            continue
        out.append({"quote": _redact(ss[:360]), "score": score})
    out.sort(key=lambda x: (int(x["score"]), len(x["quote"])), reverse=True)
    return out[:limit]


def _memory_capsule(job: sqlite3.Row, pages: list[sqlite3.Row], paths: dict[str, str]) -> dict[str, Any]:
    ranked = sorted(pages, key=lambda p: (float(p["relevance"] or 0), float(p["credibility"] or 0)), reverse=True)
    return {
        "intent": f"deep_web_crawl research report for: {job['query']}",
        "topic": "deep_web_crawl",
        "plan": "Bounded crawler with deduplicated sources, credibility scoring, robots/rate-limit safety, cache and markdown/html reports.",
        "files": list(paths.values()),
        "commands": [],
        "errors": [],
        "fixes": [],
        "verification": f"Collected {job['pages_done']}/{job['pages_target']} pages; no raw page text included in capsule.",
        "followups": ["Verify critical claims against primary sources before acting."],
        "sources": [
            {"url": p["url"], "title": p["title"], "relevance": p["relevance"], "credibility": p["credibility"], "summary": (p["summary"] or "")[:500]}
            for p in ranked[:20]
        ],
    }


def _fetch(url: str, timeout: int, cache_ttl_seconds: int, force_refresh: bool, con: sqlite3.Connection, respect_robots: bool = True, rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS, allow_private_urls: bool = False, allowed_domains: list[str] | None = None, blocked_domains: list[str] | None = None) -> dict[str, Any]:
    url = _clean_url(url)
    allowed, reason = _url_allowed_by_policy(url, allow_private_urls=allow_private_urls, allowed_domains=allowed_domains, blocked_domains=blocked_domains)
    if not allowed:
        return {"url": url, "status": 0, "content_type": "", "body": "", "error": reason, "cached": False, "policy_blocked": True}
    uh = _hash(url)
    if not force_refresh and cache_ttl_seconds > 0:
        row = con.execute("SELECT * FROM http_cache WHERE url_hash=?", (uh,)).fetchone()
        if row and _now() - float(row["fetched_at"] or 0) <= cache_ttl_seconds:
            return {"url": url, "status": row["status"], "content_type": row["content_type"], "body": row["body"] or "", "error": row["error"] or "", "cached": True}
    if respect_robots:
        allowed, robots_url = _robots_allowed(url, timeout, cache_ttl_seconds, con, allow_private_urls=allow_private_urls, allowed_domains=allowed_domains, blocked_domains=blocked_domains)
        if not allowed:
            return {"url": url, "status": 0, "content_type": "", "body": "", "error": f"blocked_by_robots: {robots_url}", "cached": False, "robots_blocked": True}
    _rate_limit(url, rate_limit_seconds)
    # ── P0 #5: SSRF-safe HTTP (DNS pinning, peer IP verify, manual redirects) ──
    try:
        from .ssrf_safe_http import safe_http_request
        status, raw_bytes, resp_headers = safe_http_request(
            url,
            timeout=timeout,
            max_body_bytes=MAX_FETCH_BYTES,
            max_redirects=5,
        )
        ctype = resp_headers.get("content-type", "")
        # Determine charset from content-type header
        charset = "utf-8"
        if "charset=" in ctype:
            try:
                charset = ctype.split("charset=")[-1].split(";")[0].strip()
            except Exception:
                pass
        body = raw_bytes.decode(charset, "replace")
        error = ""
    except ImportError as exc:
        # HERMES-SECURITY-INTEGRATION-20260728: fail closed. The legacy urllib path may be enabled only for an
        # explicitly public, operator-approved compatibility run.
        if os.environ.get("OMNICOUNCIL_ALLOW_UNSAFE_URLLIB_FALLBACK", "0").lower() not in {"1", "true", "yes"}:
            status, ctype, body, error = 0, "", "", "ssrf_safe_http_unavailable"
        else:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.8,*/*;q=0.2"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(req, timeout=timeout) as resp:
                    raw = resp.read(MAX_FETCH_BYTES)
                    ctype = resp.headers.get("content-type", "")
                    charset = resp.headers.get_content_charset() or "utf-8"
                    body = raw.decode(charset, "replace")
                    status = getattr(resp, "status", 200)
                    error = "unsafe_urllib_fallback_explicitly_enabled"
            except Exception as fallback_exc:
                status, ctype, body, error = 0, "", "", _redact(str(fallback_exc))[:1000]
    except Exception as exc:
        status, ctype, body, error = 0, "", "", _redact(str(exc))[:1000]
    con.execute(
        "INSERT OR REPLACE INTO http_cache(url_hash,url,fetched_at,status,content_type,body,error) VALUES(?,?,?,?,?,?,?)",
        (uh, url, _now(), status, ctype, body, error),
    )
    con.commit()
    return {"url": url, "status": status, "content_type": ctype, "body": body, "error": error, "cached": False}

def _parse_page(url: str, body: str, content_type: str) -> tuple[str, str, list[str]]:
    if "html" in (content_type or "").lower() or "<html" in body[:1000].lower():
        parser = PageParser(url)
        try:
            parser.feed(body)
        except Exception:
            pass
        return parser.title, parser.text[:80_000], list(dict.fromkeys(parser.links))[:600]
    if "json" in (content_type or "").lower():
        try:
            obj = json.loads(body)
            body = json.dumps(obj, ensure_ascii=False)
        except Exception:
            pass
    text = _redact(re.sub(r"\s+", " ", body).strip())
    return "", text[:80_000], []


def _extract_links_from_html(base_url: str, body: str) -> list[str]:
    urls: list[str] = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', body or ""):
        href = html.unescape(m.group(1))
        if "uddg=" in href:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            href = qs.get("uddg", [href])[0]
        u = _clean_url(urllib.parse.urljoin(base_url, href))
        if not u:
            continue
        d = _domain(u)
        if any(block in d for block in ["duckduckgo.com", "bing.com", "microsoft.com"]):
            continue
        urls.append(u)
    return list(dict.fromkeys(urls))


def _search_duckduckgo(query: str, timeout: int, con: sqlite3.Connection) -> list[str]:
    """Search via ddgs package (primary) or DDG HTML (fallback)."""
    # Primary: ddgs package (works without browser, no CAPTCHA)
    try:
        from ddgs import DDGS
        urls = []
        with DDGS(timeout=max(timeout or 10, 10)) as client:
            for hit in client.text(query, max_results=20):
                u = str(hit.get("href") or hit.get("url") or "")
                if u and u.startswith("http"):
                    urls.append(u)
        if urls:
            return list(dict.fromkeys(urls))[:20]
    except ImportError:
        pass  # ddgs package not available
    except Exception as e:
        if not any(x in str(e).lower() for x in ('timeout', 'timed out', 'connection', 'reset', 'refused')):
            pass  # silent for transient errors
    # Fallback: DDG HTML (may be broken but worth trying)
    for base in ["https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"]:
        url = base + "?" + urllib.parse.urlencode({"q": query})
        body = _fetch(url, timeout, 0, True, con, False).get("body") or ""
        urls = _extract_links_from_html(url, body)
        if urls:
            return list(dict.fromkeys(urls))[:20]
    return []


def _search_bing(query: str, timeout: int, con: sqlite3.Connection) -> list[str]:
    """Search via Bing RSS (primary) or Bing HTML (fallback)."""
    # Bing RSS endpoint still works for basic queries
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en", "format": "rss"})
    body = _fetch(url, timeout, 0, True, con, False).get("body") or ""
    urls = []
    # Extract from RSS <link> tags
    for m in re.finditer(r"<link>(https?://[^<]+)</link>", body):
        u = _clean_url(html.unescape(m.group(1).strip()))
        if u and "bing.com" not in u:
            urls.append(u)
    if not urls:
        # Try HTML cite tags (legacy)
        body_html = _fetch("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en"}), timeout, 0, True, con, False).get("body") or ""
        for m in re.finditer(r'<cite>([^<]+)</cite>', body_html):
            u = _clean_url(html.unescape(m.group(1).strip()))
            if u:
                urls.append(u)
    if not urls:
        urls = _extract_links_from_html(url, body)
    # If Bing returns nothing, try ddgs as fallback
    if not urls:
        try:
            from ddgs import DDGS
            with DDGS(timeout=max(timeout or 10, 10)) as client:
                for hit in client.text(query, max_results=10):
                    u = str(hit.get("href") or hit.get("url") or "")
                    if u and u.startswith("http") and "bing.com" not in u:
                        urls.append(u)
        except Exception:
            pass
    return list(dict.fromkeys(urls))[:20]


def _search_wikipedia(query: str, timeout: int, con: sqlite3.Connection) -> list[str]:
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({"action": "opensearch", "search": query, "limit": 8, "namespace": 0, "format": "json"})
    body = _fetch(url, timeout, 0, True, con, False).get("body") or ""
    try:
        data = json.loads(body)
        if not isinstance(data, list) or len(data) < 4:
            return []
        urls = []
        for u in data[3]:
            clean = _clean_url(str(u))
            if clean:
                urls.append(clean)
        return urls
    except Exception:
        return []



def _search_google_news(query, timeout, con):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    body = _fetch(url, timeout, 0, True, con, False).get("body") or ""
    urls = []
    # Extract source URLs from RSS <source url="..."> attributes (real article URLs)
    for m in re.finditer(r'<source[^>]*url="(https?://[^"]+)"', body):
        u = _clean_url(html.unescape(m.group(1)))
        if u:
            urls.append(u)
    # Fallback: extract article redirect URLs from <link> tags
    if not urls:
        for m in re.finditer(r"<link>(https?://news\.google\.com/rss/articles/[^<]+)</link>", body):
            u = _clean_url(html.unescape(m.group(1)))
            if u:
                urls.append(u)
    # Last fallback
    if not urls:
        urls = _extract_links_from_html(url, body)
    # If Bing returns nothing, try ddgs as fallback
    if not urls:
        try:
            from ddgs import DDGS
            with DDGS(timeout=max(timeout or 10, 10)) as client:
                for hit in client.text(query, max_results=10):
                    u = str(hit.get("href") or hit.get("url") or "")
                    if u and u.startswith("http") and "bing.com" not in u:
                        urls.append(u)
        except Exception:
            pass
    return list(dict.fromkeys(urls))[:20]

def _search_urls(query: str, timeout: int, con: sqlite3.Connection, engines: list[str] | None = None) -> tuple[list[str], dict[str, list[str]]]:
    engines = engines or SEARCH_ENGINES
    found: dict[str, list[str]] = {}
    dispatch = {"duckduckgo": _search_duckduckgo, "bing": _search_bing, "wikipedia": _search_wikipedia, "google_news": _search_google_news}
    for engine in engines:
        if engine not in dispatch:
            continue
        try:
            found[engine] = dispatch[engine](query, timeout, con)
        except Exception:
            found[engine] = []
    merged: list[str] = []
    for engine in engines:
        merged.extend(found.get(engine, []))
    return list(dict.fromkeys(merged))[:50], found


def _summarize(text: str, query: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text[:25_000])
    terms = _terms(query)
    scored = []
    for s in sentences[:180]:
        low = s.lower()
        score = sum(1 for t in terms if t in low)
        if len(s) > 40:
            scored.append((score, min(len(s), 260), s.strip()))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picks = [s for score, _ln, s in scored[:6] if score > 0] or [s for _score, _ln, s in scored[:3]]
    return _redact(" ".join(picks))[:1800]


def _credibility(url: str, status: int, text: str) -> float:
    d = _domain(url)
    score = 0.5
    if url.startswith("https://"):
        score += 0.08
    if status == 200:
        score += 0.1
    if d.endswith(('.gov', '.edu', '.ac.uk', '.mil')) or any(x in d for x in ["wikipedia.org", "arxiv.org", "ietf.org", "who.int", "un.org", "europa.eu", "nasa.gov", "nih.gov", "nature.com", "science.org"]):
        score += 0.2
    if any(x in d for x in ["github.com", "gitlab.com", "docs.python.org", "developer.mozilla.org"]):
        score += 0.08
    if any(x in d for x in ["reddit.com", "x.com", "twitter.com", "facebook.com", "tiktok.com", "pinterest.com"]):
        score -= 0.14
    if any(x in d for x in ["medium.com", "substack.com", "wordpress.com", "blogspot.com"]):
        score -= 0.04
    wc = len(re.findall(r"\w+", text or ""))
    if re.search(r"\b(references|bibliography|doi:|abstract|methodology|white paper|technical report)\b", text or "", re.I):
        score += 0.08
    if wc > 500:
        score += 0.06
    if wc < 80:
        score -= 0.08
    return round(max(0.0, min(1.0, score)), 3)


def _relevance_score(title: str, text: str, url: str, query: str) -> float:
    terms = _terms(query)
    if not terms:
        return 0.0
    hay_title = (title or "").lower()
    hay_url = (url or "").lower()
    hay_text = (text or "")[:40_000].lower()
    hits = 0.0
    for t in terms:
        if t in hay_title:
            hits += 3.0
        if t in hay_url:
            hits += 1.0
        hits += min(hay_text.count(t), 8) * 0.35
    norm = max(6.0, len(terms) * 3.0)
    return round(max(0.0, min(1.0, hits / norm)), 3)


def _event(con: sqlite3.Connection, job_id: str, event: str, detail: str, done: int, total: int) -> None:
    con.execute("INSERT INTO progress_events(job_id,ts,event,detail,done,total) VALUES(?,?,?,?,?,?)", (job_id, _now(), event, _redact(detail)[:1200], done, total))
    progress = (done / total) if total else 0.0
    con.execute("UPDATE jobs SET updated_at=?, progress=?, pages_done=?, pages_target=? WHERE job_id=?", (_now(), progress, done, total, job_id))
    con.commit()


def _progress_bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "[" + "·" * width + "] 0%"
    filled = int(width * min(done, total) / total)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {round(100 * min(done, total) / total)}%"


def _store_page(con: sqlite3.Connection, job_id: str, url: str, depth: int, fetched: dict[str, Any], title: str, text: str, summary: str, links: list[str], query: str) -> tuple[float, float, int, bool]:
    canonical_url = _canonical_url(url) or url
    content_hash = _content_hash(text)
    duplicate = _is_probably_duplicate(con, job_id, canonical_url, content_hash)
    source_id = _hash(canonical_url)
    dom = _domain(url)
    status = fetched.get("status") or 0
    word_count = len(re.findall(r"\w+", text or ""))
    relevance = _relevance_score(title, text, url, query)
    credibility = _credibility(url, int(status), text)
    citations = _extract_citations(text, query)
    con.execute(
        "INSERT INTO sources(source_id,url,domain,first_seen,last_seen,title,status,content_type,credibility,notes) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen,title=excluded.title,status=excluded.status,content_type=excluded.content_type,credibility=excluded.credibility",
        (source_id, canonical_url, dom, _now(), _now(), title, status, fetched.get("content_type") or "", credibility, "canonical source"),
    )
    con.execute(
        "INSERT OR REPLACE INTO pages(page_id,job_id,source_id,url,depth,fetched_at,status,title,text,summary,links_json,error,relevance,credibility,canonical_url,content_hash,citations_json,word_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_hash(job_id + "\n" + canonical_url), job_id, source_id, url, depth, _now(), status, title, text, summary, json.dumps(links, ensure_ascii=False), fetched.get("error") or "", relevance, credibility, canonical_url, content_hash, json.dumps(citations, ensure_ascii=False), word_count),
    )
    con.commit()
    return relevance, credibility, word_count, duplicate

def _render_markdown(job: sqlite3.Row, pages: list[sqlite3.Row], events: list[sqlite3.Row], search_meta: dict[str, Any] | None = None) -> str:
    ranked = sorted(pages, key=lambda p: (float(p["relevance"] or 0), float(p["credibility"] or 0), int(p["word_count"] or 0)), reverse=True)
    source_count = len(pages)
    domains = sorted({_domain(p["url"]) for p in pages if _domain(p["url"])})
    lines = [
        f"# Deep Web Research Report: {job['query']}", "",
        f"- Job: `{job['job_id']}`",
        f"- Preset: `{job['preset']}`",
        f"- Status: `{job['status']}`",
        f"- Pages: {job['pages_done']}/{job['pages_target']} {_progress_bar(job['pages_done'] or 0, job['pages_target'] or 0)}",
        f"- Unique domains: {len(domains)}",
        f"- Updated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(job['updated_at'] or _now()))}", "",
        "## Methodology", "",
        "- Discovery used keyless web endpoints (configured search engines) plus user-provided seed URLs.",
        "- Crawl frontier was bounded by preset page/depth limits, cache TTL, external-link budget, robots.txt checks, per-domain rate limiting and timeouts.",
        "- Each page was normalized, redacted for common secret patterns, scored for query relevance and source credibility, then persisted in SQLite.",
        "- This report is extractive: it cites collected pages and highlights sentences matching the query; verify critical claims against primary sources.", "",
    ]
    if search_meta:
        engines = search_meta.get("engines") or {}
        lines += ["## Discovery", ""]
        for engine, urls in engines.items():
            lines.append(f"- {engine}: {len(urls)} candidate URL(s)")
        lines.append("")
    lines += ["## Executive summary", ""]
    if not pages:
        lines.append("No pages were successfully collected. Check progress events, network access and seed URLs.")
    else:
        for i, p in enumerate(ranked[:12], 1):
            title = p["title"] or p["url"]
            rel = float(p["relevance"] or 0)
            cred = float(p["credibility"] or 0)
            lines.append(f"{i}. **{title}** — relevance `{rel:.2f}` credibility `{cred:.2f}` — {p['summary'] or 'No summary.'} [source]({p['url']})")
    lines += ["", "## Source table", "", "| # | Relevance | Credibility | Words | HTTP | Domain | Source |", "|---:|---:|---:|---:|---:|---|---|"]
    for i, p in enumerate(ranked, 1):
        title = (p["title"] or p["url"]).replace("|", " ")[:120]
        lines.append(f"| {i} | {float(p['relevance'] or 0):.2f} | {float(p['credibility'] or 0):.2f} | {int(p['word_count'] or 0)} | {p['status']} | `{_domain(p['url'])}` | [{title}]({p['url']}) |")
    lines += ["", "## Source notes", ""]
    for i, p in enumerate(ranked, 1):
        lines += [
            f"### {i}. {p['title'] or p['url']}", "",
            f"- URL: {p['url']}",
            f"- Domain: `{_domain(p['url'])}` | Depth: {p['depth']} | HTTP: {p['status']} | Relevance: {float(p['relevance'] or 0):.2f} | Credibility: {float(p['credibility'] or 0):.2f} | Words: {int(p['word_count'] or 0)}", "",
            p["summary"] or "No extractive summary available.", "",
        ]
        try:
            citations = json.loads(p["citations_json"] or "[]")
        except Exception:
            citations = []
        if citations:
            lines += ["**Extracted citations:**", ""]
            for c in citations[:5]:
                lines.append(f"> {str(c.get('quote', '')).replace(chr(10), ' ')}")
            lines.append("")
    lines += ["", "## Limitations", "", "- Dynamic pages, paywalls, robots/network restrictions, CAPTCHAs and search-engine blocking can reduce coverage.", "- Relevance/credibility scores are heuristics, not truth labels.", "- The crawler stores excerpts and cache entries locally; review sensitive research outputs before sharing.", "", "## Progress events", ""]
    for e in events[-80:]:
        lines.append(f"- `{time.strftime('%H:%M:%S', time.gmtime(e['ts']))}` {e['event']}: {e['detail']} ({e['done']}/{e['total']})")
    return "\n".join(lines).strip() + "\n"


def _render_html(markdown: str, job: sqlite3.Row) -> str:
    body_lines = []
    for line in markdown.splitlines():
        esc = html.escape(line)
        esc = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", esc)
        esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
        esc = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', esc)
        if line.startswith("# "):
            body_lines.append(f"<h1>{esc[2:]}</h1>")
        elif line.startswith("## "):
            body_lines.append(f"<h2>{esc[3:]}</h2>")
        elif line.startswith("### "):
            body_lines.append(f"<h3>{esc[4:]}</h3>")
        elif line.startswith("| "):
            body_lines.append(f"<pre class='table'>{esc}</pre>")
        elif line.startswith("- "):
            body_lines.append(f"<li>{esc[2:]}</li>")
        elif re.match(r"\d+\. ", line):
            body_lines.append(f"<p class='ranked'>{esc}</p>")
        elif not line.strip():
            body_lines.append("")
        else:
            body_lines.append(f"<p>{esc}</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(job['query'])}</title>
<style>
body{{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f6f7fb;color:#1f2937}}
main{{max-width:1120px;margin:32px auto;padding:32px;background:white;border-radius:18px;box-shadow:0 12px 40px rgba(15,23,42,.08)}}
h1{{font-size:34px;margin:0 0 16px;color:#0f172a}}h2{{margin-top:34px;border-top:1px solid #e5e7eb;padding-top:22px;color:#111827}}h3{{color:#1d4ed8}}
code{{background:#eef2ff;padding:2px 6px;border-radius:6px}}a{{color:#2563eb;text-decoration:none}}a:hover{{text-decoration:underline}}
li,p{{line-height:1.58}}.ranked{{padding:12px 14px;background:#f8fafc;border-left:4px solid #60a5fa;border-radius:10px}}.table{{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;background:#f8fafc;padding:4px 8px}}
.footer{{margin-top:40px;color:#64748b;font-size:13px}}
</style></head><body><main>{''.join(body_lines)}<div class="footer">Generated by deep_web_crawl {VERSION}</div></main></body></html>"""



def _export(con: sqlite3.Connection, job_id: str, formats: list[str], search_meta: dict[str, Any] | None = None, export_raw: bool = False) -> dict[str, str]:
    job = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    pages = list(con.execute("SELECT * FROM pages WHERE job_id=? ORDER BY relevance DESC, depth ASC, fetched_at ASC", (job_id,)))
    events = list(con.execute("SELECT * FROM progress_events WHERE job_id=? ORDER BY id ASC", (job_id,)))
    stem = f"{_slug(job['query'])}-{job_id[:12]}"
    paths: dict[str, str] = {}
    markdown = _render_markdown(job, pages, events, search_meta=search_meta)
    if "markdown" in formats:
        p = REPORT_DIR / f"{stem}.md"
        p.write_text(markdown, encoding="utf-8")
        paths["markdown"] = str(p)
    if "html" in formats:
        p = REPORT_DIR / f"{stem}.html"
        p.write_text(_render_html(markdown, job), encoding="utf-8")
        paths["html"] = str(p)
    if "json" in formats:
        p = REPORT_DIR / f"{stem}.json"
        page_rows = []
        for row in pages:
            item = dict(row)
            if not export_raw:
                item.pop("text", None)
                item["text_omitted"] = True
            page_rows.append(item)
        p.write_text(_json({"job": dict(job), "pages": page_rows, "events": [dict(x) for x in events], "search_meta": search_meta or {}, "export_raw": export_raw}), encoding="utf-8")
        paths["json"] = str(p)
    con.execute("UPDATE jobs SET markdown_path=?, html_path=?, json_path=? WHERE job_id=?", (paths.get("markdown", ""), paths.get("html", ""), paths.get("json", ""), job_id))
    con.commit()
    return paths

def handler(args=None, **_kw):
    if args is None:
        args = {k: v for k, v in _kw.items() if k not in {"task_id", "ctx"}}
    else:
        args = dict(args or {})
        for k, v in _kw.items():
            if k not in {"task_id", "ctx"} and k not in args:
                args[k] = v
    query = str(args.get("query") or "").strip()
    if not query:
        return _json({"status": "error", "error": "query is required"})
    preset = str(args.get("preset") or "balanced").lower()
    if preset not in PRESETS:
        preset = "balanced"
    cfg = dict(PRESETS[preset])
    max_pages = _int(args.get("max_pages"), cfg["max_pages"], 1, 1000) if int(args.get("max_pages") or 0) > 0 else cfg["max_pages"]
    max_depth = _int(args.get("max_depth"), cfg["max_depth"], 0, 10) if int(args.get("max_depth") or -1) >= 0 else cfg["max_depth"]
    timeout = _int(args.get("timeout"), cfg["timeout"], 2, 60) if int(args.get("timeout") or 0) > 0 else cfg["timeout"]
    external_budget = _int(args.get("external_link_budget"), cfg["external_link_budget"], 0, 500) if int(args.get("external_link_budget") or -1) >= 0 else cfg["external_link_budget"]
    cache_ttl = _int(args.get("cache_ttl_seconds"), 86400, 0, 30 * 86400)
    force_refresh = _bool(args.get("force_refresh"), False)
    respect_robots = _bool(args.get("respect_robots"), True)
    try:
        rate_limit_seconds = max(0.0, min(30.0, float(args.get("rate_limit_seconds", DEFAULT_RATE_LIMIT_SECONDS))))
    except Exception:
        rate_limit_seconds = DEFAULT_RATE_LIMIT_SECONDS
    follow_external = _bool(args.get("follow_external"), True)
    same_domain_only = _bool(args.get("same_domain_only"), False)
    formats = [x for x in _list(args.get("export_formats") or ["markdown", "html"]) if x in {"markdown", "html", "json"}] or ["markdown", "html"]
    include_raw = _bool(args.get("include_raw"), False)
    export_raw = _bool(args.get("export_raw"), False)
    allow_private_urls = _bool(args.get("allow_private_urls"), False)
    allowed_domains = _list(args.get("allowed_domains"))
    blocked_domains = _list(args.get("blocked_domains"))
    seed_urls = [_clean_url(u) for u in _list(args.get("urls"))]
    seed_urls = [u for u in seed_urls if u and _url_allowed_by_policy(u, allow_private_urls=allow_private_urls, allowed_domains=allowed_domains, blocked_domains=blocked_domains)[0]]
    engines = [e for e in _list(args.get("search_engines") or SEARCH_ENGINES) if e in SEARCH_ENGINES] or SEARCH_ENGINES
    job_id = str(args.get("job_id") or "").strip() or _hash(query + "\n" + "\n".join(seed_urls) + f"\n{preset}")[:24]

    con = _db()
    started = _now()
    con.execute(
        "INSERT OR REPLACE INTO jobs(job_id,query,preset,status,created_at,updated_at,progress,pages_done,pages_target,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (job_id, query, preset, "running", started, started, 0.0, 0, max_pages, ""),
    )
    con.commit()
    _event(con, job_id, "start", f"preset={preset} max_pages={max_pages} max_depth={max_depth} engines={','.join(engines)}", 0, max_pages)

    discovered_by_search: list[str] = []
    discovery_by_engine: dict[str, list[str]] = {}
    if _bool(args.get("search"), True):
        try:
            discovered_by_search, discovery_by_engine = _search_urls(query, timeout, con, engines)
            _event(con, job_id, "search", f"discovered {len(discovered_by_search)} URLs via {','.join(engines)}", 0, max_pages)
        except Exception as exc:
            _event(con, job_id, "search_error", str(exc), 0, max_pages)

    queue: list[tuple[str, int, str]] = []
    for u in list(dict.fromkeys(seed_urls + discovered_by_search)):
        cu = _canonical_url(u) or u
        queue.append((cu, 0, _domain(cu)))
    visited: set[str] = set()
    seed_domains = {_domain(u) for u in seed_urls or discovered_by_search if _domain(u)}
    external_added = 0
    pages_done = 0

    while queue and pages_done < max_pages:
        url, depth, origin_domain = queue.pop(0)
        if url in visited or depth > max_depth:
            continue
        visited.add(url)
        fetched = _fetch(url, timeout, cache_ttl, force_refresh, con, respect_robots=respect_robots, rate_limit_seconds=rate_limit_seconds, allow_private_urls=allow_private_urls, allowed_domains=allowed_domains, blocked_domains=blocked_domains)
        title, text, links = _parse_page(url, fetched.get("body") or "", fetched.get("content_type") or "")
        summary = _summarize(text, query) if text else ""
        relevance, credibility, word_count, duplicate = _store_page(con, job_id, url, depth, fetched, title, text, summary, links, query)
        pages_done += 1
        _event(con, job_id, "fetch", f"{fetched.get('status')} rel={relevance:.2f} cred={credibility:.2f} words={word_count} dup={int(duplicate)} cache={int(bool(fetched.get('cached')))} {url} title={title[:120]}", pages_done, max_pages)

        if depth < max_depth and pages_done < max_pages:
            current_domain = _domain(url)
            # Prefer links that mention query terms in URL for better deep-research focus.
            terms = _terms(query)
            links = sorted(links, key=lambda link: sum(1 for t in terms if t in link.lower()), reverse=True)
            for link in links:
                link = _canonical_url(link) or link
                if link in visited:
                    continue
                link_domain = _domain(link)
                is_internal = link_domain == current_domain or link_domain == origin_domain or link_domain in seed_domains
                if same_domain_only and not is_internal:
                    continue
                if not is_internal:
                    if not follow_external or external_added >= external_budget:
                        continue
                    external_added += 1
                queue.append((link, depth + 1, origin_domain or current_domain))

    status = "success" if pages_done >= max_pages else ("partial" if pages_done else "failed")
    con.execute("UPDATE jobs SET status=?, updated_at=?, progress=?, pages_done=?, pages_target=? WHERE job_id=?", (status, _now(), 1.0 if pages_done else 0.0, pages_done, max_pages, job_id))
    con.commit()
    search_meta = {"engines": discovery_by_engine, "seed_urls": seed_urls, "external_added": external_added, "visited": len(visited), "cache_ttl_seconds": cache_ttl, "respect_robots": respect_robots, "rate_limit_seconds": rate_limit_seconds, "allow_private_urls": allow_private_urls, "allowed_domains": allowed_domains, "blocked_domains": blocked_domains}
    paths = _export(con, job_id, formats, search_meta=search_meta, export_raw=export_raw)
    pages = list(con.execute("SELECT p.url,p.title,p.status,p.depth,p.summary,p.text,p.relevance,p.credibility,p.word_count,p.canonical_url,p.content_hash,p.citations_json FROM pages p WHERE p.job_id=? ORDER BY p.relevance DESC, p.credibility DESC, p.depth ASC, p.fetched_at ASC", (job_id,)))
    events = list(con.execute("SELECT event,detail,done,total,ts FROM progress_events WHERE job_id=? ORDER BY id DESC LIMIT 20", (job_id,)))
    result = {
        "status": status,
        "tool": "deep_web_crawl",
        "version": VERSION,
        "job_id": job_id,
        "query": query,
        "preset": preset,
        "pages_done": pages_done,
        "pages_target": max_pages,
        "progress": _progress_bar(pages_done, max_pages),
        "db_path": str(DB_PATH),
        "report_paths": paths,
        "search_engines": engines,
        "discovered_by_search": discovered_by_search[:50],
        "discovery_by_engine": {k: v[:20] for k, v in discovery_by_engine.items()},
        "sources": [
            {"url": p["url"], "canonical_url": p["canonical_url"], "title": p["title"], "status": p["status"], "depth": p["depth"], "relevance": p["relevance"], "credibility": p["credibility"], "word_count": p["word_count"], "summary": p["summary"], "citations": json.loads(p["citations_json"] or "[]"), **({"text_excerpt": (p["text"] or "")[:2000]} if include_raw else {})}
            for p in pages[:100]
        ],
        "recent_progress_events": [dict(e) for e in reversed(events)],
        "seconds": round(_now() - started, 2),
    }
    if _bool(args.get("memory_wiki_task_capsule"), False):
        job = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        result["memory_wiki_task_capsule"] = _memory_capsule(job, pages, paths)
    return _json(result)



def _job_or_error(con: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()


def handle_deep_web_status(args=None, **_kw):
    args = dict(args or {})
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return _json({"status": "error", "error": "job_id is required"})
    limit = _int(args.get("limit"), 20, 1, 200)
    con = _db()
    job = _job_or_error(con, job_id)
    if not job:
        return _json({"status": "error", "error": "job_not_found", "job_id": job_id})
    events = [dict(x) for x in con.execute("SELECT event,detail,done,total,ts FROM progress_events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit))]
    return _json({"status": "success", "job": dict(job), "progress": _progress_bar(job["pages_done"] or 0, job["pages_target"] or 0), "recent_events": list(reversed(events))})


def handle_deep_web_open_report(args=None, **_kw):
    args = dict(args or {})
    job_id = str(args.get("job_id") or "").strip()
    con = _db()
    job = _job_or_error(con, job_id)
    if not job:
        return _json({"status": "error", "error": "job_not_found", "job_id": job_id})
    return _json({"status": "success", "job_id": job_id, "report_paths": {"markdown": job["markdown_path"], "html": job["html_path"], "json": job["json_path"]}})


def handle_deep_web_query_sources(args=None, **_kw):
    args = dict(args or {})
    job_id = str(args.get("job_id") or "").strip()
    query = str(args.get("query") or "").strip().lower()
    limit = _int(args.get("limit"), 20, 1, 200)
    con = _db()
    if not _job_or_error(con, job_id):
        return _json({"status": "error", "error": "job_not_found", "job_id": job_id})
    rows = list(con.execute("SELECT url,title,status,depth,summary,relevance,credibility,word_count,canonical_url,citations_json FROM pages WHERE job_id=? ORDER BY relevance DESC, credibility DESC LIMIT ?", (job_id, max(limit * 5, limit))))
    out = []
    for row in rows:
        hay = f"{row['url']} {row['title']} {row['summary']}".lower()
        if query and query not in hay:
            continue
        item = dict(row)
        try:
            item["citations"] = json.loads(item.pop("citations_json") or "[]")
        except Exception:
            item["citations"] = []
        out.append(item)
        if len(out) >= limit:
            break
    return _json({"status": "success", "job_id": job_id, "count": len(out), "sources": out})


def handle_deep_web_delete_job(args=None, **_kw):
    args = dict(args or {})
    job_id = str(args.get("job_id") or "").strip()
    delete_files = _bool(args.get("delete_files"), False)
    con = _db()
    job = _job_or_error(con, job_id)
    if not job:
        return _json({"status": "error", "error": "job_not_found", "job_id": job_id})
    removed_files = []
    if delete_files:
        for key in ("markdown_path", "html_path", "json_path"):
            path = Path(job[key] or "")
            if path.exists() and path.is_file() and REPORT_DIR in path.parents:
                path.unlink()
                removed_files.append(str(path))
    for table in ("pages", "progress_events", "jobs"):
        con.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
    con.commit()
    return _json({"status": "success", "job_id": job_id, "deleted_files": removed_files})


def handle_deep_web_export(args=None, **_kw):
    args = dict(args or {})
    job_id = str(args.get("job_id") or "").strip()
    formats = [x for x in _list(args.get("export_formats") or ["markdown", "html"]) if x in {"markdown", "html", "json"}] or ["markdown", "html"]
    con = _db()
    if not _job_or_error(con, job_id):
        return _json({"status": "error", "error": "job_not_found", "job_id": job_id})
    paths = _export(con, job_id, formats, export_raw=_bool(args.get("export_raw"), False))
    return _json({"status": "success", "job_id": job_id, "report_paths": paths})


def handle_deep_web_resume(args=None, **_kw):
    args = dict(args or {})
    job_id = str(args.get("job_id") or "").strip()
    con = _db()
    job = _job_or_error(con, job_id)
    if not job:
        return _json({"status": "error", "error": "job_not_found", "job_id": job_id})
    rerun = {"query": job["query"], "preset": job["preset"], "job_id": job_id, "force_refresh": _bool(args.get("force_refresh"), False)}
    if int(args.get("max_pages") or 0) > 0:
        rerun["max_pages"] = int(args.get("max_pages"))
    if int(args.get("max_depth") or -1) >= 0:
        rerun["max_depth"] = int(args.get("max_depth"))
    return handler(rerun)

def register(ctx):
    ctx.register_tool(name="deep_web_crawl", toolset="hermes_omnicouncil", schema=SCHEMA, handler=handler)
