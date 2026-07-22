"""
M4/M6: Tool Use Agent for SL100 logs and docs.

Examples:
  uv run iot-ops agent "分析今天 gateway 和 deviceShadow 是否有设备登录异常"
  uv run iot-ops agent "设备 MQTT 连不上，应该看哪些服务和日志？"
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any

from iot_ops_agent.diagnosis.log_core import (
    DEFAULT_LOG_ROOTS,
    extract_rule_facts_from_paths,
    init_anthropic_client,
    list_log_files as core_list_log_files,
    local_diagnosis,
    redact_text,
    search_docs,
)
from iot_ops_agent.integrations.elasticsearch import (
    DEFAULT_ERROR_QUERY,
    analyze_logs as es_analyze_logs,
    count_logs as es_count_logs,
    list_indices as es_list_indices,
    search_logs as es_search_logs,
)
from iot_ops_agent.integrations.remote import (
    analyze_remote_logs as remote_analyze_logs,
    list_remote_logs as remote_list_logs,
    search_remote_log as remote_search_log,
    tail_remote_log as remote_tail_log,
)
from iot_ops_agent.diagnosis.diagnose import diagnose as product_diagnose


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


def list_es_indices(date: str = "", service: str = "") -> list[dict[str, Any]]:
    """List IoT Elasticsearch api-* indices through the configured gateway."""
    return es_list_indices(date_text=date, service=service)


def search_es_logs(
    service: str,
    keyword: str = "",
    date: str = "",
    from_time: str = "",
    to_time: str = "",
    around: str = "",
    around_minutes: int = 10,
    size: int = 20,
) -> dict[str, Any]:
    """Search redacted logs from the configured Elasticsearch stack."""
    return es_search_logs(
        service=service,
        keyword=keyword,
        date_text=date,
        from_text=from_time,
        to_text=to_time,
        around_text=around,
        around_minutes=around_minutes,
        size=size,
    )


def count_es_errors(
    service: str,
    keyword: str = DEFAULT_ERROR_QUERY,
    date: str = "",
    from_time: str = "",
    to_time: str = "",
    around: str = "",
    around_minutes: int = 10,
) -> dict[str, Any]:
    """Count error-like logs in Elasticsearch for one service and time window."""
    return es_count_logs(
        service=service,
        keyword=keyword,
        date_text=date,
        from_text=from_time,
        to_text=to_time,
        around_text=around,
        around_minutes=around_minutes,
    )


def analyze_es_logs(
    service: str,
    keyword: str = DEFAULT_ERROR_QUERY,
    date: str = "",
    from_time: str = "",
    to_time: str = "",
    around: str = "",
    around_minutes: int = 10,
    size: int = 80,
) -> dict[str, Any]:
    """Search ES logs and run deterministic SL100 incident diagnosis."""
    return es_analyze_logs(
        service=service,
        keyword=keyword,
        date_text=date,
        from_text=from_time,
        to_text=to_time,
        around_text=around,
        around_minutes=around_minutes,
        size=size,
        use_ai=False,
    )


def summarize_es_incident(
    service: str,
    keyword: str = DEFAULT_ERROR_QUERY,
    date: str = "",
    from_time: str = "",
    to_time: str = "",
    around: str = "",
    around_minutes: int = 10,
    size: int = 80,
) -> dict[str, Any]:
    """Return a compact incident summary from Elasticsearch logs."""
    result = analyze_es_logs(
        service=service,
        keyword=keyword,
        date=date,
        from_time=from_time,
        to_time=to_time,
        around=around,
        around_minutes=around_minutes,
        size=size,
    )
    facts = result["facts"]
    diagnosis = result["diagnosis"]
    return {
        "source": facts.get("source", {}),
        "risk_level": diagnosis.get("risk_level"),
        "summary": diagnosis.get("summary"),
        "error_count": facts.get("error_count"),
        "incidents": diagnosis.get("incidents", []),
        "next_steps": diagnosis.get("next_steps", []),
    }


def list_remote_log_files(service: str = "") -> list[dict[str, str]]:
    """List whitelisted remote service log files."""
    return remote_list_logs(service)


def tail_remote_log(service: str, log: str = "error", tail_lines: int = 300) -> dict[str, Any]:
    """Tail one whitelisted remote service log file over SSH."""
    return remote_tail_log(service, log, tail_lines)


def search_remote_log(
    service: str,
    log: str = "error",
    keyword: str = "",
    tail_lines: int = 2000,
    limit: int = 80,
    date: str = "",
    from_time: str = "",
    to_time: str = "",
    around: str = "",
    around_minutes: int = 10,
) -> list[dict[str, Any]]:
    """Search a whitelisted remote log after tailing it over SSH."""
    return remote_search_log(
        service,
        log,
        keyword,
        tail_lines,
        limit,
        date_text=date,
        from_text=from_time,
        to_text=to_time,
        around_text=around,
        around_minutes=around_minutes,
    )


def analyze_remote_service_log(
    service: str,
    logs: list[str] | None = None,
    tail_lines: int = 800,
    date: str = "",
    from_time: str = "",
    to_time: str = "",
    around: str = "",
    around_minutes: int = 10,
) -> dict[str, Any]:
    """Analyze whitelisted remote service file logs with deterministic rules."""
    return remote_analyze_logs(
        service,
        logs=logs or ["error"],
        tail_lines=tail_lines,
        use_ai=False,
        date_text=date,
        from_text=from_time,
        to_text=to_time,
        around_text=around,
        around_minutes=around_minutes,
    )


def diagnose_sl100_incident(query: str, size: int = 80, no_remote: bool = False) -> dict[str, Any]:
    """Run the productized SL100 diagnosis pipeline and return one unified report."""
    return product_diagnose(query, size=size, no_remote=no_remote)


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
    {
        "name": "list_es_indices",
        "description": "列出私有 Elasticsearch 中的 IoT api-* 日志索引。date 格式如 2026-07-09。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|access，可为空"},
            },
        },
    },
    {
        "name": "search_es_logs",
        "description": "从私有 Elasticsearch 查询脱敏日志。适合测试同事给出服务、时间、关键词后定位原始证据。",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|access"},
                "keyword": {"type": "string", "description": "关键词或 simple query string，例如 error、websocket、登录失败"},
                "date": {"type": "string", "description": "Asia/Shanghai 日期，如 2026-07-09"},
                "from_time": {"type": "string", "description": "Asia/Shanghai 开始时间，如 2026-07-09 09:00"},
                "to_time": {"type": "string", "description": "Asia/Shanghai 结束时间，如 2026-07-09 10:00"},
                "around": {"type": "string", "description": "Asia/Shanghai 中心时间，如 2026-07-09 09:20"},
                "around_minutes": {"type": "integer", "default": 10},
                "size": {"type": "integer", "default": 20},
            },
            "required": ["service"],
        },
    },
    {
        "name": "count_es_errors",
        "description": "统计私有 Elasticsearch 中某服务在时间窗口内的错误类日志数量。",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|access"},
                "keyword": {"type": "string", "default": DEFAULT_ERROR_QUERY},
                "date": {"type": "string"},
                "from_time": {"type": "string"},
                "to_time": {"type": "string"},
                "around": {"type": "string"},
                "around_minutes": {"type": "integer", "default": 10},
            },
            "required": ["service"],
        },
    },
    {
        "name": "analyze_es_logs",
        "description": "查询私有 Elasticsearch 日志并运行本地规则诊断，返回 facts + diagnosis。",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|access"},
                "keyword": {"type": "string", "default": DEFAULT_ERROR_QUERY},
                "date": {"type": "string"},
                "from_time": {"type": "string"},
                "to_time": {"type": "string"},
                "around": {"type": "string"},
                "around_minutes": {"type": "integer", "default": 10},
                "size": {"type": "integer", "default": 80},
            },
            "required": ["service"],
        },
    },
    {
        "name": "summarize_es_incident",
        "description": "从 Elasticsearch 日志生成压缩 incident 摘要，适合最终给用户解释排障结论。",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|access"},
                "keyword": {"type": "string", "default": DEFAULT_ERROR_QUERY},
                "date": {"type": "string"},
                "from_time": {"type": "string"},
                "to_time": {"type": "string"},
                "around": {"type": "string"},
                "around_minutes": {"type": "integer", "default": 10},
                "size": {"type": "integer", "default": 80},
            },
            "required": ["service"],
        },
    },
    {
        "name": "list_remote_log_files",
        "description": "列出白名单内的远程服务器文件日志。作为 ES 缺失、延迟或需要 stderr/stdout 时的补充工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|cloudStorage|AdminService|scheduledTask，可为空"},
            },
        },
    },
    {
        "name": "tail_remote_log",
        "description": "通过 SSH 只读 tail 一个白名单远程日志文件，返回脱敏内容。",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|cloudStorage|AdminService|scheduledTask"},
                "log": {"type": "string", "description": "error|debug|stderr|stdout|sql|access", "default": "error"},
                "tail_lines": {"type": "integer", "default": 300},
            },
            "required": ["service"],
        },
    },
    {
        "name": "search_remote_log",
        "description": "通过 SSH tail 白名单远程日志后在本地搜索。keyword 为空时搜索错误类行。",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|cloudStorage|AdminService|scheduledTask"},
                "log": {"type": "string", "default": "error"},
                "keyword": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 2000},
                "limit": {"type": "integer", "default": 80},
                "date": {"type": "string"},
                "from_time": {"type": "string"},
                "to_time": {"type": "string"},
                "around": {"type": "string"},
                "around_minutes": {"type": "integer", "default": 10},
            },
            "required": ["service"],
        },
    },
    {
        "name": "analyze_remote_service_log",
        "description": "分析白名单远程文件日志，返回 facts + diagnosis。用于 ES 无数据或需要 stderr/stdout 补充证据。",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "gateway|deviceShadow|pushService|cloudStorage|AdminService|scheduledTask"},
                "logs": {"type": "array", "items": {"type": "string"}, "default": ["error"]},
                "tail_lines": {"type": "integer", "default": 800},
                "date": {"type": "string"},
                "from_time": {"type": "string"},
                "to_time": {"type": "string"},
                "around": {"type": "string"},
                "around_minutes": {"type": "integer", "default": 10},
            },
            "required": ["service"],
        },
    },
    {
        "name": "diagnose_sl100_incident",
        "description": "产品化一键排障工具：根据自然语言报障生成统一 Incident Report，内部按 ES -> remote fallback 查询。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户原始报障描述"},
                "size": {"type": "integer", "default": 80},
                "no_remote": {"type": "boolean", "default": False},
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
    "list_es_indices": list_es_indices,
    "search_es_logs": search_es_logs,
    "count_es_errors": count_es_errors,
    "analyze_es_logs": analyze_es_logs,
    "summarize_es_incident": summarize_es_incident,
    "list_remote_log_files": list_remote_log_files,
    "tail_remote_log": tail_remote_log,
    "search_remote_log": search_remote_log,
    "analyze_remote_service_log": analyze_remote_service_log,
    "diagnose_sl100_incident": diagnose_sl100_incident,
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

你可以调用工具查询私有 Elasticsearch 日志、查看本地脱敏日志、提取时间线、搜索文档并诊断问题。
不要要求用户在对话中提供线上服务器权限；数据入口由部署私有配置提供，只能只读查询。
输出中文，必须给出：结论、证据、可能原因、下一步排查动作。

默认日志目录：
{log_roots}

真实线上日志查询规则：
- 对常规报障描述，优先调用 diagnose_sl100_incident 生成统一 Incident Report。
- 如果 Incident Report 的 result_status 是 data_unavailable 或 safety_blocked，只能说明数据限制和下一步，不能猜测根因或说“没有问题”。
- 如果 result_status 是 no_evidence，要明确说明当前时间范围内没有证据，不等于业务一定正常。
- 用户提到“线上、服务器、阿里云、真实日志、测试同事报错、今天/昨天/某个时间点”时，优先使用 list_es_indices、count_es_errors、search_es_logs、analyze_es_logs。
- ES 时间输入按 Asia/Shanghai 理解；如果用户说“9:20 左右”，用 around="YYYY-MM-DD 09:20"，around_minutes 默认 10。
- 支持的 ES 服务优先是 gateway、deviceShadow、pushService、access。
- 先 count_es_errors 或 search_es_logs 收敛范围，再 analyze_es_logs / summarize_es_incident 输出结论。
- 所有 ES 查询结果已经脱敏；不要要求读取或保存原始日志。
- 如果 ES 没有目标服务、结果为空、疑似有采集延迟，或用户要求查看 std_err/stdout/sql/access，才使用 list_remote_log_files、tail_remote_log、search_remote_log、analyze_remote_service_log。
- 使用远程文件日志 fallback 时，必须沿用用户给出的 date/from_time/to_time/around 时间窗口；如果没有时间窗口，要明确说明你分析的是尾部最新日志。
- 远程文件日志工具只允许读取白名单路径，不要尝试执行服务器命令或修改服务器。

如果用户问架构或排查路径，优先调用 search_sl100_docs。
如果用户问本地样例或明确给出本地路径，使用 list_log_files -> search_errors -> analyze_service_log。
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
