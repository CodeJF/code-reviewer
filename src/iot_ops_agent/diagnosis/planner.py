"""
Deterministic query planner for SL100 incident diagnosis.

This is deliberately small and predictable: it turns common Chinese incident
phrases into services, keywords, and time-window arguments that ES and remote
log tools can share.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from iot_ops_agent.integrations.elasticsearch import SHANGHAI_TZ, build_time_window


SERVICE_TERMS = {
    "gateway": ["gateway", "网关", "登录"],
    "deviceShadow": ["deviceShadow", "device-shadow", "设备影子", "mqtt", "websocket", "上下线", "离线"],
    "pushService": ["pushService", "push-service", "推送", "push"],
    "access": ["access", "接口", "http", "请求"],
}

KEYWORD_RULES = [
    (["websocket", "ws"], "websocket OR disconnect OR upgrade OR handshake OR error"),
    (["mqtt", "上下线", "离线", "掉线"], "mqtt OR offline OR disconnect OR discarded OR keepalive OR error"),
    (["登录", "login"], "login OR uuid OR invalid OR error OR failed"),
    (["推送", "push"], "push OR notify OR getui OR error OR failed"),
    (["ota", "升级"], "ota OR upgrade OR firmware OR error OR failed"),
]


def _contains_any(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in terms)


def infer_services(query: str) -> list[str]:
    services = [service for service, terms in SERVICE_TERMS.items() if _contains_any(query, terms)]
    if not services:
        return ["gateway", "deviceShadow"] if _contains_any(query, ["登录", "login"]) else ["deviceShadow"]
    if "登录" in query and "gateway" not in services:
        services.insert(0, "gateway")
    return services


def infer_keyword(query: str) -> str:
    for terms, keyword in KEYWORD_RULES:
        if _contains_any(query, terms):
            return keyword
    return "error OR fail OR failed OR fatal OR panic OR timeout OR exception OR invalid OR disconnect"


def _explicit_datetimes(query: str) -> list[str]:
    return re.findall(r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}", query)


def _explicit_date(query: str, now: datetime) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", query)
    if match:
        return match.group(0)
    if "昨天" in query:
        return (now.date() - timedelta(days=1)).isoformat()
    if "今天" in query or "上午" in query or "下午" in query or "晚上" in query:
        return now.date().isoformat()
    return ""


def _hour_window(query: str, date_text: str) -> tuple[str, str] | None:
    match = re.search(r"(上午|下午|晚上|凌晨)?\s*(\d{1,2})\s*[点時时](?:多|左右|附近)?", query)
    if not match or not date_text:
        return None
    period = match.group(1) or ""
    hour = int(match.group(2))
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    if period == "凌晨" and hour == 12:
        hour = 0
    return f"{date_text} {hour:02d}:00", f"{date_text} {min(hour + 1, 23):02d}:00"


def plan_query(query: str, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    datetimes = _explicit_datetimes(query)
    date_text = _explicit_date(query, now)
    from_time = ""
    to_time = ""
    around = ""
    around_minutes = 10

    has_explicit_time = False
    if len(datetimes) >= 2:
        from_time, to_time = datetimes[0], datetimes[1]
        has_explicit_time = True
    elif len(datetimes) == 1:
        around = datetimes[0]
        has_explicit_time = True
    elif "最近一小时" in query or "近一小时" in query:
        start = now - timedelta(hours=1)
        from_time = start.strftime("%Y-%m-%d %H:%M")
        to_time = now.strftime("%Y-%m-%d %H:%M")
        has_explicit_time = True
    else:
        hour_window = _hour_window(query, date_text)
        if hour_window:
            from_time, to_time = hour_window
            has_explicit_time = True

    time_strategy = "explicit"
    if not date_text and not has_explicit_time:
        start = now - timedelta(hours=2)
        date_text = now.date().isoformat()
        from_time = start.strftime("%Y-%m-%d %H:%M")
        to_time = now.strftime("%Y-%m-%d %H:%M")
        time_strategy = "recent_then_today"

    window = build_time_window(
        date_text=date_text,
        from_text=from_time,
        to_text=to_time,
        around_text=around,
        around_minutes=around_minutes,
    )
    services = infer_services(query)
    keyword = infer_keyword(query)
    return {
        "query": query,
        "services": services,
        "primary_service": services[0],
        "keyword": keyword,
        "date": date_text,
        "from_time": from_time,
        "to_time": to_time,
        "around": around,
        "around_minutes": around_minutes,
        "time_window": window.to_dict(),
        "has_explicit_time": has_explicit_time or bool(date_text and not time_strategy == "recent_then_today"),
        "time_strategy": time_strategy,
        "chain_services": ["gateway", "deviceShadow", "pushService", "access"]
        if _contains_any(query, ["链路", "串", "登录失败", "完整"])
        else services,
        "data_source_order": ["elasticsearch", "remote_file_fallback"],
    }
