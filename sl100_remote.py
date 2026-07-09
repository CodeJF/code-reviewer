"""
Read-only SSH tail access to SL100 service log files.

This is a fallback for cases where Elasticsearch is missing a service, delayed,
or does not contain process stderr/stdout logs. Paths are fixed in a whitelist;
callers cannot pass arbitrary remote paths or commands.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sl100_es import SHANGHAI_TZ, TimeWindow, build_time_window
from sl100_config import get_remote_logs_config
from sl100_log_core import (
    LogSnapshot,
    assert_redacted,
    diagnose_with_claude,
    docs_context_for_query,
    extract_log_facts,
    local_diagnosis,
    redact_text,
)


DEFAULT_REMOTE_TAIL_LINES = 300
ERROR_KEYWORD_RE = re.compile(r"(?i)(error|fail|failed|fatal|panic|timeout|invalid|offline|disconnect|exception)")
LINE_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


REMOTE_LOGS: dict[str, dict[str, Any]] = get_remote_logs_config()

SERVICE_ALIASES = {
    "gateway": "gateway",
    "deviceshadow": "deviceShadow",
    "device-shadow": "deviceShadow",
    "device_shadow": "deviceShadow",
    "deviceShadow": "deviceShadow",
    "scheduledtask": "scheduledTask",
    "scheduled-task": "scheduledTask",
    "pushservice": "pushService",
    "push-service": "pushService",
    "push_service": "pushService",
    "pushService": "pushService",
    "cloudstorage": "cloudStorage",
    "cloud-storage": "cloudStorage",
    "cloud_storage": "cloudStorage",
    "cloudStorage": "cloudStorage",
    "adminservice": "AdminService",
    "admin-service": "AdminService",
    "admin_service": "AdminService",
    "AdminService": "AdminService",
}


@dataclass(frozen=True)
class RemoteLogRef:
    service: str
    log: str
    host: str
    path: str


def normalize_service(service: str) -> str:
    normalized = SERVICE_ALIASES.get(service.strip())
    if not normalized:
        normalized = SERVICE_ALIASES.get(service.strip().lower())
    if not normalized:
        raise ValueError(f"unsupported remote service: {service}")
    return normalized


def resolve_log_ref(service: str, log: str) -> RemoteLogRef:
    service_name = normalize_service(service)
    service_config = REMOTE_LOGS[service_name]
    log_name = log.strip().lower()
    logs = service_config["logs"]
    if log_name not in logs:
        raise ValueError(f"unsupported log '{log}' for {service_name}; available: {', '.join(sorted(logs))}")
    return RemoteLogRef(
        service=service_name,
        log=log_name,
        host=service_config["host"],
        path=logs[log_name],
    )


def list_remote_logs(service: str = "") -> list[dict[str, str]]:
    services = [normalize_service(service)] if service else sorted(REMOTE_LOGS)
    result = []
    for service_name in services:
        service_config = REMOTE_LOGS[service_name]
        for log_name, path in sorted(service_config["logs"].items()):
            result.append({
                "service": service_name,
                "log": log_name,
                "host": service_config["host"],
                "path": path,
            })
    return result


def _ssh_tail(ref: RemoteLogRef, tail_lines: int, timeout: int = 10) -> str:
    line_count = max(1, min(tail_lines, 5000))
    remote_command = " ".join([
        "test",
        "-r",
        shlex.quote(ref.path),
        "&&",
        "tail",
        "-n",
        str(line_count),
        shlex.quote(ref.path),
    ])
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            ref.host,
            remote_command,
        ],
        text=True,
        capture_output=True,
        timeout=timeout + 5,
        check=False,
    )
    if completed.returncode != 0:
        stderr = redact_text(completed.stderr.strip())
        raise RuntimeError(f"remote log tail failed for {ref.service}/{ref.log}: {stderr or completed.returncode}")
    return redact_text(completed.stdout)


def _optional_window(
    date_text: str = "",
    from_text: str = "",
    to_text: str = "",
    around_text: str = "",
    around_minutes: int = 10,
) -> TimeWindow | None:
    if not any([date_text, from_text, to_text, around_text]):
        return None
    return build_time_window(
        date_text=date_text,
        from_text=from_text,
        to_text=to_text,
        around_text=around_text,
        around_minutes=around_minutes,
    )


def _parse_line_timestamp(line: str) -> datetime | None:
    match = LINE_TIMESTAMP_RE.search(line)
    if not match:
        return None
    text = match.group(0)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def _filter_content_by_window(content: str, window: TimeWindow | None) -> str:
    if window is None:
        return content
    selected = []
    for line in content.splitlines():
        timestamp = _parse_line_timestamp(line)
        if timestamp is None:
            continue
        if window.start_local <= timestamp <= window.end_local:
            selected.append(line)
    return "\n".join(selected)


def tail_remote_log(service: str, log: str = "error", tail_lines: int = DEFAULT_REMOTE_TAIL_LINES) -> dict[str, Any]:
    ref = resolve_log_ref(service, log)
    content = _ssh_tail(ref, tail_lines)
    leaks = assert_redacted(content)
    if leaks:
        raise RuntimeError(f"remote log redaction failed: {', '.join(leaks)}")
    return {
        "service": ref.service,
        "log": ref.log,
        "host": ref.host,
        "path": ref.path,
        "tail_lines": max(1, min(tail_lines, 5000)),
        "content": content,
    }


def search_remote_log(
    service: str,
    log: str = "error",
    keyword: str = "",
    tail_lines: int = 2000,
    limit: int = 80,
    date_text: str = "",
    from_text: str = "",
    to_text: str = "",
    around_text: str = "",
    around_minutes: int = 10,
) -> list[dict[str, Any]]:
    ref = resolve_log_ref(service, log)
    content = _filter_content_by_window(
        _ssh_tail(ref, tail_lines),
        _optional_window(date_text, from_text, to_text, around_text, around_minutes),
    )
    results = []
    keyword_lower = keyword.lower().strip()
    for index, line in enumerate(content.splitlines(), start=1):
        lower = line.lower()
        if keyword_lower:
            matched = keyword_lower in lower
        else:
            matched = bool(ERROR_KEYWORD_RE.search(line))
        if not matched:
            continue
        results.append({
            "service": ref.service,
            "log": ref.log,
            "host": ref.host,
            "path": ref.path,
            "line": index,
            "content": line[:1000],
        })
        if len(results) >= max(1, min(limit, 200)):
            break
    return results


def analyze_remote_logs(
    service: str,
    logs: list[str] | None = None,
    tail_lines: int = 800,
    use_ai: bool = False,
    date_text: str = "",
    from_text: str = "",
    to_text: str = "",
    around_text: str = "",
    around_minutes: int = 10,
) -> dict[str, Any]:
    service_name = normalize_service(service)
    log_names = logs or ["error"]
    window = _optional_window(date_text, from_text, to_text, around_text, around_minutes)
    snapshots = []
    refs = []
    for log_name in log_names:
        ref = resolve_log_ref(service_name, log_name)
        content = _filter_content_by_window(_ssh_tail(ref, tail_lines), window)
        leaks = assert_redacted(content)
        if leaks:
            raise RuntimeError(f"remote log redaction failed: {', '.join(leaks)}")
        refs.append(ref)
        snapshots.append(LogSnapshot(
            path=f"ssh://{ref.host}{ref.path}",
            service=ref.service,
            line_count=len(content.splitlines()),
            content=content,
        ))
    facts = extract_log_facts(snapshots)
    facts["source"] = {
        "type": "remote_file",
        "refs": [
            {"service": ref.service, "log": ref.log, "host": ref.host, "path": ref.path}
            for ref in refs
        ],
        "tail_lines": max(1, min(tail_lines, 5000)),
        "time_window": window.to_dict() if window else None,
    }
    if use_ai:
        docs_context = docs_context_for_query(f"SL100 {service_name} 日志 排障")
        diagnosis = diagnose_with_claude(facts, docs_context=docs_context)
    else:
        diagnosis = local_diagnosis(facts)
    return {"facts": facts, "diagnosis": diagnosis}
