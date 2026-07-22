"""Domain operations shared by the HTTP API and background worker."""
from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from iot_ops_agent.diagnosis.diagnose import diagnose
from iot_ops_agent.diagnosis.log_core import assert_redacted, redact_text
from iot_ops_agent.web.agent_runtime import build_approved_plan, run_controlled_agent
from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.models import (
    AgentToolCall,
    AuditEvent,
    DiagnosisFeedback,
    DiagnosisJob,
    DiagnosisStatus,
    Incident,
    IncidentComment,
    IncidentStatus,
    InviteToken,
    LoginAudit,
    PasswordResetToken,
    Role,
    User,
    UserSession,
    utcnow,
)


def ensure_user(
    session: Session,
    *,
    subject: str,
    email: str,
    display_name: str,
    role: Role,
) -> User:
    dev_subject = f"dev:{subject}"
    username = f"dev-{subject}".lower()[:64]
    user = session.scalar(select(User).where(User.subject == dev_subject))
    if user is None:
        user = User(subject=dev_subject, username=username, email=email, display_name=display_name, role=role)
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


def create_diagnosis(
    session: Session,
    *,
    actor: User,
    query: str,
    no_remote: bool,
    ttl_days: int,
    retry_of_id: str | None = None,
    planned: bool = False,
    settings: TeamSettings | None = None,
) -> DiagnosisJob:
    safe_query = redact_text(query.strip())
    plan = build_approved_plan(safe_query, no_remote=no_remote, settings=settings) if planned and settings else None
    job = DiagnosisJob(
        created_by_id=actor.id,
        query=safe_query,
        no_remote=no_remote,
        retry_of_id=retry_of_id,
        plan_json=plan,
        plan_digest=(plan or {}).get("digest", ""),
        status=DiagnosisStatus.PLANNED if planned else DiagnosisStatus.QUEUED,
        expires_at=utcnow() + timedelta(days=ttl_days),
    )
    session.add(job)
    session.flush()
    audit(
        session,
        actor_id=actor.id,
        action="diagnosis.retried" if retry_of_id else "diagnosis.created",
        target_type="diagnosis",
        target_id=job.id,
        metadata={"retry_of_id": retry_of_id} if retry_of_id else None,
    )
    session.commit()
    session.refresh(job)
    return job


def approve_diagnosis(
    session: Session,
    *,
    actor: User,
    job: DiagnosisJob,
    execution_mode: str,
    external_ai_consent: bool,
    settings: TeamSettings,
) -> DiagnosisJob:
    if job.status != DiagnosisStatus.PLANNED:
        raise ValueError("only a planned diagnosis can be executed")
    if execution_mode not in {"rules", "ai_assisted"}:
        raise ValueError("execution mode must be rules or ai_assisted")
    if execution_mode == "ai_assisted":
        if not settings.ai_assisted_enabled:
            raise ValueError("AI-assisted diagnosis is not enabled")
        if not external_ai_consent:
            raise ValueError("AI-assisted diagnosis requires explicit consent")
        job.external_ai_approved_by_id = actor.id
        job.external_ai_approved_at = utcnow()
        job.model_id = settings.agent_model_id
        job.prompt_version = settings.agent_prompt_version
    job.execution_mode = execution_mode
    job.status = DiagnosisStatus.QUEUED
    audit(
        session,
        actor_id=actor.id,
        action="diagnosis.approved",
        target_type="diagnosis",
        target_id=job.id,
        metadata={"execution_mode": execution_mode, "plan_digest": job.plan_digest},
    )
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
    agent_fn: Callable[..., dict[str, Any]] = run_controlled_agent,
    settings: TeamSettings | None = None,
) -> None:
    """Run an existing safe diagnosis pipeline and persist only its redacted report."""
    with session_factory() as session:
        job = session.get(DiagnosisJob, job_id)
        if not job or job.status != DiagnosisStatus.QUEUED:
            return
        job.status = DiagnosisStatus.RUNNING
        job.started_at = utcnow()
        audit(session, actor_id=job.created_by_id, action="diagnosis.started", target_type="diagnosis", target_id=job.id)
        session.commit()
        query = job.query
        no_remote = job.no_remote
        execution_mode = job.execution_mode
        plan = job.plan_json

    started = time.monotonic()
    tool_calls: list[dict[str, Any]] = []
    execution: dict[str, Any] = {}
    try:
        if execution_mode == "ai_assisted":
            runtime_settings = settings or TeamSettings.from_env()
            if not plan:
                plan = build_approved_plan(query, no_remote=no_remote, settings=runtime_settings)
            try:
                result = agent_fn(query, plan=plan, settings=runtime_settings)
                report = result["report"]
                tool_calls = list(result.get("tool_calls", []))
                execution = dict(result.get("execution", {}))
            except Exception as exc:  # noqa: BLE001 - AI failure must preserve deterministic diagnosis.
                report = diagnosis_fn(query, no_remote=no_remote)
                execution = {
                    "mode": "ai_assisted",
                    "status": "fallback",
                    "reason": redact_text(str(exc))[:500],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
                report["agent_execution"] = execution
        else:
            report = diagnosis_fn(query, no_remote=no_remote)
            execution = {
                "mode": "rules",
                "status": "completed",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            report["agent_execution"] = execution
        report = redact_report_value(report)
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
            job.report_schema_version = str(report.get("schema_version", "1.0"))
            job.result_status = str(report.get("result_status", ""))[:32]
            job.duration_ms = int(execution.get("duration_ms", (time.monotonic() - started) * 1000))
            job.input_tokens = int(execution.get("input_tokens", 0))
            job.output_tokens = int(execution.get("output_tokens", 0))
            job.tool_call_count = len(tool_calls)
            for item in tool_calls:
                session.add(AgentToolCall(
                    diagnosis_id=job.id,
                    sequence=int(item.get("sequence", 0)),
                    tool_name=str(item.get("tool_name", ""))[:100],
                    arguments_json=redact_report_value(item.get("arguments", {})),
                    evidence_refs_json=list(item.get("evidence_refs", []))[:20],
                    status=str(item.get("status", "completed"))[:32],
                    duration_ms=int(item.get("duration_ms", 0)),
                    error_text=redact_text(str(item.get("error", "")))[:500],
                ))
            job.completed_at = utcnow()
            audit(session, actor_id=job.created_by_id, action="diagnosis.completed", target_type="diagnosis", target_id=job.id)
            session.commit()


def record_diagnosis_feedback(
    session: Session,
    *,
    actor: User,
    diagnosis: DiagnosisJob,
    rating: str,
    evidence_correct: bool | None,
    corrected_incident_types: list[str],
    note: str,
) -> DiagnosisFeedback:
    if diagnosis.status != DiagnosisStatus.COMPLETED:
        raise ValueError("feedback is only available for a completed diagnosis")
    if rating not in {"useful", "partial", "not_useful"}:
        raise ValueError("invalid feedback rating")
    feedback = session.scalar(
        select(DiagnosisFeedback).where(
            DiagnosisFeedback.diagnosis_id == diagnosis.id,
            DiagnosisFeedback.user_id == actor.id,
        )
    )
    if feedback is None:
        feedback = DiagnosisFeedback(diagnosis_id=diagnosis.id, user_id=actor.id, rating=rating)
        session.add(feedback)
    feedback.rating = rating
    feedback.evidence_correct = evidence_correct
    feedback.corrected_incident_types_json = list(dict.fromkeys(corrected_incident_types))[:12]
    feedback.note = redact_text(note.strip())[:2000]
    audit(
        session,
        actor_id=actor.id,
        action="diagnosis.feedback_recorded",
        target_type="diagnosis",
        target_id=diagnosis.id,
        metadata={"rating": rating, "evidence_correct": evidence_correct},
    )
    session.commit()
    session.refresh(feedback)
    return feedback


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
        title=redact_text(title.strip() or diagnosis.query[:300]),
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


def transition_incident(
    session: Session,
    *,
    actor: User,
    incident: Incident,
    status: IncidentStatus,
    assignee_id: str | None,
    assign: bool = False,
) -> Incident:
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
    if assign or assignee_id is not None:
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
    tool_calls = session.scalars(select(AgentToolCall).where(AgentToolCall.expires_at <= now)).all()
    for tool_call in tool_calls:
        session.delete(tool_call)
    audit_events = session.scalars(select(AuditEvent).where(AuditEvent.expires_at <= now)).all()
    for event in audit_events:
        session.delete(event)
    login_audits = session.scalars(select(LoginAudit).where(LoginAudit.expires_at <= now)).all()
    for event in login_audits:
        session.delete(event)
    sessions = session.scalars(select(UserSession).where(UserSession.expires_at <= now)).all()
    for user_session in sessions:
        session.delete(user_session)
    token_cutoff = now - timedelta(days=30)
    invites = session.scalars(select(InviteToken).where(InviteToken.expires_at <= token_cutoff)).all()
    for invite in invites:
        session.delete(invite)
    resets = session.scalars(select(PasswordResetToken).where(PasswordResetToken.expires_at <= token_cutoff)).all()
    for reset in resets:
        session.delete(reset)
    session.commit()
    return {
        "reports_expired": len(jobs),
        "comments_deleted": len(comments),
        "tool_calls_deleted": len(tool_calls),
        "audit_events_deleted": len(audit_events),
        "login_audits_deleted": len(login_audits),
        "sessions_deleted": len(sessions),
        "tokens_deleted": len(invites) + len(resets),
    }
