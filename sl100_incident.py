"""
Unified incident report model and renderer for SL100 diagnosis outputs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sl100_es import SHANGHAI_TZ


RESULT_STATUS_LABELS = {
    "actionable": "找到可处理的异常",
    "no_evidence": "未找到明确异常",
    "data_unavailable": "日志数据不可用",
    "safety_blocked": "因安全过滤无法分析",
}


def _stable_incident_id(query: str, data_sources: list[dict[str, Any]], time_window: dict[str, Any] | None) -> str:
    payload = json.dumps(
        {"query": query, "data_sources": data_sources, "time_window": time_window},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sl100-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _collect_evidence(diagnosis: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_items = []
    for incident in diagnosis.get("incidents", []):
        for item in incident.get("evidence", [])[:8]:
            if isinstance(item, dict):
                evidence_items.append({
                    "service": item.get("service", ""),
                    "line": item.get("line"),
                    "level": item.get("level", ""),
                    "message": item.get("message", ""),
                })
            else:
                evidence_items.append({"message": str(item)})
    if evidence_items:
        return evidence_items[:12]
    return [
        {
            "service": item.get("service", ""),
            "line": item.get("line"),
            "level": item.get("level", ""),
            "message": item.get("message", ""),
        }
        for item in facts.get("timeline", [])[:12]
        if isinstance(item, dict)
    ]


def _result_status(data_sources: list[dict[str, Any]], evidence: list[dict[str, Any]], facts: dict[str, Any]) -> str:
    if evidence or facts.get("incidents"):
        return "actionable"
    statuses = [source.get("status", "ok") for source in data_sources]
    if statuses and all(status == "safety_blocked" for status in statuses):
        return "safety_blocked"
    if statuses and all(status in {"unavailable", "safety_blocked"} for status in statuses):
        return "data_unavailable"
    return "no_evidence"


def _confidence(facts: dict[str, Any], evidence: list[dict[str, Any]], result_status: str) -> str:
    if result_status in {"data_unavailable", "safety_blocked"}:
        return "unknown"
    if facts.get("error_count", 0) > 0 and evidence:
        return "high"
    if facts.get("timeline") or evidence:
        return "medium"
    return "low"


def build_incident_report(
    *,
    query: str,
    analysis: dict[str, Any],
    plan: dict[str, Any] | None = None,
    redaction_status: str = "passed",
) -> dict[str, Any]:
    facts = analysis.get("facts", {})
    diagnosis = analysis.get("diagnosis", analysis)
    source = facts.get("source") or {}
    time_window = source.get("time_window") or (plan or {}).get("time_window")
    data_sources = [source] if source else []
    services = sorted(facts.get("services", {}).keys()) or (plan or {}).get("services", [])
    evidence = _collect_evidence(diagnosis, facts)
    next_actions = diagnosis.get("next_steps", [])
    root_cause = diagnosis.get("summary", "")
    result_status = _result_status(data_sources, evidence, facts)
    for incident in diagnosis.get("incidents", []):
        causes = incident.get("possible_causes") or incident.get("suggestions") or []
        incident_type = incident.get("type")
        if incident_type and causes:
            root_cause = f"命中 {incident_type}：{causes[0]}"
            break
    if evidence and not diagnosis.get("incidents"):
        root_cause = "命中未分类的异常日志；需要结合请求链路和业务现象确认是否属于本次故障。"
        next_actions = next_actions or [
            "按 message_id、uuid 或请求时间串联上下游服务日志。",
            "人工确认该异常是否与测试反馈的现象和时间一致。",
        ]
    if result_status == "data_unavailable":
        root_cause = "日志数据不可用，暂时不能判断是否存在异常。"
        next_actions = next_actions or ["检查 sl100-93 SSH、ES 索引或远程日志路径后重试。"]
    elif result_status == "safety_blocked":
        root_cause = "命中的日志无法通过安全过滤，已停止展示和分析该批内容。"
        next_actions = next_actions or ["补充本地脱敏规则后重新查询；不要把原始日志发送给模型。"]
    elif not evidence and facts.get("error_count", 0) == 0:
        root_cause = "未命中明确异常；需要扩大时间窗口、调整关键词，或确认日志采集是否覆盖该服务。"
        if not next_actions:
            next_actions = [
                "扩大时间窗口重新查询。",
                "降低关键词限制，只按服务和时间查询。",
                "用远程文件日志 fallback 校验 ES 是否存在采集延迟。",
            ]

    return {
        "incident_id": _stable_incident_id(query, data_sources, time_window),
        "query": query,
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "time_window": time_window,
        "services": services,
        "data_sources": data_sources,
        "evidence": evidence,
        "timeline": facts.get("timeline", [])[:20],
        "root_cause": root_cause,
        "result_status": result_status,
        "confidence": _confidence(facts, evidence, result_status),
        "risk_level": "unknown" if result_status in {"data_unavailable", "safety_blocked"} else diagnosis.get("risk_level", facts.get("risk_level", "unknown")),
        "next_actions": next_actions,
        "redaction_status": (
            "blocked" if result_status == "safety_blocked"
            else "partial" if any(source.get("redaction_dropped_count", 0) for source in data_sources)
            else redaction_status
        ),
        "query_attempts": (plan or {}).get("query_attempts", []),
        "facts_summary": {
            "summary": facts.get("summary", ""),
            "error_count": facts.get("error_count", 0),
            "incidents": [item.get("type") for item in facts.get("incidents", [])],
        },
    }


def render_incident_report(report: dict[str, Any]) -> str:
    lines = [
        "=" * 72,
        "SL100 排障报告",
        "=" * 72,
        f"Incident ID: {report.get('incident_id')}",
        f"分析结果: {RESULT_STATUS_LABELS.get(report.get('result_status'), report.get('result_status'))}",
        f"风险等级: {report.get('risk_level')}",
        f"置信度: {report.get('confidence')}",
        f"脱敏状态: {report.get('redaction_status')}",
        f"问题: {report.get('query')}",
    ]
    window = report.get("time_window") or {}
    if window:
        lines.append(f"时间窗口: {window.get('start_local')} -> {window.get('end_local')} ({window.get('timezone')})")
    services = report.get("services") or []
    if services:
        lines.append(f"服务: {', '.join(services)}")

    query_attempts = report.get("query_attempts") or []
    if query_attempts:
        lines.extend(["", "查询过程:"])
        for attempt in query_attempts:
            window = attempt.get("time_window") or {}
            lines.append(
                f"- {attempt.get('name', 'query')}: {window.get('start_local', '-')} -> "
                f"{window.get('end_local', '-')}，结果={RESULT_STATUS_LABELS.get(attempt.get('result_status'), attempt.get('result_status'))}"
            )

    lines.extend(["", "结论:", f"- {report.get('root_cause', '')}"])

    evidence = report.get("evidence") or []
    lines.append("")
    lines.append("证据:")
    if not evidence:
        lines.append("- 未命中可直接引用的异常日志。")
    for item in evidence[:8]:
        service = item.get("service", "")
        line = item.get("line", "")
        message = item.get("message", "")
        prefix = f"{service}:{line}" if service or line else "log"
        lines.append(f"- {prefix} {message}")

    timeline = report.get("timeline") or []
    if timeline:
        lines.append("")
        lines.append("时间线:")
        for item in timeline[:8]:
            timestamp = item.get("timestamp") or "-"
            service = item.get("service") or "-"
            level = item.get("level") or "-"
            message = item.get("message", "")
            lines.append(f"- {timestamp} [{service}/{level}] {message}")

    next_actions = report.get("next_actions") or []
    if next_actions:
        lines.append("")
        lines.append("下一步:")
        lines.extend(f"- {item}" for item in next_actions[:8])

    data_sources = report.get("data_sources") or []
    if data_sources:
        lines.append("")
        lines.append("数据源:")
        for source in data_sources:
            source_type = source.get("type", "unknown")
            status = source.get("status", "ok")
            if source_type == "elasticsearch":
                lines.append(
                    f"- ES [{status}] {source.get('index_pattern')} returned={source.get('returned')} "
                    f"total={source.get('total')} dropped={source.get('redaction_dropped_count', 0)}"
                )
            elif source_type == "remote_file":
                refs = source.get("refs", [])
                names = ", ".join(f"{ref.get('host')}:{ref.get('path')}" for ref in refs[:4])
                lines.append(f"- remote_file [{status}] {names}")
            else:
                lines.append(f"- {source_type} [{status}]")

    return "\n".join(lines)


def combine_incident_reports(query: str, reports: list[dict[str, Any]], plan: dict[str, Any] | None = None) -> dict[str, Any]:
    if not reports:
        return build_incident_report(
            query=query,
            analysis={"facts": {}, "diagnosis": {"risk_level": "low", "summary": "未执行任何查询。"}},
            plan=plan,
        )

    risk_order = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
    confidence_order = {"low": 1, "medium": 2, "high": 3}
    risk = max((report.get("risk_level", "unknown") for report in reports), key=lambda item: risk_order.get(item, 0))
    confidence = max((report.get("confidence", "low") for report in reports), key=lambda item: confidence_order.get(item, 0))
    services = sorted({service for report in reports for service in report.get("services", [])})
    data_sources = [source for report in reports for source in report.get("data_sources", [])]
    evidence = [item for report in reports for item in report.get("evidence", [])][:20]
    timeline = [item for report in reports for item in report.get("timeline", [])][:40]
    next_actions = []
    for report in reports:
        for action in report.get("next_actions", []):
            if action not in next_actions:
                next_actions.append(action)
    result_status = _result_status(data_sources, evidence, {"incidents": [
        incident
        for report in reports
        for incident in report.get("facts_summary", {}).get("incidents", [])
        if incident
    ]})
    if result_status == "data_unavailable":
        root_cause = "所有可用日志来源均不可用，暂时不能判断是否存在异常。"
    elif result_status == "safety_blocked":
        root_cause = "所有命中日志均因安全过滤被阻断，暂时不能输出诊断结论。"
    elif not evidence:
        root_cause = "所有查询均未命中明确异常；建议扩大时间窗口或检查日志采集覆盖。"
    else:
        root_cause = "；".join(
            report.get("root_cause", "")
            for report in reports
            if report.get("evidence") and report.get("root_cause")
        )[:1000]
    time_window = (plan or {}).get("time_window") or next((report.get("time_window") for report in reports if report.get("time_window")), None)
    return {
        "incident_id": _stable_incident_id(query, data_sources, time_window),
        "query": query,
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "time_window": time_window,
        "services": services,
        "data_sources": data_sources,
        "evidence": evidence,
        "timeline": timeline,
        "root_cause": root_cause,
        "result_status": result_status,
        "confidence": "unknown" if result_status in {"data_unavailable", "safety_blocked"} else confidence,
        "risk_level": "unknown" if result_status in {"data_unavailable", "safety_blocked"} else risk,
        "next_actions": next_actions[:12],
        "redaction_status": (
            "blocked" if result_status == "safety_blocked"
            else "partial" if any(source.get("redaction_dropped_count", 0) for source in data_sources)
            else "passed"
        ),
        "query_attempts": (plan or {}).get("query_attempts", []),
        "facts_summary": {
            "child_reports": len(reports),
            "incidents": [
                incident
                for report in reports
                for incident in report.get("facts_summary", {}).get("incidents", [])
                if incident
            ],
        },
        "child_reports": reports,
    }
