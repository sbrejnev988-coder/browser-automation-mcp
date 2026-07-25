import pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apply_overlay import patch_browser_server

FIXTURE = '''import subprocess\nimport os\nimport stat\nimport shlex\nimport json\nimport threading\nimport itertools\nimport concurrent.futures\nimport queue\nimport time\nimport hashlib\nfrom typing import Any, Dict, List, Optional, Tuple\nMEMORY_WIKI_TIMEOUT = int(os.environ.get("BROWSER_MEMORY_WIKI_TIMEOUT", "5"))\nNAVIGATION_ALLOWED_HOSTS=set()\nRECONNECT_BASE_DELAY=1\nRECONNECT_MAX_DELAY=2\nTIMEOUT=30\nTOKEN=""\nMETRICS=type("M",(),{"incr":lambda *a,**k:None})()\nclass BrowserError(Exception): pass\nclass W: WebSocketTimeoutException=TimeoutError; WebSocketConnectionClosedException=OSError\nwebsocket=W()\ndef _log(*a,**k): pass\ndef _is_sensitive_key(k): return False\ndef _validate_url(url):\n    hostname="x"\n    if hostname in NAVIGATION_ALLOWED_HOSTS:\n        return True, "allowlisted"\n    return True, "ok"\n_ws_counter=itertools.count(1)\nCDP_DISCONNECTED="CDP_DISCONNECTED"\nclass CDPConnection:\n    def __init__(self): self._closed=threading.Event()\n    def _reader_loop(self):\n        msg={}\n        if msg.get("id") is not None: pass\n        self._reject_all_pending(CDP_DISCONNECTED)\n    def _reject_all_pending(self, reason): pass\nclass CDPConnectionManager:\n    @classmethod\n    def beat(cls):\n        connections=[]\n        dead=[]\n        for tab_id, conn in connections:\n            try: pass\n            except Exception: dead.append((tab_id,conn))\n# ====== MEMORY-WIKI INTEGRATION (P1) ======\ndef _memory_wiki_post(endpoint,payload): return None\ndef _persist_to_wiki(tab_id,page_data): return None\ndef _recall_from_wiki(url_pattern): return None\n# ====== UTILITY HELPERS (unchanged from v1.4) ======\ndef _artifact_path(a,b): return "/tmp/x"\ndef _media_hint(a): return a\ndef _write_b64_artifact(prefix, ext, data_b64): return {}\ndef _write_text_artifact(prefix, ext, text): return {}\ndef _public_tab(t): return t\n# ====== TOOLS ======\nTOOLS=[
 {"name":"browser_exec"},
 {"name":"browser_cdp"},
 {"name":"browser_login", "inputSchema": {"type":"object", "properties": {
   "credential_ref": {"type":"string"},
   "username": {"type":"string", "description":"DEPRECATED: используй credential_ref"},
   "password": {"type":"string", "description":"DEPRECATED: используй credential_ref"},
 }}}
]\ndef dispatch(name,args):\n    if name == "browser_exec": return 1\n    if name == "browser_cdp": return 2\n    if name == "browser_cookie_set": return 3\n'''

def test_patcher_is_idempotent_and_compilable():
    patched = patch_browser_server(FIXTURE)
    compile(patched, "fixture.py", "exec")
    assert patch_browser_server(patched) == patched
    assert "class _MemoryWikiMCPClient" in patched
    assert "HERMES_BROWSER_RUNTIME_GUARDS" in patched
    assert "os.O_NOFOLLOW" in patched


def test_security_invariants_are_present():
    patched = patch_browser_server(FIXTURE)
    assert '"username": {"type":"string", "description":"DEPRECATED' not in patched
    assert '"password": {"type":"string", "description":"DEPRECATED' not in patched
    assert 'allowlisted = hostname in NAVIGATION_ALLOWED_HOSTS' in patched
    assert 'return True, "allowlisted" if allowlisted else "ok"' in patched
    assert 'Browser snapshot [{content_hash[:16]}]' in patched
    assert 'if name == "browser_exec" and not ALLOW_BROWSER_EXEC' in patched
    assert 'if name == "browser_cdp" and not ALLOW_RAW_CDP' in patched
    assert 'if name == "browser_cookie_set"' in patched


def test_reader_does_not_reject_pending_after_normal_dispatch():
    patched = patch_browser_server(FIXTURE)
    reader = patched.split('    def _reader_loop(self):', 1)[1].split('    def _reject_all_pending', 1)[0]
    normal_dispatch = reader.split('            mid = msg.get("id")', 1)[1]
    # Only the final shutdown cleanup may reject pending after normal dispatch.
    assert normal_dispatch.count('_reject_all_pending(CDP_DISCONNECTED)') == 1
    assert normal_dispatch.rstrip().endswith('self._reject_all_pending(CDP_DISCONNECTED)')


def test_partial_overlay_fails_closed():
    from apply_overlay import PatchError
    broken = FIXTURE.replace('import subprocess\n', 'import subprocess\n# HERMES_BROWSER_OVERLAY_20260725\n', 1)
    try:
        patch_browser_server(broken)
    except PatchError as exc:
        assert 'partial overlay detected' in str(exc)
    else:
        raise AssertionError('partial overlay was accepted')
