from __future__ import annotations
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod

b = load(ROOT / "overlays" / "browser_security_overlay.py", "browser_overlay_test")
m = load(ROOT / "overlays" / "memory_wiki_browser_overlay.py", "memory_overlay_test")

class BrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("BROWSER_") or key.startswith("HERMES_HOME"):
                os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.old)

    def test_cookie_values_fail_closed(self):
        with self.assertRaises(PermissionError):
            b.preflight("browser_cookie_list", {"include_values": True, "confirm_sensitive_data": True, "domain_filter": "example.com"})

    def test_cookie_values_allowlisted(self):
        os.environ["BROWSER_ALLOW_RAW_COOKIE_VALUES"] = "1"
        os.environ["BROWSER_COOKIE_ALLOWED_DOMAINS"] = "example.com"
        b.preflight("browser_cookie_list", {"include_values": True, "confirm_sensitive_data": True, "domain_filter": "sub.example.com"})

    def test_cookie_prefix_validation(self):
        with self.assertRaises(PermissionError):
            b.preflight("browser_cookie_set", {"name": "__Host-sid", "value": "x", "domain": "example.com", "path": "/", "secure": True})
        b.preflight("browser_cookie_set", {"name": "__Host-sid", "value": "x", "url": "https://example.com/", "path": "/", "secure": True})

    def test_sensitive_exec_blocked(self):
        with self.assertRaises(PermissionError):
            b.preflight("browser_exec", {"expression": "document.cookie"})

    def test_batch_cannot_bypass_sensitive_exec(self):
        with self.assertRaises(PermissionError):
            b.preflight("browser_batch", {"steps": [{"tool": "browser_exec", "arguments": {"expression": "document.cookie"}}]})

    def test_har_bodies_fail_closed(self):
        with self.assertRaises(PermissionError):
            b.preflight("browser_network_har", {"include_bodies": True, "confirm_sensitive_data": True})

    def test_nested_mcp_json_is_redacted(self):
        result = {"content": [{"type": "text", "text": json.dumps({"cookies": [{"name": "sid", "value": "SECRET"}]})}]}
        cleaned = b._postprocess("browser_cookie_list", {}, result)
        self.assertNotIn("SECRET", json.dumps(cleaned))

    def test_plaintext_login_blocked(self):
        with self.assertRaises(PermissionError):
            b.preflight("browser_login", {"username": "u", "password": "p"})

    def test_bridge_never_writes_cookie(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["HERMES_HOME"] = td
            event = b.persist_to_wiki("t1", {"url": "https://example.com", "title": "T", "text": "safe", "cookies": [{"value": "SECRET"}]})
            self.assertTrue(event)
            files = list((Path(td) / "memory-wiki" / "browser_bridge" / "inbox").glob("*.json"))
            raw = files[0].read_text()
            self.assertNotIn("SECRET", raw)
            self.assertNotIn('"cookies"', raw)

class MemoryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.copy()

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.old)

    def test_drain_ingests_and_indexes(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["HERMES_HOME"] = td
            event_id = b.persist_to_wiki("t1", {"url": "https://example.com/a", "title": "A", "text": "Durable evidence about release behavior."})
            class Provider:
                home = Path(td)
                def __init__(self): self.rows=[]
                def _ingest_text(self, text, source, max_claims): self.rows.append((text, source, max_claims))
            p = Provider()
            stats = m.drain(p)
            self.assertEqual(stats["ingested"], 1)
            self.assertEqual(len(p.rows), 1)
            self.assertTrue((Path(td) / "memory-wiki" / "browser_bridge" / "index.jsonl").exists())
            result = b.recall_from_wiki("example.com/a")
            self.assertTrue(result["found"])
            self.assertEqual(result["results"][0]["event_id"], event_id)

if __name__ == "__main__":
    unittest.main()
