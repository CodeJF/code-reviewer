"""Canonical incident types used by diagnostics, reviews, and quality evals.

The machine-readable key is stored in reviewed cases.  The Chinese label is
shown to the reviewer, so a human never needs to invent a free-form type.
"""
from __future__ import annotations


INCIDENT_TYPE_CATALOG: tuple[tuple[str, str], ...] = (
    ("device_login_failed", "设备登录失败"),
    ("mqtt_connection_failed", "MQTT 连接失败"),
    ("mqtt_payload_invalid", "MQTT 消息格式不合法"),
    ("device_online_offline_flapping", "设备频繁上下线"),
    ("rpc_call_failed", "服务间 RPC 调用失败"),
    ("push_failed", "推送失败"),
    ("ota_failed", "OTA 升级失败"),
    ("websocket_failed", "WebSocket 连接异常"),
    ("config_init_failed", "配置初始化失败"),
    ("database_connection_failed", "数据库或 Redis 连接/认证失败"),
    ("payload_type_mismatch", "Payload 数据类型不匹配"),
)

INCIDENT_TYPE_LABELS = dict(INCIDENT_TYPE_CATALOG)
INCIDENT_TYPE_KEYS = frozenset(INCIDENT_TYPE_LABELS)


def incident_type_label(incident_type: str) -> str:
    """Return a display label without making an unknown value look valid."""
    return INCIDENT_TYPE_LABELS.get(incident_type, incident_type)
