#!/usr/bin/env python3
"""Browser E2E test: Chromium + MCP lifecycle with local HTTP server."""
import subprocess, json, sys, os, select

test_url = os.environ.get("BROWSER_E2E_URL", "http://127.0.0.1:8765/")

proc = subprocess.Popen(
    [sys.executable, 'server.py'],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1
)

def send(msg):
    proc.stdin.write(json.dumps(msg) + '\n')
    proc.stdin.flush()

def recv(timeout=15):
    r, _, _ = select.select([proc.stdout], [], [], timeout)
    if r:
        return json.loads(proc.stdout.readline())
    raise TimeoutError('MCP timeout')

try:
    # Initialize
    send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'ci-test','version':'1.0'}}})
    recv()
    send({'jsonrpc':'2.0','method':'notifications/initialized'})

    # Check tools
    send({'jsonrpc':'2.0','id':2,'method':'tools/list'})
    tools_resp = recv()
    tools = [t['name'] for t in tools_resp['result']['tools']]
    print(f'Tools: {len(tools)}')
    assert 'browser_navigate' in tools, 'Missing browser_navigate'
    assert 'browser_cookie_list' in tools, 'Missing browser_cookie_list'

    # Navigate to test HTTP server
    send({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'browser_navigate','arguments':{'url':test_url}}})
    nav = recv()
    nav_data = json.loads(nav['result']['content'][0]['text'])
    assert nav_data.get('ok'), f'Navigate failed: {nav_data}'
    tab_id = nav_data['tab_id']
    print(f'Navigate OK: {tab_id} -> {nav_data.get("url")}')

    # Set a cookie
    send({'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'browser_cookie_set','arguments':{'name':'test_cookie','value':'hello_world','url':test_url,'sameSite':'Lax'}}})
    cs = json.loads(recv()['result']['content'][0]['text'])
    assert cs.get('ok'), f'Cookie set failed: {cs}'
    print(f'Cookie set OK: {cs["set"]}')

    # List cookies
    send({'jsonrpc':'2.0','id':6,'method':'tools/call','params':{'name':'browser_cookie_list','arguments':{'scope':'current_page','include_values':True,'confirm_sensitive_data':True}}})
    cl = json.loads(recv()['result']['content'][0]['text'])
    assert cl.get('ok'), f'Cookie list failed: {cl}'
    assert cl['count'] >= 1, f'No cookies found'
    test_cookie_found = any(c['name'] == 'test_cookie' for c in cl['cookies'])
    assert test_cookie_found, 'test_cookie not found in list'
    print(f'Cookie list OK: {cl["count"]} cookies, test_cookie found')

    # Delete cookie
    send({'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'browser_cookie_delete','arguments':{'name':'test_cookie','url':test_url}}})
    cd = json.loads(recv()['result']['content'][0]['text'])
    assert cd.get('ok'), f'Cookie delete failed: {cd}'
    assert cd.get('deleted', 0) >= 1, f'Cookie was not deleted: {cd}'
    print(f'Cookie delete OK: deleted={cd["deleted"]}')

    # Verify deletion via list
    send({'jsonrpc':'2.0','id':10,'method':'tools/call','params':{'name':'browser_cookie_list','arguments':{'scope':'current_page','include_values':False}}})
    cl2 = json.loads(recv()['result']['content'][0]['text'])
    after_delete = [c['name'] for c in cl2.get('cookies', [])]
    assert 'test_cookie' not in after_delete, f'Cookie still present after delete: {after_delete}'
    print(f'Cookie verify-deleted OK: {cl2["count"]} remaining, test_cookie absent')

    # Health
    send({'jsonrpc':'2.0','id':8,'method':'tools/call','params':{'name':'browser_health','arguments':{'autostart':False}}})
    h = json.loads(recv()['result']['content'][0]['text'])
    assert h.get('ok'), f'Health failed: {h}'
    print(f'Health OK: cdp={h.get("cdp_reachable")}')

    # Tabs
    send({'jsonrpc':'2.0','id':9,'method':'tools/call','params':{'name':'browser_tabs','arguments':{}}})
    tabs = json.loads(recv()['result']['content'][0]['text'])
    assert tabs.get('ok'), f'Tabs failed: {tabs}'
    print(f'Tabs OK: {tabs["count"]} tabs')

    print('ALL E2E TESTS PASSED')
    sys.exit(0)
except Exception as e:
    print(f'E2E FAILED: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
finally:
    proc.terminate()
