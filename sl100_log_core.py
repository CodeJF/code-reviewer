"""
Shared helpers for the SL100 operations diagnosis Agent.

This module is intentionally dependency-light. It does three jobs:
1. redact sensitive log content before any model call,
2. extract deterministic facts from logs,
3. optionally ask Claude to turn those facts into a diagnosis report.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


MODEL = "claude-sonnet-4-6"
SL100_ROOT = Path(os.environ.get("SL100_ROOT", "/Users/jianfengxu/Desktop/SL100_Service"))
DEFAULT_LOG_ROOTS = [
    SL100_ROOT / ".local-run",
    SL100_ROOT / "gateway" / "log",
    SL100_ROOT / "deviceShadow" / "log",
    SL100_ROOT / "pushService" / "log",
    SL100_ROOT / "AdminService" / "log",
    SL100_ROOT / "cloudStorage" / "log",
]
DEFAULT_DOC_PATHS = [
    SL100_ROOT / "docs" / "SL100服务架构与部署说明.md",
    SL100_ROOT / "docs" / "SL100模拟设备实现说明.md",
    SL100_ROOT / "MQTT_WebSocket_对接技术文档.md",
    SL100_ROOT / "gateway" / "README.md",
    SL100_ROOT / "deviceShadow" / "README.md",
    SL100_ROOT / "pushService" / "README.md",
    SL100_ROOT / "AdminService" / "README.md",
    SL100_ROOT / "cloudStorage" / "README.md",
]
DOC_DOMAIN_TERMS = [
    "AdminService",
    "cloudStorage",
    "deviceShadow",
    "gateway",
    "MongoDB",
    "MQTT",
    "MySQL",
    "pushService",
    "Redis",
    "RPC",
    "WebSocket",
    "绑定",
    "部署",
    "登录",
    "队列",
    "服务",
    "架构",
    "离线",
    "日志",
    "设备",
    "上线",
    "推送",
    "影子",
]

SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(['\"]?(?:password|passwd|pwd)['\"]?\s*:\s*)['\"]?[^,'\"\s}]+['\"]?"), r'\1"<REDACTED_PASSWORD>"'),
    (re.compile(r"(?i)(['\"]?(?:token|auth[_-]?token|access[_-]?token)['\"]?\s*:\s*)['\"]?[^,'\"\s}]+['\"]?"), r'\1"<REDACTED_TOKEN>"'),
    (re.compile(r"(?i)(['\"]?(?:secret|access[_-]?key|access[_-]?key[_-]?secret|api[_-]?key)['\"]?\s*:\s*)['\"]?[^,'\"\s}]+['\"]?"), r'\1"<REDACTED_SECRET>"'),
    (re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"]?[^,'\"\s}]+"), r"\1=<REDACTED_PASSWORD>"),
    (re.compile(r"(?i)(token|auth[_-]?token|access[_-]?token)\s*[:=]\s*['\"]?[^,'\"\s}]+"), r"\1=<REDACTED_TOKEN>"),
    (re.compile(r"(?i)(secret|access[_-]?key|access[_-]?key[_-]?secret|api[_-]?key)\s*[:=]\s*['\"]?[^,'\"\s}]+"), r"\1=<REDACTED_SECRET>"),
    (re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_-]+"), "<REDACTED_ANTHROPIC_KEY>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "<PHONE>"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b[A-Fa-f0-9]{32,64}\b"), "<HEX_ID>"),
]

TIMESTAMP_RE = re.compile(
    r"(?P<ts>\d{4}[-/]\d{2}[-/]\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
MESSAGE_ID_RE = re.compile(r"(?i)\b(?:message[_-]?id|msg[_-]?id|request[_-]?id)\b[\"'=:\s]+(?P<id>[A-Za-z0-9_-]{6,})")
UUID_RE = re.compile(r"(?i)\b(?:uuid|device[_-]?id|sn)\b[\"'=:\s]+(?P<id>[A-Za-z0-9_-]{6,})")
ERROR_RE = re.compile(r"(?i)\b(error|err|fail|failed|fatal|panic|timeout|invalid|denied|refused|disconnect|offline|unauthorized|exception)\b")
WARN_RE = re.compile(r"(?i)\b(warn|warning|retry|reconnect|slow|degraded)\b")

INCIDENT_RULES = [
    {
        "type": "device_login_failed",
        "keywords": ["/device/login", "devicelogin", "device login failed", "uuidinvalid"],
        "services": ["gateway"],
        "title": "设备登录失败",
    },
    {
        "type": "mqtt_connection_failed",
        "all_keywords": ["mqtt"],
        "keywords": ["init fail", "connect retry", "connection refused", "connect refused", "network timeout", "subscribe fail", "reconnect", "disconnect error", "connection error"],
        "services": ["deviceShadow"],
        "title": "MQTT 连接或订阅异常",
    },
    {
        "type": "mqtt_payload_invalid",
        "keywords": ["payload", "format", "unmarshal", "json", "topic is invalid"],
        "services": ["deviceShadow"],
        "title": "MQTT payload 格式异常",
    },
    {
        "type": "device_online_offline_flapping",
        "keywords": ["device mqtt online", "device mqtt offline", "connected", "disconnected"],
        "min_evidence": 3,
        "services": ["deviceShadow"],
        "title": "设备上下线频繁",
    },
    {
        "type": "rpc_call_failed",
        "keywords": ["rpc", "notify", "device bind", "unbind", "wake up", "设备无法唤醒"],
        "services": ["gateway", "deviceShadow"],
        "title": "RPC 或跨服务通知失败",
    },
    {
        "type": "push_failed",
        "keywords": ["push send error", "getui", "device event", "notify user"],
        "services": ["pushService", "deviceShadow"],
        "title": "推送失败",
    },
    {
        "type": "ota_failed",
        "keywords": ["ota", "upgrade", "firmware", "device upgrade", "upgrade failed"],
        "services": ["deviceShadow", "pushService", "gateway"],
        "title": "OTA 升级异常",
    },
    {
        "type": "websocket_failed",
        "keywords": [
            "websocket send error",
            "websocket read error",
            "websocket broken pipe",
            "websocket connection error",
            "websocket failed",
            "websocket: close",
            "abnormal closure",
            "客户端数据读取错误",
            "连接升级websocket失败",
        ],
        "services": ["deviceShadow"],
        "title": "WebSocket 连接或推送异常",
    },
    {
        "type": "config_init_failed",
        "keywords": ["env file open error", "configuration parameter validation failed", "config init", "configuration init", "load config", "env.yaml"],
        "services": ["gateway", "deviceShadow", "pushService", "AdminService", "cloudStorage"],
        "title": "配置初始化失败",
    },
    {
        "type": "database_connection_failed",
        "keyword_groups": [
            ["mysql", "mongo", "mongodb", "redis", "wrongpass"],
            ["connection error", "ping error", "conn error", "connect refused", "connection refused", "dial tcp", "noauth", "authentication required", "wrongpass"],
        ],
        "services": ["gateway", "deviceShadow", "pushService", "AdminService", "cloudStorage"],
        "title": "数据库或缓存连接失败",
    },
    {
        "type": "payload_type_mismatch",
        "all_keywords": ["error decoding key payload", "cannot decode string into a map"],
        "services": ["gateway"],
        "title": "Payload 数据类型不匹配",
    },
]


@dataclass
class LogSnapshot:
    path: str
    service: str
    line_count: int
    content: str


def init_anthropic_client():
    """Create an Anthropic client while avoiding Claude Code env var collisions."""
    load_dotenv(override=True)
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    import anthropic

    return anthropic.Anthropic()


def redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def assert_redacted(text: str) -> list[str]:
    """Return descriptions of sensitive-looking values still present."""
    leaks: list[str] = []
    checks = [
        ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
        ("phone", re.compile(r"\b1[3-9]\d{9}\b")),
        ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
        ("anthropic_key", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_-]+")),
        ("token_assignment", re.compile(r"(?i)(token|auth[_-]?token|access[_-]?token)\s*[:=]\s*(?![\[{]|[\"']?<REDACTED_)[^,\s}]+")),
        ("password_assignment", re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*(?![\[\{]|[\"']?<REDACTED_)[^,\s}]+")),
        ("secret_assignment", re.compile(r"(?i)(secret|access[_-]?key|api[_-]?key)\s*[:=]\s*(?![\[\{]|[\"']?<REDACTED_)[^,\s}]+")),
    ]
    for name, pattern in checks:
        if pattern.search(text):
            leaks.append(name)
    return leaks


def infer_service(path: str, text: str = "") -> str:
    lower = f"{path}\n{text}".lower()
    for service in ["gateway", "deviceshadow", "pushservice", "adminservice", "cloudstorage"]:
        if service in lower:
            if service == "deviceshadow":
                return "deviceShadow"
            if service == "pushservice":
                return "pushService"
            if service == "adminservice":
                return "AdminService"
            if service == "cloudstorage":
                return "cloudStorage"
            return service
    if "/v1/device/" in lower or " route/v1/device " in lower:
        return "gateway"
    if "mqtt" in lower or "websocket" in lower or "device shadow" in lower:
        return "deviceShadow"
    if "push send" in lower or "getui" in lower:
        return "pushService"
    return Path(path).stem


def read_log_file(path: str, tail_lines: int = 800) -> LogSnapshot:
    p = Path(path).expanduser()
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[-tail_lines:] if tail_lines and len(lines) > tail_lines else lines
    content = redact_text("\n".join(selected))
    return LogSnapshot(
        path=str(p),
        service=infer_service(str(p), content),
        line_count=len(lines),
        content=content,
    )


def list_log_files(root: str | None = None) -> list[str]:
    roots = [Path(root).expanduser()] if root else DEFAULT_LOG_ROOTS
    files: list[str] = []
    for log_root in roots:
        if not log_root.exists():
            continue
        files.extend(str(path) for path in log_root.rglob("*.log") if path.is_file())
    return sorted(set(files))


def _is_normal_websocket_close(line: str) -> bool:
    lower = line.lower()
    return (
        "websocket: close 1000" in lower
        or "websocket: close 1001" in lower
        or bool(re.search(r"\bnormal closure\b", lower))
    )


def _line_level(line: str) -> str:
    if _is_normal_websocket_close(line):
        return "info"

    timestamp = TIMESTAMP_RE.search(line)
    if timestamp:
        level = re.match(r"\s*(?:\t|\s)*(debug|info|warn|warning|error|fatal|panic)\b", line[timestamp.end():], re.IGNORECASE)
        if level:
            normalized = level.group(1).lower()
            if normalized == "warn":
                return "warning"
            return normalized
    if re.search(r"(?i)\b(fatal|panic)\b", line):
        return "fatal"
    if ERROR_RE.search(line):
        return "error"
    if WARN_RE.search(line):
        return "warning"
    return "info"


def _rule_matches(rule: dict[str, Any], service: str, line: str) -> bool:
    if rule["services"] and service not in rule["services"]:
        return False

    lower = line.lower()
    if rule["type"] == "websocket_failed" and _is_normal_websocket_close(lower):
        return False
    required_keywords = rule.get("all_keywords", [])
    if any(keyword.lower() not in lower for keyword in required_keywords):
        return False

    keyword_groups = rule.get("keyword_groups", [])
    if keyword_groups and not all(
        any(keyword.lower() in lower for keyword in group)
        for group in keyword_groups
    ):
        return False

    keywords = rule.get("keywords", [])
    if keywords and not any(keyword.lower() in lower for keyword in keywords):
        return False

    return bool(required_keywords or keyword_groups or keywords)


def extract_log_facts(snapshots: list[LogSnapshot], max_evidence_per_rule: int = 6) -> dict[str, Any]:
    services: dict[str, dict[str, Any]] = {}
    all_errors: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    message_ids: set[str] = set()
    uuids: set[str] = set()
    incident_hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_error_keywords = Counter()

    for snapshot in snapshots:
        counts = Counter()
        error_messages = Counter()
        error_keywords = Counter()
        service_timeline: list[dict[str, Any]] = []

        for index, line in enumerate(snapshot.content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            level = _line_level(stripped)
            counts[level] += 1

            ts_match = TIMESTAMP_RE.search(stripped)
            timestamp = ts_match.group("ts") if ts_match else None
            msg_match = MESSAGE_ID_RE.search(stripped)
            uuid_match = UUID_RE.search(stripped)
            if msg_match:
                message_ids.add(msg_match.group("id"))
            if uuid_match:
                uuids.add(uuid_match.group("id"))

            if level in {"fatal", "error", "warning"}:
                for keyword in ERROR_RE.findall(stripped):
                    normalized_keyword = str(keyword).lower()
                    error_keywords[normalized_keyword] += 1
                    all_error_keywords[normalized_keyword] += 1
                event = {
                    "service": snapshot.service,
                    "line": index,
                    "level": level,
                    "timestamp": timestamp,
                    "message": stripped[:500],
                }
                all_errors.append(event)
                service_timeline.append(event)
                timeline.append(event)
                normalized = re.sub(r"\s+", " ", stripped)
                normalized = re.sub(r"<[^>]+>", "<REDACTED>", normalized)
                error_messages[normalized[:180]] += 1

            lower = stripped.lower()
            for rule in INCIDENT_RULES:
                if _rule_matches(rule, snapshot.service, lower):
                    if len(incident_hits[rule["type"]]) < max_evidence_per_rule:
                        incident_hits[rule["type"]].append({
                            "service": snapshot.service,
                            "line": index,
                            "level": level,
                            "message": stripped[:500],
                        })

        service_info = {
            "path": snapshot.path,
            "paths": [snapshot.path],
            "line_count": snapshot.line_count,
            "level_counts": dict(counts),
            "error_count": counts["fatal"] + counts["error"],
            "warning_count": counts["warning"],
            "top_errors": [
                {"message": msg, "count": count}
                for msg, count in error_messages.most_common(8)
            ],
            "error_keywords": [
                {"keyword": keyword, "count": count}
                for keyword, count in error_keywords.most_common(8)
            ],
            "timeline": service_timeline[:30],
        }
        if snapshot.service in services:
            existing = services[snapshot.service]
            existing.setdefault("paths", [existing.get("path", "")])
            existing["paths"].append(snapshot.path)
            existing["line_count"] += service_info["line_count"]
            merged_levels = Counter(existing.get("level_counts", {}))
            merged_levels.update(service_info["level_counts"])
            existing["level_counts"] = dict(merged_levels)
            existing["error_count"] += service_info["error_count"]
            existing["warning_count"] += service_info["warning_count"]

            merged_errors = Counter({
                item["message"]: item["count"]
                for item in existing.get("top_errors", [])
            })
            merged_errors.update({
                item["message"]: item["count"]
                for item in service_info["top_errors"]
            })
            existing["top_errors"] = [
                {"message": msg, "count": count}
                for msg, count in merged_errors.most_common(8)
            ]

            merged_keywords = Counter({
                item["keyword"]: item["count"]
                for item in existing.get("error_keywords", [])
            })
            merged_keywords.update({
                item["keyword"]: item["count"]
                for item in service_info["error_keywords"]
            })
            existing["error_keywords"] = [
                {"keyword": keyword, "count": count}
                for keyword, count in merged_keywords.most_common(8)
            ]
            existing["timeline"] = (existing.get("timeline", []) + service_info["timeline"])[:30]
        else:
            services[snapshot.service] = service_info

    incidents = []
    for rule in INCIDENT_RULES:
        evidence = incident_hits.get(rule["type"], [])
        if len(evidence) < int(rule.get("min_evidence", 1)):
            continue
        error_evidence = [item for item in evidence if item["level"] == "error"]
        fatal_evidence = [item for item in evidence if item["level"] in {"fatal", "panic"}]
        severity = "medium"
        if fatal_evidence:
            severity = "high"
        elif rule["type"] == "config_init_failed" and error_evidence:
            severity = "high"
        elif len(error_evidence) >= 3:
            severity = "high"
        incidents.append({
            "type": rule["type"],
            "title": rule["title"],
            "risk_level": severity,
            "related_services": sorted({e["service"] for e in evidence}),
            "evidence": evidence,
        })

    risk_level = "low"
    total_errors = sum(s["error_count"] for s in services.values())
    if any(i["risk_level"] == "high" for i in incidents):
        risk_level = "high"
    elif incidents or total_errors > 0:
        risk_level = "medium"

    return {
        "summary": f"分析 {len(snapshots)} 个日志文件，发现 {len(incidents)} 类可疑问题，错误数 {total_errors}",
        "risk_level": risk_level,
        "services": services,
        "error_count": total_errors,
        "error_keywords": [
            {"keyword": keyword, "count": count}
            for keyword, count in all_error_keywords.most_common(12)
        ],
        "message_ids": sorted(message_ids)[:20],
        "uuids": sorted(uuids)[:20],
        "incidents": incidents,
        "timeline": timeline[:80],
    }


def local_diagnosis(facts: dict[str, Any]) -> dict[str, Any]:
    incidents = []
    for item in facts.get("incidents", []):
        incident_type = item["type"]
        suggestions = {
            "device_login_failed": ["检查 gateway /v1/device/login 请求参数、设备 uuid 与 Redis MQTT 用户写入是否成功。"],
            "mqtt_connection_failed": ["检查 EMQX 是否可达、Redis 鉴权账号是否写入、deviceShadow MQTT 订阅是否成功。"],
            "mqtt_payload_invalid": ["保存原始 topic/payload，核对设备协议字段和 JSON 格式。"],
            "device_online_offline_flapping": ["按 uuid 聚合上下线时间线，确认网络抖动、心跳配置或设备重启。"],
            "rpc_call_failed": ["检查 gateway 到 deviceShadow RPC 地址、端口和服务存活状态。"],
            "push_failed": ["检查 pushService 队列消费、个推 token、用户推送配置和第三方 API 返回。"],
            "ota_failed": ["检查固件版本规则、设备在线状态、OTA topic 下发和设备响应。"],
            "websocket_failed": ["检查 deviceShadow WebSocket 连接保存、用户 token 和客户端连接状态。"],
            "config_init_failed": ["检查 env.yaml、工作目录、外部依赖配置和启动参数。"],
            "database_connection_failed": ["检查 MySQL/MongoDB/Redis 地址、隧道、账号和服务健康状态。"],
            "payload_type_mismatch": ["核对写入 payload 的数据结构；gateway 当前需要对象/map，不能传入字符串。"],
        }.get(incident_type, ["结合证据日志确认上游请求、依赖服务和配置是否一致。"])
        incidents.append({
            "type": incident_type,
            "risk_level": item["risk_level"],
            "related_services": item["related_services"],
            "evidence": item["evidence"],
            "possible_causes": suggestions,
            "suggestions": suggestions,
        })

    return {
        "summary": facts["summary"],
        "risk_level": facts["risk_level"],
        "incidents": incidents,
        "next_steps": [
            "先按 message_id 或 uuid 串联 gateway、deviceShadow、pushService 日志。",
            "优先处理 fatal/error，再看 warning/retry 类日志。",
            "确认脱敏日志足够覆盖故障时间窗口后再交给 Claude 复盘。",
        ],
    }


def build_diagnosis_prompt(facts: dict[str, Any], docs_context: str = "") -> str:
    return f"""你是 SL100 IoT 后端运维诊断专家。

请只返回 JSON，不要 markdown 代码块。字段必须是：
{{
  "summary": "一句话概括本次日志诊断结果",
  "risk_level": "high|medium|low",
  "incidents": [
    {{
      "type": "稳定的英文类型",
      "risk_level": "high|medium|low",
      "related_services": ["gateway|deviceShadow|pushService|AdminService|cloudStorage"],
      "evidence": ["引用脱敏日志证据，不要编造"],
      "possible_causes": ["可能原因"],
      "suggestions": ["下一步排查建议"]
    }}
  ],
  "next_steps": ["按优先级排列的排查动作"]
}}

诊断原则：
- 只能基于 facts 和 docs_context 归因，不要编造不存在的服务、日志或字段。
- 设备链路优先按 gateway -> deviceShadow -> pushService 串联。
- 如果证据不足，要明确说缺少哪些日志。
- 所有输出用中文。

docs_context:
{docs_context}

facts:
{json.dumps(facts, ensure_ascii=False, indent=2)}
"""


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def diagnose_with_claude(facts: dict[str, Any], docs_context: str = "", max_retries: int = 2) -> dict[str, Any]:
    client = init_anthropic_client()
    prompt = build_diagnosis_prompt(facts, docs_context)
    last_error: Exception | None = None
    for _ in range(max_retries + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system="你是严谨的 SL100 IoT 运维诊断助手，只输出可解析 JSON。",
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        try:
            return _extract_json(text)
        except Exception as exc:  # noqa: BLE001 - keep retry diagnostics simple for a learning project.
            last_error = exc
            prompt += f"\n\n上次返回不是合法 JSON，错误：{exc}。请重新只返回 JSON。"
    raise RuntimeError(f"Claude 返回 JSON 解析失败: {last_error}")


def render_report(result: dict[str, Any]) -> str:
    lines = [
        "=" * 60,
        "SL100 日志诊断报告",
        "=" * 60,
        f"风险等级: {result.get('risk_level', 'unknown')}",
        f"总结: {result.get('summary', '')}",
        "",
    ]
    incidents = result.get("incidents", [])
    if not incidents:
        lines.append("未发现明确异常。")
    for index, incident in enumerate(incidents, start=1):
        lines.append(f"{index}. {incident.get('type', 'unknown')} [{incident.get('risk_level', 'unknown')}]")
        services = ", ".join(incident.get("related_services", []))
        if services:
            lines.append(f"   相关服务: {services}")
        evidence = incident.get("evidence", [])
        if evidence:
            lines.append("   证据:")
            for item in evidence[:5]:
                if isinstance(item, dict):
                    msg = item.get("message", "")
                    svc = item.get("service", "")
                    line = item.get("line", "")
                    lines.append(f"   - {svc}:{line} {msg}")
                else:
                    lines.append(f"   - {item}")
        causes = incident.get("possible_causes", [])
        if causes:
            lines.append("   可能原因:")
            lines.extend(f"   - {item}" for item in causes[:5])
        suggestions = incident.get("suggestions", [])
        if suggestions:
            lines.append("   建议:")
            lines.extend(f"   - {item}" for item in suggestions[:5])
        lines.append("")
    next_steps = result.get("next_steps", [])
    if next_steps:
        lines.append("下一步:")
        lines.extend(f"- {item}" for item in next_steps)
    return "\n".join(lines)


def _doc_query_terms(query: str) -> list[str]:
    lower = query.lower()
    terms = {
        term.lower()
        for term in re.findall(r"[A-Za-z0-9_./:-]+", query)
        if len(term) > 1
    }
    for term in DOC_DOMAIN_TERMS:
        if term.lower() in lower:
            terms.add(term.lower())
    for term in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        terms.add(term.lower())
        if len(term) > 4:
            terms.update(term[i:i + 2].lower() for i in range(len(term) - 1))
    return sorted(terms)


def _markdown_chunks(path: Path, max_chars: int = 1600) -> list[dict[str, str]]:
    text = redact_text(path.read_text(encoding="utf-8", errors="replace"))
    chunks: list[dict[str, str]] = []
    title = path.name
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if not body:
            buffer = []
            return
        while len(body) > max_chars:
            part = body[:max_chars]
            split_at = max(part.rfind("\n\n"), part.rfind("\n"))
            if split_at < max_chars // 2:
                split_at = max_chars
            chunks.append({"title": title, "text": body[:split_at].strip()})
            body = body[split_at:].strip()
        if body:
            chunks.append({"title": title, "text": body})
        buffer = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            title = stripped.lstrip("#").strip() or path.name
            buffer.append(stripped)
            continue
        buffer.append(line)
    flush()
    return chunks


def search_docs(query: str, doc_paths: list[str] | None = None, max_chunks: int = 5) -> list[dict[str, str]]:
    paths = [Path(p).expanduser() for p in doc_paths] if doc_paths else DEFAULT_DOC_PATHS
    terms = _doc_query_terms(query)
    chunks: list[dict[str, str]] = []
    for path in paths:
        if not path.exists():
            continue
        for idx, chunk in enumerate(_markdown_chunks(path)):
            text = chunk["text"]
            title = chunk["title"]
            lower = text.lower()
            title_lower = title.lower()
            path_lower = path.name.lower()
            score = sum(lower.count(term) for term in terms)
            score += sum(title_lower.count(term) * 4 for term in terms)
            score += sum(path_lower.count(term) * 2 for term in terms)
            if query.lower() in lower:
                score += 8
            if score > 0:
                chunks.append({
                    "path": str(path),
                    "source": path.name,
                    "chunk": str(idx),
                    "title": title,
                    "score": str(score),
                    "terms": ", ".join(terms[:20]),
                    "text": text[:1600],
                })
    chunks.sort(key=lambda item: int(item["score"]), reverse=True)
    return chunks[:max_chunks]


def docs_context_for_query(query: str, max_chunks: int = 5) -> str:
    chunks = search_docs(query, max_chunks=max_chunks)
    return "\n\n".join(
        f"[{chunk['source']}#{chunk['chunk']} {chunk['title']} score={chunk['score']}]\n{chunk['text']}"
        for chunk in chunks
    )


def extract_rule_facts_from_paths(paths: list[str], tail_lines: int = 800) -> dict[str, Any]:
    snapshots = [read_log_file(path, tail_lines=tail_lines) for path in paths]
    combined_text = "\n".join(snapshot.content for snapshot in snapshots)
    leaks = assert_redacted(combined_text)
    if leaks:
        raise RuntimeError(f"脱敏后仍疑似包含敏感信息: {', '.join(leaks)}")
    return extract_log_facts(snapshots)


def analyze_paths(paths: list[str], tail_lines: int = 800, use_ai: bool = True, docs_query: str = "") -> dict[str, Any]:
    facts = extract_rule_facts_from_paths(paths, tail_lines=tail_lines)
    if not use_ai:
        return local_diagnosis(facts)
    docs_context = docs_context_for_query(docs_query or "SL100 gateway deviceShadow pushService MQTT 设备登录 日志")
    return diagnose_with_claude(facts, docs_context=docs_context)
