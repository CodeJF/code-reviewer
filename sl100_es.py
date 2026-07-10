"""
Read-only Elasticsearch access for the SL100 operations Agent.

The ES cluster is reached through SSH on sl100-93 and queried on the remote
loopback address. This keeps the logging system private while giving local
tools a small, controlled search surface.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sl100_config import get_es_config
from sl100_log_core import (
    LogSnapshot,
    assert_redacted,
    diagnose_with_claude,
    docs_context_for_query,
    extract_log_facts,
    local_diagnosis,
    redact_text,
)


ES_CONFIG = get_es_config()
ES_SSH_HOST = str(ES_CONFIG["ssh_host"])
ES_BASE_URL = str(ES_CONFIG["base_url"])
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
DEFAULT_ERROR_QUERY = "error OR fail OR failed OR fatal OR panic OR timeout OR exception OR invalid OR disconnect"
ES_SOURCE_FIELDS = ["@timestamp", "fields.log_type", "host.hostname", "host.ip", "message"]

SERVICE_INDEX_PREFIXES = dict(ES_CONFIG["services"])
MAX_ES_HITS = int(ES_CONFIG.get("max_hits", 200))

SERVICE_ALIASES = {
    "gateway": "gateway",
    "gw": "gateway",
    "deviceshadow": "deviceShadow",
    "device-shadow": "deviceShadow",
    "device_shadow": "deviceShadow",
    "deviceShadow": "deviceShadow",
    "pushservice": "pushService",
    "push-service": "pushService",
    "push_service": "pushService",
    "pushService": "pushService",
    "access": "access",
    "api-access": "access",
}

SAFE_ES_PATH_RE = re.compile(r"^[A-Za-z0-9_./,*?=&-]+$")


class ElasticsearchQueryError(RuntimeError):
    """A safe, typed error returned by the read-only Elasticsearch client."""

    def __init__(self, message: str, *, status: int | None = None, error_type: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type


class ElasticsearchIndexNotFound(ElasticsearchQueryError):
    """The requested daily index does not exist and therefore cannot be queried."""


@dataclass(frozen=True)
class TimeWindow:
    start_local: datetime
    end_local: datetime

    @property
    def start_utc(self) -> datetime:
        return self.start_local.astimezone(UTC)

    @property
    def end_utc(self) -> datetime:
        return self.end_local.astimezone(UTC)

    def to_dict(self) -> dict[str, str]:
        return {
            "timezone": "Asia/Shanghai",
            "start_local": self.start_local.isoformat(),
            "end_local": self.end_local.isoformat(),
            "start_utc": _format_es_datetime(self.start_utc),
            "end_utc": _format_es_datetime(self.end_utc),
        }


def normalize_service(service: str) -> str:
    normalized = SERVICE_ALIASES.get(service.strip())
    if not normalized:
        normalized = SERVICE_ALIASES.get(service.strip().lower())
    if not normalized:
        raise ValueError(f"不支持的 ES 服务名: {service}. 可选: {', '.join(SERVICE_INDEX_PREFIXES)}")
    return normalized


def _format_es_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_local_datetime(value: str) -> datetime:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("时间不能为空")
    if cleaned.endswith("Z"):
        parsed = datetime.fromisoformat(cleaned[:-1] + "+00:00")
    else:
        parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def build_time_window(
    *,
    date_text: str = "",
    from_text: str = "",
    to_text: str = "",
    around_text: str = "",
    around_minutes: int = 10,
) -> TimeWindow:
    """Build an Asia/Shanghai time window for ES UTC filtering."""
    if around_text:
        center = _parse_local_datetime(around_text)
        delta = timedelta(minutes=max(1, around_minutes))
        return TimeWindow(center - delta, center + delta)

    if from_text or to_text:
        if not from_text or not to_text:
            raise ValueError("--from 和 --to 必须同时提供")
        start = _parse_local_datetime(from_text)
        end = _parse_local_datetime(to_text)
        if end < start:
            raise ValueError("--to 不能早于 --from")
        return TimeWindow(start, end)

    if date_text:
        day = date.fromisoformat(date_text)
    else:
        day = datetime.now(SHANGHAI_TZ).date()
    start = datetime.combine(day, time.min, tzinfo=SHANGHAI_TZ)
    end = datetime.combine(day, time.max, tzinfo=SHANGHAI_TZ)
    return TimeWindow(start, end)


def _dates_for_window(window: TimeWindow) -> list[date]:
    start_day = window.start_local.date()
    end_day = window.end_local.date()
    days = []
    current = start_day
    while current <= end_day:
        days.append(current)
        current += timedelta(days=1)
    return days


def index_pattern_for_service(service: str, window: TimeWindow | None = None, date_text: str = "") -> str:
    service_name = normalize_service(service)
    prefix = SERVICE_INDEX_PREFIXES[service_name]
    if window is None and not date_text:
        return f"{prefix}-*"
    if window is None:
        day = date.fromisoformat(date_text)
        return f"{prefix}-{day.isoformat()}"
    indices = [f"{prefix}-{day.isoformat()}" for day in _dates_for_window(window)]
    # Daily indices can be sparse. A wildcard plus the mandatory @timestamp
    # range is safer than failing an entire multi-day query on one missing day.
    return indices[0] if len(indices) == 1 else f"{prefix}-*"


def _validate_es_path(path: str) -> None:
    if not SAFE_ES_PATH_RE.match(path):
        raise ValueError(f"非法 ES path: {path}")


def _ssh_curl(path: str, *, method: str = "GET", body: dict[str, Any] | None = None, timeout: int = 10) -> str:
    _validate_es_path(path)
    method = method.upper()
    if method not in {"GET", "POST"}:
        raise ValueError(f"不支持的 HTTP method: {method}")

    url = f"{ES_BASE_URL}/{path.lstrip('/')}"
    command_parts = [
        "curl",
        "-sS",
        "-m",
        str(timeout),
        "-X",
        method,
        shlex.quote(url),
    ]
    input_text = None
    if body is not None:
        command_parts.extend(["-H", shlex.quote("Content-Type: application/json"), "--data-binary", "@-"])
        input_text = json.dumps(body, ensure_ascii=False)

    remote_command = " ".join(command_parts)
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            ES_SSH_HOST,
            remote_command,
        ],
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout + 5,
        check=False,
    )
    if completed.returncode != 0:
        stderr = redact_text(completed.stderr.strip())
        raise RuntimeError(f"ES SSH 查询失败: {stderr or completed.returncode}")
    return completed.stdout


def _parse_json_response(text: str) -> Any:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ElasticsearchQueryError(f"ES 返回不是合法 JSON: {redact_text(text[:300])}") from exc

    if not isinstance(data, dict) or "error" not in data:
        return data

    error = data.get("error")
    error_type = error.get("type", "") if isinstance(error, dict) else ""
    reason = error.get("reason", "") if isinstance(error, dict) else str(error)
    status = data.get("status")
    safe_reason = redact_text(str(reason))[:300]
    message = f"ES 查询失败: {error_type or 'unknown'} {safe_reason}".strip()
    if error_type == "index_not_found_exception" or status == 404:
        raise ElasticsearchIndexNotFound(message, status=status, error_type=error_type)
    raise ElasticsearchQueryError(message, status=status, error_type=error_type)


def es_health() -> dict[str, Any]:
    data = _parse_json_response(_ssh_curl("/", timeout=5))
    return {
        "cluster_name": data.get("cluster_name"),
        "version": data.get("version", {}).get("number"),
        "node": data.get("name"),
        "ssh_host": ES_SSH_HOST,
        "base_url": "127.0.0.1:9200 on remote host",
    }


def list_indices(date_text: str = "", service: str = "") -> list[dict[str, Any]]:
    if service:
        if date_text:
            pattern = index_pattern_for_service(service, date_text=date_text)
        else:
            pattern = index_pattern_for_service(service)
    else:
        pattern = f"api-*-{date.fromisoformat(date_text).isoformat()}" if date_text else "api-*"
    path = f"_cat/indices/{pattern}?format=json&s=index"
    try:
        data = _parse_json_response(_ssh_curl(path, timeout=10))
    except ElasticsearchIndexNotFound:
        return []
    if not isinstance(data, list):
        raise ElasticsearchQueryError("ES 索引列表返回格式异常")
    return [
        {
            "health": item.get("health"),
            "status": item.get("status"),
            "index": item.get("index"),
            "docs_count": item.get("docs.count"),
            "store_size": item.get("store.size"),
        }
        for item in data
    ]


def build_search_body(
    *,
    keyword: str = "",
    window: TimeWindow,
    size: int = 20,
    source_fields: list[str] | None = None,
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = [
        {
            "range": {
                "@timestamp": {
                    "gte": _format_es_datetime(window.start_utc),
                    "lte": _format_es_datetime(window.end_utc),
                }
            }
        }
    ]
    must: list[dict[str, Any]] = [_keyword_query(keyword)]

    return {
        "size": max(0, min(size, MAX_ES_HITS)),
        "sort": [{"@timestamp": {"order": "desc"}}],
        "_source": source_fields or ES_SOURCE_FIELDS,
        "query": {"bool": {"filter": filters, "must": must}},
    }


def _keyword_query(keyword: str) -> dict[str, Any]:
    cleaned = keyword.strip()
    if not cleaned:
        return {"match_all": {}}
    parts = [
        part.strip()
        for part in re.split(r"\s+(?:OR|or)\s+|\|", cleaned)
        if part.strip()
    ]
    if len(parts) <= 1:
        return {
            "simple_query_string": {
                "query": cleaned,
                "fields": ["message"],
                "default_operator": "and",
            }
        }
    return {
        "bool": {
            "should": [
                {
                    "simple_query_string": {
                        "query": part,
                        "fields": ["message"],
                        "default_operator": "and",
                    }
                }
                for part in parts
            ],
            "minimum_should_match": 1,
        }
    }


def search_logs(
    *,
    service: str,
    keyword: str = "",
    date_text: str = "",
    from_text: str = "",
    to_text: str = "",
    around_text: str = "",
    around_minutes: int = 10,
    size: int = 20,
) -> dict[str, Any]:
    service_name = normalize_service(service)
    window = build_time_window(
        date_text=date_text,
        from_text=from_text,
        to_text=to_text,
        around_text=around_text,
        around_minutes=around_minutes,
    )
    index_pattern = index_pattern_for_service(service_name, window=window)
    body = build_search_body(keyword=keyword, window=window, size=size)
    response = _parse_json_response(_ssh_curl(f"{index_pattern}/_search", method="POST", body=body, timeout=15))
    if not isinstance(response, dict):
        raise ElasticsearchQueryError("ES 搜索返回格式异常")
    hits = []
    redaction_dropped_count = 0
    for item in response.get("hits", {}).get("hits", []):
        source = item.get("_source", {})
        message = redact_text(str(source.get("message", "")))
        if assert_redacted(message):
            redaction_dropped_count += 1
            continue
        host = source.get("host") if isinstance(source.get("host"), dict) else {}
        fields = source.get("fields") if isinstance(source.get("fields"), dict) else {}
        hits.append({
            "index": item.get("_index"),
            "id": item.get("_id"),
            "timestamp": source.get("@timestamp"),
            "log_type": fields.get("log_type"),
            "host": redact_text(str(host.get("hostname", ""))),
            "message": message,
        })
    return {
        "service": service_name,
        "index_pattern": index_pattern,
        "keyword": keyword,
        "time_window": window.to_dict(),
        "total": response.get("hits", {}).get("total"),
        "hits": hits,
        "raw_returned": len(response.get("hits", {}).get("hits", [])),
        "redaction_dropped_count": redaction_dropped_count,
        "source_status": "partial" if redaction_dropped_count else "ok",
    }


def count_logs(
    *,
    service: str,
    keyword: str = "",
    date_text: str = "",
    from_text: str = "",
    to_text: str = "",
    around_text: str = "",
    around_minutes: int = 10,
) -> dict[str, Any]:
    service_name = normalize_service(service)
    window = build_time_window(
        date_text=date_text,
        from_text=from_text,
        to_text=to_text,
        around_text=around_text,
        around_minutes=around_minutes,
    )
    index_pattern = index_pattern_for_service(service_name, window=window)
    body = build_search_body(keyword=keyword, window=window, size=0)
    response = _parse_json_response(_ssh_curl(f"{index_pattern}/_count", method="POST", body={"query": body["query"]}, timeout=10))
    if not isinstance(response, dict):
        raise ElasticsearchQueryError("ES 计数返回格式异常")
    return {
        "service": service_name,
        "index_pattern": index_pattern,
        "keyword": keyword,
        "time_window": window.to_dict(),
        "count": response.get("count", 0),
    }


def facts_from_es_search(search_result: dict[str, Any]) -> dict[str, Any]:
    service = search_result["service"]
    lines = [hit["message"] for hit in search_result.get("hits", []) if hit.get("message")]
    content = redact_text("\n".join(lines))
    leaks = assert_redacted(content)
    if leaks:
        raise RuntimeError(f"ES 安全过滤异常: {', '.join(leaks)}")
    snapshot = LogSnapshot(
        path=f"elasticsearch://{search_result['index_pattern']}",
        service=service,
        line_count=len(lines),
        content=content,
    )
    facts = extract_log_facts([snapshot])
    facts["source"] = {
        "type": "elasticsearch",
        "ssh_host": ES_SSH_HOST,
        "index_pattern": search_result["index_pattern"],
        "keyword": search_result.get("keyword", ""),
        "time_window": search_result["time_window"],
        "total": search_result.get("total"),
        "returned": len(search_result.get("hits", [])),
        "raw_returned": search_result.get("raw_returned", len(search_result.get("hits", []))),
        "redaction_dropped_count": search_result.get("redaction_dropped_count", 0),
        "status": (
            "safety_blocked"
            if not lines and search_result.get("redaction_dropped_count", 0)
            else search_result.get("source_status", "ok")
        ),
    }
    facts["top_messages"] = [
        {"message": message, "count": count}
        for message, count in Counter(lines).most_common(8)
    ]
    return facts


def analyze_logs(
    *,
    service: str,
    keyword: str = DEFAULT_ERROR_QUERY,
    date_text: str = "",
    from_text: str = "",
    to_text: str = "",
    around_text: str = "",
    around_minutes: int = 10,
    size: int = 80,
    use_ai: bool = False,
) -> dict[str, Any]:
    search_result = search_logs(
        service=service,
        keyword=keyword,
        date_text=date_text,
        from_text=from_text,
        to_text=to_text,
        around_text=around_text,
        around_minutes=around_minutes,
        size=size,
    )
    facts = facts_from_es_search(search_result)
    if use_ai:
        docs_context = docs_context_for_query(f"SL100 {service} {keyword} 日志 排障")
        diagnosis = diagnose_with_claude(facts, docs_context=docs_context)
    else:
        diagnosis = local_diagnosis(facts)
    return {
        "facts": facts,
        "diagnosis": diagnosis,
    }
