"""
M4/M6: Tool Use Agent for SL100 logs and docs.

Examples:
  uv run sl100_agent.py "分析今天 gateway 和 deviceShadow 是否有设备登录异常"
  uv run sl100_agent.py "设备 MQTT 连不上，应该看哪些服务和日志？"
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from sl100_log_core import (
    DEFAULT_LOG_ROOTS,
    extract_rule_facts_from_paths,
    init_anthropic_client,
    list_log_files as core_list_log_files,
    local_diagnosis,
    redact_text,
    search_docs,
)


def list_log_files(root: str = "") -> list[str]:
    """List known SL100 log files."""
    return core_list_log_files(root or None)


def read_log_slice(path: str, start_line: int = 1, line_count: int = 80) -> dict[str, Any]:
    """Read a redacted slice from one log file."""
    lines = Path(path).expanduser().read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(start_line, 1)
    end = min(start + line_count - 1, len(lines))
    selected = "\n".join(lines[start - 1:end])
    return {
        "path": path,
        "start_line": start,
        "end_line": end,
        "content": redact_text(selected),
    }


def search_errors(root: str = "", keyword: str = "", limit: int = 80) -> list[dict[str, Any]]:
    """Search error-like log lines, optionally filtered by keyword."""
    results: list[dict[str, Any]] = []
    for path in list_log_files(root):
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines, start=1):
            lower = line.lower()
            if keyword and keyword.lower() not in lower:
                continue
            if any(token in lower for token in ["error", "fail", "fatal", "panic", "timeout", "invalid", "offline", "disconnect"]):
                results.append({
                    "path": path,
                    "line": index,
                    "content": redact_text(line.strip())[:800],
                })
            if len(results) >= limit:
                return results
    return results


def extract_timeline(paths: list[str], tail_lines: int = 500) -> dict[str, Any]:
    """Extract deterministic timeline facts from selected logs."""
    facts = extract_rule_facts_from_paths(paths, tail_lines=tail_lines)
    return {
        "summary": facts["summary"],
        "risk_level": facts["risk_level"],
        "timeline": facts["timeline"],
        "services": facts["services"],
    }


def analyze_service_log(paths: list[str], tail_lines: int = 800) -> dict[str, Any]:
    """Run local deterministic diagnosis on selected logs."""
    facts = extract_rule_facts_from_paths(paths, tail_lines=tail_lines)
    diagnosis = local_diagnosis(facts)
    return {"facts": facts, "diagnosis": diagnosis}


def go_analyze_log(path: str) -> dict[str, Any]:
    """Call optional Go log analyzer service on localhost:8788."""
    payload = json.dumps({"file_path": str(Path(path).expanduser().resolve())}).encode()
    request = urllib.request.Request(
        "http://localhost:8788/analyze",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def search_sl100_docs(query: str, max_chunks: int = 5) -> list[dict[str, str]]:
    """Search local SL100 docs with lightweight keyword retrieval."""
    return search_docs(query, max_chunks=max_chunks)


TOOLS = [
    {
        "name": "list_log_files",
        "description": "列出本地 SL100 日志文件。root 为空时搜索默认日志目录。",
        "input_schema": {
            "type": "object",
            "properties": {"root": {"type": "string", "description": "可选日志根目录"}},
        },
    },
    {
        "name": "read_log_slice",
        "description": "读取某个日志文件的脱敏片段。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": 1},
                "line_count": {"type": "integer", "default": 80},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_errors",
        "description": "搜索 error/fail/fatal/timeout/offline 等异常日志行。",
        "input_schema": {
            "type": "object",
            "properties": {
                "root": {"type": "string"},
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "default": 80},
            },
        },
    },
    {
        "name": "extract_timeline",
        "description": "从指定日志文件提取时间线、错误统计和服务事实。",
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "tail_lines": {"type": "integer", "default": 500},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "analyze_service_log",
        "description": "对指定日志运行本地规则诊断，返回 facts + diagnosis。",
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "tail_lines": {"type": "integer", "default": 800},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "go_analyze_log",
        "description": "调用可选 Go 日志分析服务 localhost:8788，对单个日志做高性能解析。服务未启动时会返回错误。",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search_sl100_docs",
        "description": "检索 SL100 架构、部署、MQTT/WebSocket 文档。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_chunks": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
]

TOOL_FUNCTIONS = {
    "list_log_files": list_log_files,
    "read_log_slice": read_log_slice,
    "search_errors": search_errors,
    "extract_timeline": extract_timeline,
    "analyze_service_log": analyze_service_log,
    "go_analyze_log": go_analyze_log,
    "search_sl100_docs": search_sl100_docs,
}


def tool_summaries() -> list[dict[str, str]]:
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
        }
        for tool in TOOLS
    ]


def run_agent(user_message: str, max_turns: int = 8, trace_output: str = "") -> None:
    client = init_anthropic_client()
    log_roots = "\n".join(str(path) for path in DEFAULT_LOG_ROOTS)
    system = f"""你是 SL100 IoT 运维诊断 Agent。

你可以调用工具查看本地脱敏日志、提取时间线、搜索文档并诊断问题。
不要要求用户提供线上服务器权限；先基于本地日志和文档判断。
输出中文，必须给出：结论、证据、可能原因、下一步排查动作。

默认日志目录：
{log_roots}

如果用户问架构或排查路径，优先调用 search_sl100_docs。
如果用户问具体异常，优先 list_log_files -> search_errors -> analyze_service_log。
如果用户明确给出日志文件路径，直接调用 analyze_service_log 分析这个路径。
"""
    messages = [{"role": "user", "content": user_message}]
    trace: list[dict[str, Any]] = []

    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text":
                    print(block.text)
            if trace_output:
                Path(trace_output).write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"\n工具调用 trace 已保存到: {trace_output}")
            return

        if response.stop_reason != "tool_use":
            print(f"Claude stopped with reason: {response.stop_reason}")
            return

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "text" and block.text:
                print(block.text)
            if block.type != "tool_use":
                continue

            func = TOOL_FUNCTIONS.get(block.name)
            print(f"[调用工具] {block.name}({json.dumps(block.input, ensure_ascii=False)})")
            if not func:
                result_text = f"未知工具: {block.name}"
            else:
                try:
                    result = func(**block.input)
                    result_text = json.dumps(result, ensure_ascii=False, indent=2)
                except Exception as exc:  # noqa: BLE001 - tool errors should go back to Claude.
                    result_text = f"工具执行出错: {exc}"
            trace.append({
                "turn": turn,
                "tool": block.name,
                "input": block.input,
                "result": result_text,
            })
            print(f"[工具返回] {result_text[:300]}{'...' if len(result_text) > 300 else ''}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

        messages.append({"role": "user", "content": tool_results})

    if trace_output:
        Path(trace_output).write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n工具调用 trace 已保存到: {trace_output}")
    print(f"达到最大轮数 {max_turns}，Agent 停止。")


def main() -> int:
    parser = argparse.ArgumentParser(description="SL100 Tool Use diagnosis Agent.")
    parser.add_argument("message", nargs="?", help="User goal for the Agent.")
    parser.add_argument("--list-tools", action="store_true", help="List available Agent tools without calling Claude.")
    parser.add_argument("--max-turns", type=int, default=8, help="Maximum Claude/tool loop turns.")
    parser.add_argument("--trace-output", help="Optional path to save tool call trace JSON.")
    args = parser.parse_args()
    if args.list_tools:
        print(json.dumps(tool_summaries(), ensure_ascii=False, indent=2))
        return 0
    if not args.message:
        parser.error("message is required unless --list-tools is used")
    run_agent(args.message, max_turns=args.max_turns, trace_output=args.trace_output or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
