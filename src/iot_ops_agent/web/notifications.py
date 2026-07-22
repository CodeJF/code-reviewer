"""Feishu notification delivery without embedding secrets in the database."""
from __future__ import annotations

import json
import urllib.request
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from iot_ops_agent.diagnosis.log_core import redact_text
from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.models import Incident, NotificationDelivery, utcnow


def feishu_payload(incident: Incident, *, event_type: str, app_url: str) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"SL100 事件 · {incident.risk_level}"}, "template": "red" if incident.risk_level == "high" else "orange"},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**{incident.title}**\n服务：`{incident.service or '未识别'}`\n状态：`{incident.status.value}`\n事件：`{event_type}`"}},
                {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "打开工作台"}, "type": "primary", "url": f"{app_url}/#incident-{incident.id}"}]},
            ],
        },
    }


def deliver_feishu(session: Session, *, settings: TeamSettings, delivery: NotificationDelivery, incident: Incident) -> None:
    if not settings.feishu_webhook_url:
        delivery.status = "skipped"
        delivery.error_text = "飞书通知未启用"
        delivery.next_attempt_at = None
        session.commit()
        return
    body = json.dumps(feishu_payload(incident, event_type=delivery.event_type, app_url=settings.app_url), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(settings.feishu_webhook_url, data=body, headers={"Content-Type": "application/json"})
    delivery.attempts += 1
    try:
        with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - webhook URL is admin-only config.
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code", 0) != 0:
            raise RuntimeError(payload.get("msg", "Feishu webhook rejected request"))
        delivery.status = "delivered"
        delivery.delivered_at = utcnow()
        delivery.error_text = ""
        delivery.next_attempt_at = None
    except Exception as exc:  # noqa: BLE001 - retry is handled by the queue worker.
        delivery.error_text = redact_text(str(exc))[:500]
        delays = (5, 30, 120)
        if delivery.attempts <= len(delays):
            delivery.status = "retrying"
            delivery.next_attempt_at = utcnow() + timedelta(seconds=delays[delivery.attempts - 1])
        else:
            delivery.status = "failed"
            delivery.next_attempt_at = None
        raise
    finally:
        session.commit()
