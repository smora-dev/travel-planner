"""Muestra el primer resultado crudo de cada fuente de actividades (diagnóstico)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collector as c
for url, tool, args in [("https://piqod.it/mcp", "get_destination_guide", {"place": "Orlando", "limit": 2}),
                        ("https://piqod.it/mcp", "search_experiences", {"query": "things to do", "location": "Orlando", "limit": 2}),
                        ("https://mcp.tickadoo.com/mcp", "get_city_highlights", {"city": "orlando", "format": "json"}),
                        ("https://mcp.tripways.com/mcp", "search_tours", {"query": "best things to do in Orlando", "destination": "Orlando"})]:
    print("=====", url, tool)
    try:
        print(json.dumps(c.mcp_call(url, tool, args), ensure_ascii=False)[:1500])
    except Exception as e:
        print("ERR", e)
