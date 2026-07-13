"""Durable background jobs for diagnosis and notification delivery."""
from __future__ import annotations

from team_app.config import TeamSettings
from team_app.db import make_session_factory
from team_app.models import Incident, NotificationDelivery
from team_app.notifications import deliver_feishu
from team_app.services import run_diagnosis_job


def _enqueue_unique(queue_name: str, function: str, record_id: str, *, timeout: int, retry=None) -> None:
    from redis import Redis
    from rq import Queue
    from rq.exceptions import NoSuchJobError
    from rq.job import Job, JobStatus

    settings = TeamSettings.from_env()
    connection = Redis.from_url(settings.redis_url)
    job_id = f"{queue_name}-{record_id}"
    try:
        existing = Job.fetch(job_id, connection=connection)
        current_status = existing.get_status(refresh=True)
        if current_status in {JobStatus.QUEUED, JobStatus.STARTED, JobStatus.DEFERRED, JobStatus.SCHEDULED}:
            return
        existing.delete()
    except NoSuchJobError:
        pass
    Queue(queue_name, connection=connection).enqueue(
        function,
        record_id,
        job_id=job_id,
        job_timeout=timeout,
        retry=retry,
    )


def enqueue_diagnosis(job_id: str) -> None:
    _enqueue_unique("diagnosis", "team_app.tasks.execute_diagnosis", job_id, timeout=180)


def execute_diagnosis(job_id: str) -> None:
    settings = TeamSettings.from_env()
    session_factory = make_session_factory(settings)
    try:
        run_diagnosis_job(session_factory, job_id)
    finally:
        session_factory.kw["bind"].dispose()


def enqueue_notification(delivery_id: str) -> None:
    from rq import Retry

    _enqueue_unique(
        "notifications",
        "team_app.tasks.execute_notification",
        delivery_id,
        timeout=30,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )


def execute_notification(delivery_id: str) -> None:
    settings = TeamSettings.from_env()
    session_factory = make_session_factory(settings)
    try:
        with session_factory() as session:
            delivery = session.get(NotificationDelivery, delivery_id)
            if not delivery or delivery.status == "delivered":
                return
            incident = session.get(Incident, delivery.incident_id)
            if not incident:
                return
            deliver_feishu(session, settings=settings, delivery=delivery, incident=incident)
    finally:
        session_factory.kw["bind"].dispose()
