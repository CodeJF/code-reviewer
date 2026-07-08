"""M7: MCP stdio server for SL100 diagnosis tools."""
from __future__ import annotations

import json
import sys
from typing import Any

from sl100_log_core import (
    analyze_paths,
    extract_log_facts,
    list_log_files,
    read_log_file,
    render_report,
    search_docs,
)


SERVER_NAME = "sl100-diagnosis"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "analyze_logs",
        "description": "Analyze one or more local SL100 log files. Uses local deterministic diagnosis by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "tail_lines": {"type": "integer", "default": 800},
                "use_ai": {"type": "boolean", "default": False},
                "format": {"type": "string", "enum": ["report", "json", "both"], "default": "both"},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "search_sl100_docs",
        "description": "Search SL100 architecture/deployment/MQTT docs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_chunks": {"type": "integer", "default": 5},
                "format": {"type": "string", "enum": ["context", "json"], "default": "context"},
            },
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
            "properties": {
                "root": {"type": "string"},
                "service": {"type": "string"},
                "keyword": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 800},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
]


def text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def require_paths(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("paths must be a non-empty string array")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError("paths must contain only non-empty strings")
    return value


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "analyze_logs":
        paths = require_paths(arguments.get("paths"))
        tail_lines = int(arguments.get("tail_lines", 800))
        use_ai = bool(arguments.get("use_ai", False))
        output_format = arguments.get("format", "both")
        result = analyze_paths(paths, tail_lines=tail_lines, use_ai=use_ai)
        if output_format == "report":
            return text_result(render_report(result))
        if output_format == "json":
            return text_result(json_text(result))
        return text_result(render_report(result) + "\n\nJSON:\n" + json_text(result))

    if name == "search_sl100_docs":
        query = require_string(arguments.get("query"), "query")
        max_chunks = int(arguments.get("max_chunks", 5))
        output_format = arguments.get("format", "context")
        chunks = search_docs(query, max_chunks=max_chunks)
        if output_format == "json":
            return text_result(json_text({"query": query, "chunks": chunks}))
        context = "\n\n".join(
            f"[{chunk['source']}#{chunk['chunk']} {chunk['title']} score={chunk['score']}]\n{chunk['text']}"
            for chunk in chunks
        )
        return text_result(context or "未检索到相关文档片段。")

    if name == "summarize_incident":
        incident = arguments.get("incident")
        if not isinstance(incident, dict):
            raise ValueError("incident must be an object")
        lines = [
            f"类型: {incident.get('type', 'unknown')}",
            f"风险: {incident.get('risk_level', 'unknown')}",
            f"服务: {', '.join(incident.get('related_services', []))}",
            "证据:",
        ]
        for evidence in incident.get("evidence", [])[:5]:
            if isinstance(evidence, dict):
                service = evidence.get("service", "")
                line = evidence.get("line", "")
                message = evidence.get("message", "")
                lines.append(f"- {service}:{line} {message}")
            else:
                lines.append(f"- {evidence}")
        suggestions = incident.get("suggestions") or incident.get("possible_causes") or []
        if suggestions:
            lines.append("建议:")
            lines.extend(f"- {item}" for item in suggestions[:5])
        return text_result("\n".join(lines))

    if name == "find_service_errors":
        root = arguments.get("root") or None
        service_filter = str(arguments.get("service") or "").lower()
        keyword = str(arguments.get("keyword") or "").lower()
        tail_lines = int(arguments.get("tail_lines", 800))
        limit = int(arguments.get("limit", 50))
        events = []
        for path in list_log_files(root):
            if service_filter and service_filter not in path.lower():
                continue
            facts = extract_log_facts([read_log_file(path, tail_lines=tail_lines)])
            for item in facts.get("timeline", []):
                if keyword and keyword not in item.get("message", "").lower():
                    continue
                events.append({"path": path, **item})
                if len(events) >= limit:
                    return text_result(json_text(events))
        return text_result(json_text(events))

    raise ValueError(f"unknown tool: {name}")


def rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")

    if request.get("jsonrpc") != "2.0":
        return rpc_error(request_id, -32600, "invalid request: jsonrpc must be 2.0")

    if method == "initialize":
        params = request.get("params", {})
        protocol_version = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return rpc_result(request_id, {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return rpc_result(request_id, {})

    if method == "tools/list":
        return rpc_result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        params = request.get("params", {})
        try:
            result = call_tool(params["name"], params.get("arguments", {}))
            return rpc_result(request_id, result)
        except Exception as exc:  # noqa: BLE001 - report tool failures to MCP client.
            return rpc_result(request_id, text_result(str(exc), is_error=True))

    if method and method.startswith("notifications/"):
        return None

    return rpc_error(request_id, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps(rpc_error(None, -32700, f"parse error: {exc}"), ensure_ascii=False), flush=True)
            continue
        response = handle(request)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
