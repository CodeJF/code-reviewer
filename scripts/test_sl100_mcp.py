"""
Smoke test for the SL100 MCP stdio server.

This does not call Claude. It speaks newline-delimited JSON-RPC to the local
server process and checks initialize, tools/list, and two tools/call paths.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def send(proc: subprocess.Popen[str], message: dict[str, Any]) -> dict[str, Any]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("MCP server exited before responding")
    return json.loads(line)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "iot_ops_agent.agent.mcp_server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        init = send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "local-smoke-test", "version": "0.1.0"},
            },
        })
        require(init["result"]["serverInfo"]["name"] == "sl100-diagnosis", "initialize server name mismatch")

        tools = send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        require("analyze_logs" in tool_names, "analyze_logs missing")
        require("search_sl100_docs" in tool_names, "search_sl100_docs missing")
        require("search_es_logs" in tool_names, "search_es_logs missing")
        require("analyze_es_logs" in tool_names, "analyze_es_logs missing")
        require("summarize_es_incident" in tool_names, "summarize_es_incident missing")
        require("list_remote_log_files" in tool_names, "list_remote_log_files missing")
        require("analyze_remote_service_log" in tool_names, "analyze_remote_service_log missing")

        analyze = send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "analyze_logs",
                "arguments": {
                    "paths": ["samples/sl100_logs/device_login_failed.log"],
                    "format": "json",
                },
            },
        })
        analyze_text = analyze["result"]["content"][0]["text"]
        require("device_login_failed" in analyze_text, "analyze_logs did not detect device_login_failed")

        docs = send(proc, {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "search_sl100_docs",
                "arguments": {
                    "query": "设备 MQTT 连不上应该看哪些服务",
                    "max_chunks": 3,
                },
            },
        })
        docs_text = docs["result"]["content"][0]["text"]
        require("MQTT" in docs_text or "deviceShadow" in docs_text, "search_sl100_docs returned irrelevant content")

        remote_logs = send(proc, {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "list_remote_log_files",
                "arguments": {"service": "gateway"},
            },
        })
        remote_logs_text = remote_logs["result"]["content"][0]["text"]
        require("iot-app-a" in remote_logs_text, "list_remote_log_files did not return configured gateway logs")

        print("MCP smoke test passed")
        return 0
    finally:
        proc.kill()
        proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
