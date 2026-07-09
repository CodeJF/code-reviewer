"""
Unified incident report model and renderer for SL100 diagnosis outputs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sl100_es import SHANGHAI_TZ


def _stable_incident_id(query: str, data_sources: list[dict[str, Any]], time_window: dict[str, Any] | None) -> str:
    payload = json.dumps(
        {"query": query, "data_sources": data_sources, "time_window": time_window},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sl100-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _collect_evidence(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
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
    return evidence_items[:12]


def _confidence(facts: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
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
    evidence = _collect_evidence(diagnosis)
    next_actions = diagnosis.get("next_steps", [])
    root_cause = diagnosis.get("summary", "")
    for incident in diagnosis.get("incidents", []):
        causes = incident.get("possible_causes") or incident.get("suggestions") or []
        incident_type = incident.get("type")
        if incident_type and causes:
            root_cause = f"命中 {incident_type}：{causes[0]}"
            break
    if not evidence and facts.get("error_count", 0) == 0:
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
        "confidence": _confidence(facts, evidence),
        "risk_level": diagnosis.get("risk_level", facts.get("risk_level", "unknown")),
        "next_actions": next_actions,
        "redaction_status": redaction_status,
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
            if source_type == "elasticsearch":
                lines.append(f"- ES {source.get('index_pattern')} returned={source.get('returned')} total={source.get('total')}")
            elif source_type == "remote_file":
                refs = source.get("refs", [])
                names = ", ".join(f"{ref.get('host')}:{ref.get('path')}" for ref in refs[:4])
                lines.append(f"- remote_file {names}")
            else:
                lines.append(f"- {source_type}")

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
    if not evidence:
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
        "confidence": confidence,
        "risk_level": risk,
        "next_actions": next_actions[:12],
        "redaction_status": "passed",
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
