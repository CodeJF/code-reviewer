"""Domain operations shared by the HTTP API and background worker."""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sl100_diagnose import diagnose
from sl100_log_core import assert_redacted, redact_text
from team_app.config import TeamSettings
from team_app.models import (
    AuditEvent,
    DiagnosisJob,
    DiagnosisStatus,
    Incident,
    IncidentComment,
    IncidentStatus,
    Role,
    User,
    utcnow,
)


def role_from_groups(settings: TeamSettings, groups: list[str]) -> Role:
    group_set = set(groups)
    if group_set & settings.admin_groups:
        return Role.ADMIN
    if group_set & settings.oncall_groups:
        return Role.ONCALL
    return Role.VIEWER


def ensure_user(
    session: Session,
    *,
    subject: str,
    email: str,
    display_name: str,
    role: Role,
) -> User:
    user = session.scalar(select(User).where(User.subject == subject))
    if user is None:
        user = User(subject=subject, email=email, display_name=display_name, role=role)
        session.add(user)
    else:
        user.email = email or user.email
        user.display_name = display_name or user.display_name
        user.role = role
    session.commit()
    session.refresh(user)
    return user


def audit(session: Session, *, actor_id: str | None, action: str, target_type: str, target_id: str, metadata: dict[str, Any] | None = None) -> None:
    session.add(AuditEvent(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata_json=metadata or {},
    ))


def create_diagnosis(session: Session, *, actor: User, query: str, no_remote: bool, ttl_days: int) -> DiagnosisJob:
    job = DiagnosisJob(
        created_by_id=actor.id,
        query=query.strip(),
        no_remote=no_remote,
        expires_at=utcnow() + timedelta(days=ttl_days),
    )
    session.add(job)
    session.flush()
    audit(session, actor_id=actor.id, action="diagnosis.created", target_type="diagnosis", target_id=job.id)
    session.commit()
    session.refresh(job)
    return job


def _assert_safe_report(report: dict[str, Any]) -> None:
    serialized = json.dumps(report, ensure_ascii=False)
    leaks = assert_redacted(serialized)
    if leaks:
        raise RuntimeError(f"diagnosis report failed redaction validation: {', '.join(leaks)}")


def redact_report_value(value: Any) -> Any:
    """Apply the storage boundary redaction recursively without changing report shape."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_report_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_report_value(item) for key, item in value.items()}
    return value


def run_diagnosis_job(
    session_factory: sessionmaker[Session],
    job_id: str,
    diagnosis_fn: Callable[..., dict[str, Any]] = diagnose,
) -> None:
    """Run an existing safe diagnosis pipeline and persist only its redacted report."""
    with session_factory() as session:
        job = session.get(DiagnosisJob, job_id)
        if not job or job.status != DiagnosisStatus.QUEUED:
            return
        job.status = DiagnosisStatus.RUNNING
        audit(session, actor_id=job.created_by_id, action="diagnosis.started", target_type="diagnosis", target_id=job.id)
        session.commit()
        query = job.query
        no_remote = job.no_remote

    try:
        report = redact_report_value(diagnosis_fn(query, no_remote=no_remote))
        if not isinstance(report, dict):
            raise RuntimeError("diagnosis pipeline returned a non-object report")
        _assert_safe_report(report)
    except Exception as exc:  # noqa: BLE001 - the UI must receive a safe failure state.
        with session_factory() as session:
            job = session.get(DiagnosisJob, job_id)
            if job:
                job.status = DiagnosisStatus.FAILED
                job.error_text = redact_text(str(exc))[:500]
                job.completed_at = utcnow()
                audit(session, actor_id=job.created_by_id, action="diagnosis.failed", target_type="diagnosis", target_id=job.id)
                session.commit()
        return

    with session_factory() as session:
        job = session.get(DiagnosisJob, job_id)
        if job:
            job.status = DiagnosisStatus.COMPLETED
            job.report_json = report
            job.completed_at = utcnow()
            audit(session, actor_id=job.created_by_id, action="diagnosis.completed", target_type="diagnosis", target_id=job.id)
            session.commit()


def create_incident_from_diagnosis(session: Session, *, actor: User, diagnosis: DiagnosisJob, title: str) -> Incident:
    if diagnosis.status != DiagnosisStatus.COMPLETED or not diagnosis.report_json:
        raise ValueError("only a completed diagnosis can be promoted to an incident")
    existing = session.scalar(select(Incident).where(Incident.diagnosis_id == diagnosis.id))
    if existing:
        return existing
    report = diagnosis.report_json
    services = report.get("services") or []
    incident = Incident(
        diagnosis_id=diagnosis.id,
        title=title.strip() or diagnosis.query[:300],
        service=services[0] if services else "",
        risk_level=report.get("risk_level", "unknown"),
        created_by_id=actor.id,
        assignee_id=actor.id,
    )
    session.add(incident)
    session.flush()
    audit(session, actor_id=actor.id, action="incident.created", target_type="incident", target_id=incident.id, metadata={"diagnosis_id": diagnosis.id})
    session.commit()
    session.refresh(incident)
    return incident


def transition_incident(session: Session, *, actor: User, incident: Incident, status: IncidentStatus, assignee_id: str | None) -> Incident:
    allowed = {
        IncidentStatus.OPEN: {IncidentStatus.INVESTIGATING, IncidentStatus.RESOLVED},
        IncidentStatus.INVESTIGATING: {IncidentStatus.MITIGATED, IncidentStatus.RESOLVED},
        IncidentStatus.MITIGATED: {IncidentStatus.RESOLVED, IncidentStatus.INVESTIGATING},
        IncidentStatus.RESOLVED: {IncidentStatus.INVESTIGATING},
    }
    if status != incident.status and status not in allowed[incident.status]:
        raise ValueError(f"invalid incident transition: {incident.status.value} -> {status.value}")
    previous_status = incident.status
    incident.status = status
    if assignee_id is not None:
        incident.assignee_id = assignee_id
    incident.resolved_at = utcnow() if status == IncidentStatus.RESOLVED else None
    audit(
        session,
        actor_id=actor.id,
        action="incident.updated",
        target_type="incident",
        target_id=incident.id,
        metadata={"from": previous_status.value, "to": status.value, "assignee_id": incident.assignee_id},
    )
    session.commit()
    session.refresh(incident)
    return incident


def add_comment(session: Session, *, actor: User, incident: Incident, body: str, ttl_days: int) -> IncidentComment:
    safe_body = redact_text(body.strip())
    if not safe_body:
        raise ValueError("comment cannot be empty")
    comment = IncidentComment(
        incident_id=incident.id,
        author_id=actor.id,
        body=safe_body,
        expires_at=utcnow() + timedelta(days=ttl_days),
    )
    session.add(comment)
    session.flush()
    audit(session, actor_id=actor.id, action="incident.commented", target_type="incident", target_id=incident.id, metadata={"comment_id": comment.id})
    session.commit()
    session.refresh(comment)
    return comment


def purge_expired_data(session: Session) -> dict[str, int]:
    now = utcnow()
    jobs = session.scalars(select(DiagnosisJob).where(DiagnosisJob.expires_at <= now, DiagnosisJob.report_json.is_not(None))).all()
    for job in jobs:
        job.report_json = None
        job.error_text = "expired by retention policy"
        job.status = DiagnosisStatus.EXPIRED
    comments = session.scalars(select(IncidentComment).where(IncidentComment.expires_at <= now)).all()
    for comment in comments:
        session.delete(comment)
    audit_events = session.scalars(select(AuditEvent).where(AuditEvent.expires_at <= now)).all()
    for event in audit_events:
        session.delete(event)
    session.commit()
    return {"reports_expired": len(jobs), "comments_deleted": len(comments), "audit_events_deleted": len(audit_events)}
