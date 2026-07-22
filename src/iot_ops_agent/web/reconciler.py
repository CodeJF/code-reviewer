"""Recover durable database jobs that were not accepted by Redis/RQ."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.db import make_session_factory
from iot_ops_agent.web.models import DiagnosisJob, DiagnosisStatus, NotificationDelivery, utcnow
from iot_ops_agent.web.tasks import enqueue_diagnosis, enqueue_notification


def reconcile_once(
    session_factory: sessionmaker[Session],
    *,
    enqueue_diagnosis_fn: Callable[[str], None] = enqueue_diagnosis,
    enqueue_notification_fn: Callable[[str], None] = enqueue_notification,
) -> dict[str, int]:
    queued_diagnoses = 0
    queued_notifications = 0
    now = utcnow()
    with session_factory() as session:
        diagnoses = session.scalars(
            select(DiagnosisJob)
            .where(
                or_(
                    DiagnosisJob.status == DiagnosisStatus.QUEUED,
                    (DiagnosisJob.status == DiagnosisStatus.RUNNING) & (DiagnosisJob.updated_at <= now - timedelta(minutes=10)),
                )
            )
            .order_by(DiagnosisJob.created_at)
            .limit(100)
        ).all()
        notifications = session.scalars(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.status == "queued",
                NotificationDelivery.attempts < 4,
                or_(NotificationDelivery.next_attempt_at.is_(None), NotificationDelivery.next_attempt_at <= now),
            )
            .order_by(NotificationDelivery.created_at)
            .limit(100)
        ).all()
        for job in diagnoses:
            if job.status == DiagnosisStatus.RUNNING:
                job.status = DiagnosisStatus.QUEUED
                job.error_text = "检测到执行中断，恢复服务已重新排队"
        session.commit()
        for job in diagnoses:
            try:
                enqueue_diagnosis_fn(job.id)
                job.error_text = ""
                queued_diagnoses += 1
            except Exception:
                job.error_text = "队列仍不可用，恢复服务将在一分钟后重试"
        for delivery in notifications:
            try:
                enqueue_notification_fn(delivery.id)
                delivery.status = "queued"
                queued_notifications += 1
            except Exception:
                delivery.error_text = "通知队列仍不可用，恢复服务将在一分钟后重试"
        session.commit()
    return {"diagnoses": queued_diagnoses, "notifications": queued_notifications}


def main() -> None:
    settings = TeamSettings.from_env()
    session_factory = make_session_factory(settings)
    while True:
        reconcile_once(session_factory)
        time.sleep(60)


if __name__ == "__main__":
    main()
