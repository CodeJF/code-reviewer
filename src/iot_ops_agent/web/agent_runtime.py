"""Bounded, auditable Agent execution built on the existing SL100 tools."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from iot_ops_agent.agent.tools import TOOLS, TOOL_FUNCTIONS
from iot_ops_agent.diagnosis.diagnose import diagnose
from iot_ops_agent.diagnosis.log_core import assert_redacted, init_anthropic_client, redact_text
from iot_ops_agent.diagnosis.planner import plan_query
from iot_ops_agent.web.config import TeamSettings


SAFE_TOOL_NAMES = frozenset({
    "diagnose_sl100_incident",
    "search_sl100_docs",
    "list_es_indices",
    "search_es_logs",
    "count_es_errors",
    "analyze_es_logs",
    "summarize_es_incident",
    "list_remote_log_files",
    "search_remote_log",
    "analyze_remote_service_log",
})
REMOTE_TOOL_NAMES = frozenset({"list_remote_log_files", "search_remote_log", "analyze_remote_service_log"})
TIME_TOOL_NAMES = frozenset({
    "search_es_logs",
    "count_es_errors",
    "analyze_es_logs",
    "summarize_es_incident",
    "search_remote_log",
    "analyze_remote_service_log",
})
PROMPT_VERSION = "controlled-ops-v1"


class AgentPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentBudget:
    max_turns: int = 3
    max_tool_calls: int = 6
    timeout_seconds: int = 120
    max_input_tokens: int = 32000
    max_output_tokens: int = 4096
    max_tool_result_chars: int = 20000

    @classmethod
    def from_settings(cls, settings: TeamSettings) -> "AgentBudget":
        return cls(
            max_turns=settings.agent_max_turns,
            max_tool_calls=settings.agent_max_tool_calls,
            timeout_seconds=settings.agent_timeout_seconds,
            max_input_tokens=settings.agent_max_input_tokens,
            max_output_tokens=settings.agent_max_output_tokens,
            max_tool_result_chars=settings.agent_max_tool_result_chars,
        )


def build_approved_plan(query: str, *, no_remote: bool, settings: TeamSettings) -> dict[str, Any]:
    clean_query = redact_text(query.strip())
    plan = plan_query(clean_query)
    budget = AgentBudget.from_settings(settings)
    approved = {
        **plan,
        "schema_version": "1.0",
        "query": clean_query,
        "allow_remote": not no_remote,
        "allowed_tools": sorted(SAFE_TOOL_NAMES - (REMOTE_TOOL_NAMES if no_remote else set())),
        "budget": asdict(budget),
    }
    digest_payload = json.dumps(approved, ensure_ascii=False, sort_keys=True)
    approved["digest"] = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    return approved


def _bounded_int(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def constrain_tool_arguments(name: str, arguments: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    if name not in SAFE_TOOL_NAMES or name not in set(plan.get("allowed_tools", [])):
        raise AgentPolicyError(f"tool is not allowed by the approved plan: {name}")
    if name in REMOTE_TOOL_NAMES and not plan.get("allow_remote"):
        raise AgentPolicyError("remote logs were not approved")

    constrained = dict(arguments or {})
    services = list(plan.get("chain_services") or plan.get("services") or [])
    if name == "diagnose_sl100_incident":
        return {"query": plan["query"], "size": 80, "no_remote": not plan.get("allow_remote", False)}

    if name == "search_sl100_docs":
        return {
            "query": redact_text(str(constrained.get("query") or plan["query"]))[:300],
            "max_chunks": _bounded_int(constrained.get("max_chunks"), 5, 5),
        }

    if name == "list_es_indices":
        requested_service = str(constrained.get("service") or (services[0] if services else ""))
        if requested_service and requested_service not in services:
            raise AgentPolicyError(f"service is outside the approved plan: {requested_service}")
        return {"date": str(plan.get("date", "")), "service": requested_service}

    requested_service = str(constrained.get("service") or "")
    if not requested_service or requested_service not in services:
        raise AgentPolicyError(f"service is outside the approved plan: {requested_service or '<missing>'}")
    constrained["service"] = requested_service

    if name in TIME_TOOL_NAMES:
        constrained.update({
            "date": str(plan.get("date", "")),
            "from_time": str(plan.get("from_time", "")),
            "to_time": str(plan.get("to_time", "")),
            "around": str(plan.get("around", "")),
            "around_minutes": _bounded_int(plan.get("around_minutes"), 10, 60),
        })
    if "keyword" in constrained:
        constrained["keyword"] = redact_text(str(constrained["keyword"]))[:240]
    if "size" in constrained:
        constrained["size"] = _bounded_int(constrained["size"], 80, 80)
    if "tail_lines" in constrained:
        constrained["tail_lines"] = _bounded_int(constrained["tail_lines"], 800, 2000)
    if "limit" in constrained:
        constrained["limit"] = _bounded_int(constrained["limit"], 80, 80)
    if "logs" in constrained:
        allowed_logs = {"error", "debug", "stderr", "stdout", "sql", "access"}
        constrained["logs"] = [item for item in constrained["logs"] if item in allowed_logs][:2] or ["error"]
    return constrained


def _safe_json(value: Any, limit: int) -> str:
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    safe = redact_text(serialized)
    leaks = assert_redacted(safe)
    if leaks:
        raise AgentPolicyError(f"tool result failed redaction validation: {', '.join(leaks)}")
    if len(safe) <= limit:
        return safe
    preview = safe[:max(0, limit - 96)]
    envelope = json.dumps(
        {"truncated": True, "original_chars": len(safe), "preview": preview},
        ensure_ascii=False,
    )
    while len(envelope) > limit and preview:
        preview = preview[:-(max(1, len(envelope) - limit))]
        envelope = json.dumps(
            {"truncated": True, "original_chars": len(safe), "preview": preview},
            ensure_ascii=False,
        )
    if len(envelope) > limit:
        raise AgentPolicyError("tool result budget is too small for a truncation envelope")
    return envelope


def _evidence_refs(value: Any) -> list[str]:
    refs: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("evidence_id"):
                refs.append(str(item["evidence_id"]))
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return list(dict.fromkeys(refs))[:20]


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise AgentPolicyError("model response did not contain a JSON object")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise AgentPolicyError("model response must be a JSON object")
    return value


def _merge_ai_analysis(report: dict[str, Any], analysis: dict[str, Any], *, model_id: str, prompt_version: str) -> dict[str, Any]:
    valid_refs = {str(item.get("evidence_id")) for item in report.get("evidence", []) if item.get("evidence_id")}
    requested_refs = [str(item) for item in analysis.get("evidence_refs", [])]
    if any(item not in valid_refs for item in requested_refs):
        raise AgentPolicyError("model referenced evidence outside the deterministic report")
    summary = redact_text(str(analysis.get("summary", "")))[:1200]
    root_cause = redact_text(str(analysis.get("root_cause", "")))[:1200]
    actions = [redact_text(str(item))[:500] for item in analysis.get("next_actions", []) if str(item).strip()][:8]
    report["ai_analysis"] = {
        "summary": summary,
        "root_cause": root_cause,
        "next_actions": actions,
        "evidence_refs": requested_refs,
        "model_id": model_id,
        "prompt_version": prompt_version,
    }
    if requested_refs and root_cause:
        report["root_cause"] = root_cause
    if actions:
        report["next_actions"] = actions
    return report


def run_controlled_agent(
    query: str,
    *,
    plan: dict[str, Any],
    settings: TeamSettings,
    client: Any | None = None,
    tool_functions: dict[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Execute the existing tools under an approved plan and return a safe report plus trace."""
    budget = AgentBudget.from_settings(settings)
    tool_functions = tool_functions or TOOL_FUNCTIONS
    client = client or init_anthropic_client()
    allowed_tools = [tool for tool in TOOLS if tool["name"] in set(plan.get("allowed_tools", []))]
    started = time.monotonic()
    trace: list[dict[str, Any]] = []
    input_tokens = output_tokens = 0
    report: dict[str, Any] | None = None
    final_text = ""
    system = f"""You are a bounded IoT operations diagnosis Agent.
The approved plan below is authoritative. Use only the supplied read-only tools.
Never expand services, time windows, remote access, or tool count beyond the plan.
Call diagnose_sl100_incident first so the deterministic evidence report remains the source of truth.
Your final response must be one JSON object with keys summary, root_cause, next_actions, evidence_refs.
Every evidence_refs value must be an evidence_id returned by tools. If evidence is insufficient, use an empty list and say so.
Approved plan: {json.dumps(plan, ensure_ascii=False, sort_keys=True)}
"""
    messages: list[dict[str, Any]] = [{"role": "user", "content": redact_text(query)}]

    for turn in range(1, budget.max_turns + 1):
        if time.monotonic() - started > budget.timeout_seconds:
            raise AgentPolicyError("agent wall-clock budget exceeded")
        remaining_output_tokens = budget.max_output_tokens - output_tokens
        if remaining_output_tokens <= 0:
            raise AgentPolicyError("agent output-token budget exhausted")
        response = client.messages.create(
            model=settings.agent_model_id,
            max_tokens=min(2048, remaining_output_tokens),
            system=system,
            tools=allowed_tools,
            messages=messages,
            timeout=max(1, budget.timeout_seconds - (time.monotonic() - started)),
        )
        usage = getattr(response, "usage", None)
        input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        if input_tokens > budget.max_input_tokens or output_tokens > budget.max_output_tokens:
            raise AgentPolicyError("agent token budget exceeded")
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        turn_text = ""
        for block in response.content:
            if getattr(block, "type", "") == "text" and getattr(block, "text", ""):
                turn_text += block.text
            if getattr(block, "type", "") != "tool_use":
                continue
            if len(trace) >= budget.max_tool_calls:
                raise AgentPolicyError("agent tool-call budget exceeded")
            name = str(block.name)
            call_started = time.monotonic()
            status = "completed"
            error = ""
            value: Any = {}
            try:
                arguments = constrain_tool_arguments(name, dict(block.input or {}), plan)
                function = tool_functions.get(name)
                if not function:
                    raise AgentPolicyError(f"tool implementation is missing: {name}")
                value = function(**arguments)
                if name == "diagnose_sl100_incident" and isinstance(value, dict):
                    report = value
                result_text = _safe_json(value, budget.max_tool_result_chars)
            except Exception as exc:
                status = "blocked" if isinstance(exc, AgentPolicyError) else "failed"
                error = redact_text(str(exc))[:500]
                arguments = redact_text(str(getattr(block, "input", {})))
                result_text = json.dumps({"error": error, "status": status}, ensure_ascii=False)
            trace.append({
                "sequence": len(trace) + 1,
                "turn": turn,
                "tool_name": name,
                "arguments": arguments if isinstance(arguments, dict) else {"summary": arguments},
                "evidence_refs": _evidence_refs(value),
                "status": status,
                "duration_ms": int((time.monotonic() - call_started) * 1000),
                "error": error,
            })
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})
            if time.monotonic() - started > budget.timeout_seconds:
                raise AgentPolicyError("agent wall-clock budget exceeded")

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
            continue
        if getattr(response, "stop_reason", "") == "end_turn":
            final_text = turn_text
            break

    if report is None:
        report = diagnose(query, no_remote=not plan.get("allow_remote", False))
    ai_status = "completed"
    try:
        analysis = _extract_json(final_text)
        report = _merge_ai_analysis(
            report,
            analysis,
            model_id=settings.agent_model_id,
            prompt_version=settings.agent_prompt_version or PROMPT_VERSION,
        )
    except Exception as exc:
        ai_status = "fallback"
        report["ai_analysis"] = {
            "status": "fallback",
            "reason": redact_text(str(exc))[:500],
            "model_id": settings.agent_model_id,
            "prompt_version": settings.agent_prompt_version or PROMPT_VERSION,
        }
    duration_ms = int((time.monotonic() - started) * 1000)
    if duration_ms > budget.timeout_seconds * 1000:
        raise AgentPolicyError("agent wall-clock budget exceeded")
    report["agent_execution"] = {
        "mode": "ai_assisted",
        "status": ai_status,
        "plan_digest": plan.get("digest", ""),
        "tool_call_count": len(trace),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
    }
    return {"report": report, "tool_calls": trace, "execution": report["agent_execution"]}
