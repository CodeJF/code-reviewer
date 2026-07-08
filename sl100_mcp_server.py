"""
M7: Minimal MCP stdio server for SL100 diagnosis tools.

It implements the JSON-RPC methods used by MCP clients:
  - initialize
  - tools/list
  - tools/call

Run:
  uv run sl100_mcp_server.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from sl100_log_core import (
    analyze_paths,
    docs_context_for_query,
    extract_log_facts,
    list_log_files,
    read_log_file,
    render_report,
    search_docs,
)


TOOLS = [
    {
        "name": "analyze_logs",
        "description": "Analyze one or more local SL100 log files. Uses local deterministic diagnosis by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "use_ai": {"type": "boolean", "default": False},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "search_sl100_docs",
        "description": "Search SL100 architecture/deployment/MQTT docs.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "max_chunks": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "summarize_incident",
        "description": "Summarize an already extracted incident JSON object into readable Chinese.",
        "inputSchema": {
            "type": "object",
            "properties": {"incident": {"type": "object"}},
            "required": ["incident"],
        },
    },
    {
        "name": "find_service_errors",
        "description": "Find error-like lines from known SL100 logs under an optional root.",
        "inputSchema": {
            "type": "object",
            "properties": {"root": {"type": "string"}, "limit": {"type": "integer", "default": 50}},
        },
    },
]


def text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "analyze_logs":
        paths = arguments.get("paths", [])
        use_ai = bool(arguments.get("use_ai", False))
        result = analyze_paths(paths, use_ai=use_ai)
        return text_result(render_report(result) + "\n\nJSON:\n" + json.dumps(result, ensure_ascii=False, indent=2))

    if name == "search_sl100_docs":
        chunks = search_docs(arguments["query"], max_chunks=int(arguments.get("max_chunks", 5)))
        return text_result(json.dumps(chunks, ensure_ascii=False, indent=2))

    if name == "summarize_incident":
        incident = arguments["incident"]
        lines = [
            f"类型: {incident.get('type', 'unknown')}",
            f"风险: {incident.get('risk_level', 'unknown')}",
            f"服务: {', '.join(incident.get('related_services', []))}",
            "证据:",
        ]
        for evidence in incident.get("evidence", [])[:5]:
            lines.append(f"- {evidence}")
        return text_result("\n".join(lines))

    if name == "find_service_errors":
        root = arguments.get("root") or None
        limit = int(arguments.get("limit", 50))
        events = []
        for path in list_log_files(root):
            facts = extract_log_facts([read_log_file(path)])
            for item in facts.get("timeline", []):
                events.append({"path": path, **item})
                if len(events) >= limit:
                    return text_result(json.dumps(events, ensure_ascii=False, indent=2))
        return text_result(json.dumps(events, ensure_ascii=False, indent=2))

    raise ValueError(f"unknown tool: {name}")


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sl100-diagnosis", "version": "0.1.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = request.get("params", {})
        try:
            result = call_tool(params["name"], params.get("arguments", {}))
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # noqa: BLE001 - report tool failures to MCP client.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    if method and method.startswith("notifications/"):
        return None

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        response = handle(json.loads(line))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

