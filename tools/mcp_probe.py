"""Lista las herramientas de servidores MCP públicos (diagnóstico)."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collector as c, urllib.request
UA = c.UA

def tools_list(url):
    sid = None
    def post(body):
        nonlocal sid
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": UA}
        if sid: h["Mcp-Session-Id"] = sid
        r = urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), h), timeout=60)
        sid = r.headers.get("Mcp-Session-Id") or sid
        raw = r.read().decode()
        if not raw.strip(): return None
        if raw.lstrip().startswith("{"): return json.loads(raw)
        d = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
        return json.loads(d[-1]) if d else None
    post({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}})
    post({"jsonrpc":"2.0","method":"notifications/initialized"})
    return post({"jsonrpc":"2.0","id":2,"method":"tools/list"})

for url in sys.argv[1:]:
    print("=====", url)
    try:
        r = tools_list(url)
        for t in (r or {}).get("result", {}).get("tools", []):
            print("TOOL", t["name"], "::", (t.get("description") or "")[:150].replace("\n"," "))
            print("  ARGS", json.dumps(t.get("inputSchema", {}).get("properties", {}))[:600])
    except Exception as e:
        print("ERR", type(e).__name__, str(e)[:200])
