"""Durable background jobs for diagnosis and notification delivery."""
from __future__ import annotations

from team_app.config import TeamSettings
from team_app.db import make_session_factory
from team_app.models import Incident, NotificationDelivery
from team_app.notifications import deliver_feishu
from team_app.services import run_diagnosis_job


def enqueue_diagnosis(job_id: str) -> None:
    from redis import Redis
    from rq import Queue, Retry

    settings = TeamSettings.from_env()
    Queue("diagnosis", connection=Redis.from_url(settings.redis_url)).enqueue("team_app.tasks.execute_diagnosis", job_id, job_timeout=180)


def execute_diagnosis(job_id: str) -> None:
    settings = TeamSettings.from_env()
    run_diagnosis_job(make_session_factory(settings), job_id)


def enqueue_notification(delivery_id: str) -> None:
    from redis import Redis
    from rq import Queue

    settings = TeamSettings.from_env()
    Queue("notifications", connection=Redis.from_url(settings.redis_url)).enqueue(
        "team_app.tasks.execute_notification", delivery_id, job_timeout=30, retry=Retry(max=2, interval=[5, 30]),
    )


def execute_notification(delivery_id: str) -> None:
    settings = TeamSettings.from_env()
    session_factory = make_session_factory(settings)
    with session_factory() as session:
        delivery = session.get(NotificationDelivery, delivery_id)
        if not delivery or delivery.status == "delivered":
            return
        incident = session.get(Incident, delivery.incident_id)
        if not incident:
            return
        deliver_feishu(session, settings=settings, delivery=delivery, incident=incident)
