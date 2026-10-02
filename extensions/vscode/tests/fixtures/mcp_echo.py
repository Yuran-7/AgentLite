"""A local stdio MCP server used by real-core integration tests; no network or model API."""
from __future__ import annotations

import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "echo", "version": "1"}}
    elif request["method"] == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo test text",
                             "inputSchema": {"type": "object", "properties": {
                                 "text": {"type": "string"}}, "required": ["text"]}}]}
    else:
        result = {"content": [{"type": "text",
                               "text": request["params"]["arguments"]["text"]}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
