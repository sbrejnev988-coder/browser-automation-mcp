"""Durable browser capsule ingestion overlay for hermes-memory-wiki.

This module preserves the repository's monolithic __init__.py. It wraps the
MemoryWikiProvider lifecycle at runtime and drains the browser bridge safely.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional

OVERLAY_VERSION = "1.0.0"
_LOCK = threading.RLock()
_MAX_EVENT_BYTES = int(os.environ.get("MEMORY_WIKI_BROWSER_EVENT_MAX_BYTES", "1048576"))
_MAX_DRAIN = int(os.environ.get("MEMORY_WIKI_BROWSER_DRAIN_LIMIT", "50"))


def _home(provider: Any) -> Path:
    value = getattr(provider, "home", None) or os.environ.get("HERMES_HOME") or "~/.hermes"
    return Path(value).expanduser().resolve()


def _root(provider: Any) -> Path:
    override = os.environ.get("BROWSER_MEMORY_WIKI_BRIDGE_DIR", "").strip()
    return Path(override).expanduser().resolve() if override else _home(provider) / "memory-wiki" / "browser_bridge"


def _safe_read(path: Path) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("event is not a regular file")
    st = path.stat()
    if st.st_size <= 0 or st.st_size > _MAX_EVENT_BYTES:
        raise ValueError("event size outside allowed range")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags)
    try:
        raw = os.read(fd, _MAX_EVENT_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_EVENT_BYTES:
        raise ValueError("event exceeds maximum size")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("event must be a JSON object")
    return data


def _validate(data: Mapping[str, Any]) -> Dict[str, Any]:
    if data.get("schema") != "hermes.browser_memory_bridge.v1":
        raise ValueError("unsupported browser bridge schema")
    event_id = str(data.get("event_id") or "")
    url = str(data.get("url") or "")
    if not event_id.startswith("browser_") or not url.startswith(("http://", "https://")):
        raise ValueError("invalid event identity or URL")
    summary = str(data.get("summary") or "")[:12000]
    title = str(data.get("title") or "")[:500]
    domain = str(data.get("domain") or "")[:255]
    content_hash = str(data.get("content_hash") or "")
    expected = hashlib.sha256((url + "\n" + title + "\n" + summary).encode("utf-8", errors="replace")).hexdigest()
    if content_hash != expected:
        raise ValueError("browser capsule content hash mismatch")
    return {
        "event_id": event_id,
        "captured_at": int(data.get("captured_at") or 0),
        "url": url[:4096],
        "domain": domain,
        "title": title,
        "summary": summary,
        "artifact_path": str(data.get("artifact_path") or "")[:4096],
        "content_hash": content_hash,
        "source": "browser-automation-mcp",
    }


def _append_index(path: Path, item: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe = {
        "event_id": item["event_id"],
        "captured_at": item["captured_at"],
        "ingested_at": int(time.time()),
        "url": item["url"],
        "domain": item["domain"],
        "title": item["title"],
        "summary": item["summary"][:2000],
        "content_hash": item["content_hash"],
    }
    line = (json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _index_contains_hash(path: Path, content_hash: str) -> bool:
    if not path.exists() or path.is_symlink():
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-5000:]
        for line in reversed(lines):
            try:
                if json.loads(line).get("content_hash") == content_hash:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _claim_text(item: Mapping[str, Any]) -> str:
    title = item.get("title") or "без заголовка"
    summary = str(item.get("summary") or "").strip()
    if not summary:
        summary = "Страница сохранена как проверяемый браузерный источник."
    return (
        f"Browser evidence for {item['url']}: title={title}. "
        f"Summary: {summary[:700]}. content_hash={item['content_hash']}. "
        f"Captured_at={item['captured_at']}."
    )


def drain(provider: Any, limit: Optional[int] = None) -> Dict[str, Any]:
    root = _root(provider)
    inbox = root / "inbox"
    processed = root / "processed"
    rejected = root / "rejected"
    for directory in (inbox, processed, rejected):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    max_items = max(1, min(int(limit or _MAX_DRAIN), 500))
    stats = {"seen": 0, "ingested": 0, "duplicates": 0, "rejected": 0, "errors": []}
    with _LOCK:
        paths = sorted(inbox.glob("*.json"))[:max_items]
        for path in paths:
            stats["seen"] += 1
            try:
                item = _validate(_safe_read(path))
                index_path = root / "index.jsonl"
                if _index_contains_hash(index_path, item["content_hash"]):
                    os.replace(path, processed / (path.name + ".duplicate"))
                    stats["duplicates"] += 1
                    continue
                text = _claim_text(item)
                ingest = getattr(provider, "_ingest_text", None)
                if not callable(ingest):
                    raise RuntimeError("MemoryWikiProvider._ingest_text is unavailable")
                ingest(text, source="memory_wiki_browser_bridge", max_claims=3)
                _append_index(index_path, item)
                target = processed / path.name
                os.replace(path, target)
                stats["ingested"] += 1
            except Exception as exc:
                target = rejected / (path.name + "." + secrets.token_hex(3) + ".rejected")
                try:
                    os.replace(path, target)
                except Exception:
                    try:
                        shutil.copy2(path, target)
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                stats["rejected"] += 1
                stats["errors"].append({"file": path.name, "error": type(exc).__name__})
    return stats


def _wrap_after(method: Any) -> Any:
    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = method(self, *args, **kwargs)
        try:
            drain(self)
        except Exception:
            pass
        return result
    return wrapper


def _wrap_before(method: Any) -> Any:
    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            drain(self)
        except Exception:
            pass
        return method(self, *args, **kwargs)
    return wrapper


def install(namespace: MutableMapping[str, Any], *, strict: bool = True) -> Dict[str, Any]:
    if namespace.get("_HMW_BROWSER_OVERLAY_INSTALLED"):
        return dict(namespace.get("_HMW_BROWSER_OVERLAY_STATUS") or {})
    cls = namespace.get("MemoryWikiProvider")
    if cls is None:
        if strict:
            raise RuntimeError("MemoryWikiProvider class not found")
        return {"version": OVERLAY_VERSION, "installed": False}
    if hasattr(cls, "initialize") and not getattr(cls.initialize, "__browser_bridge_wrapped__", False):
        wrapped = _wrap_after(cls.initialize)
        wrapped.__browser_bridge_wrapped__ = True
        cls.initialize = wrapped
    for name in ("prefetch", "sync_turn"):
        method = getattr(cls, name, None)
        if callable(method) and not getattr(method, "__browser_bridge_wrapped__", False):
            wrapped = _wrap_before(method)
            wrapped.__browser_bridge_wrapped__ = True
            setattr(cls, name, wrapped)
    cls.browser_bridge_drain = drain
    status = {"version": OVERLAY_VERSION, "installed": True, "class": cls.__name__}
    namespace["_HMW_BROWSER_OVERLAY_INSTALLED"] = True
    namespace["_HMW_BROWSER_OVERLAY_STATUS"] = status
    return status
