"""FastAPI workspace for team diagnosis and incident collaboration."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Callable

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from team_app.config import TeamSettings
from team_app.db import initialize_database, make_session_factory
from team_app.models import (
    AuditEvent,
    DiagnosisJob,
    DiagnosisStatus,
    Incident,
    IncidentComment,
    IncidentStatus,
    NotificationDelivery,
    Role,
    User,
)
from team_app.services import (
    add_comment,
    create_diagnosis,
    create_incident_from_diagnosis,
    ensure_user,
    role_from_groups,
    transition_incident,
)
from team_app.tasks import enqueue_diagnosis, enqueue_notification


STATIC_DIR = Path(__file__).with_name("static")


class DiagnosisCreate(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    no_remote: bool = False


class IncidentCreate(BaseModel):
    diagnosis_id: str
    title: str = Field(default="", max_length=300)


class IncidentPatch(BaseModel):
    status: IncidentStatus | None = None
    assign_to_me: bool = False


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


def user_dict(user: User) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "role": user.role.value}


def diagnosis_dict(job: DiagnosisJob, *, include_report: bool = False) -> dict[str, Any]:
    payload = {
        "id": job.id,
        "query": job.query,
        "no_remote": job.no_remote,
        "status": job.status.value,
        "error": job.error_text,
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "expires_at": job.expires_at.isoformat(),
    }
    if include_report:
        payload["report"] = job.report_json
    return payload


def incident_dict(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "diagnosis_id": incident.diagnosis_id,
        "title": incident.title,
        "service": incident.service,
        "risk_level": incident.risk_level,
        "status": incident.status.value,
        "assignee_id": incident.assignee_id,
        "created_at": incident.created_at.isoformat(),
        "updated_at": incident.updated_at.isoformat(),
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
    }


def create_app(
    settings: TeamSettings | None = None,
    *,
    enqueue_diagnosis_fn: Callable[[str], None] = enqueue_diagnosis,
    enqueue_notification_fn: Callable[[str], None] = enqueue_notification,
) -> FastAPI:
    settings = settings or TeamSettings.from_env()
    session_factory = make_session_factory(settings)
    oauth = OAuth()
    if settings.auth_mode == "oidc" and settings.oidc_discovery_url:
        oauth.register(
            name="oidc",
            server_metadata_url=settings.oidc_discovery_url,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            client_kwargs={"scope": "openid profile email"},
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate_runtime()
        initialize_database(session_factory)
        yield

    app = FastAPI(title="SL100 Team Ops", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.is_production,
        same_site="lax",
        max_age=8 * 60 * 60,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.oauth = oauth
    app.state.enqueue_diagnosis = enqueue_diagnosis_fn
    app.state.enqueue_notification = enqueue_notification_fn

    def database_session(request: Request):
        session = request.app.state.session_factory()
        try:
            yield session
        finally:
            session.close()

    def current_user(request: Request) -> User:
        with session_factory() as session:
            if settings.auth_mode == "dev":
                raw_role = request.headers.get("X-Dev-Role", "admin").lower()
                try:
                    role = Role(raw_role)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="invalid X-Dev-Role") from exc
                return ensure_user(
                    session,
                    subject=request.headers.get("X-Dev-User", "local-admin"),
                    email=request.headers.get("X-Dev-Email", "local-admin@example.invalid"),
                    display_name=request.headers.get("X-Dev-Name", "本地管理员"),
                    role=role,
                )
            user_id = request.session.get("user_id")
            if not user_id:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
            user = session.get(User, user_id)
            if not user:
                request.session.clear()
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login session expired")
            return user

    def require_role(*allowed: Role):
        def guard(request: Request) -> User:
            user = current_user(request)
            if user.role not in allowed:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
            return user
        return guard

    def queue_notification(session: Session, incident: Incident, event_type: str) -> None:
        delivery = NotificationDelivery(incident_id=incident.id, event_type=event_type)
        session.add(delivery)
        session.commit()
        try:
            app.state.enqueue_notification(delivery.id)
        except Exception:  # Keep a durable queued delivery for worker recovery.
            pass

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.app_env}

    @app.get("/api/auth/login")
    async def login(request: Request):
        if settings.auth_mode == "dev":
            return RedirectResponse("/#local-mode")
        if not settings.oidc_discovery_url:
            raise HTTPException(status_code=503, detail="OIDC is not configured")
        callback = f"{settings.app_url}/api/auth/callback"
        return await oauth.oidc.authorize_redirect(request, callback)

    @app.get("/api/auth/callback")
    async def callback(request: Request):
        if settings.auth_mode != "oidc":
            raise HTTPException(status_code=404, detail="OIDC disabled")
        token = await oauth.oidc.authorize_access_token(request)
        userinfo = token.get("userinfo") or {}
        if not isinstance(userinfo, dict):
            raise HTTPException(status_code=401, detail="OIDC response has invalid user information")
        subject = str(userinfo.get("sub", ""))
        if not subject:
            raise HTTPException(status_code=401, detail="OIDC response missing subject")
        raw_groups = userinfo.get(settings.oidc_groups_claim, [])
        groups = raw_groups if isinstance(raw_groups, list) else [str(raw_groups)]
        with session_factory() as session:
            user = ensure_user(
                session,
                subject=subject,
                email=str(userinfo.get("email", "")),
                display_name=str(userinfo.get("name") or userinfo.get("preferred_username") or ""),
                role=role_from_groups(settings, [str(group) for group in groups]),
            )
        request.session["user_id"] = user.id
        return RedirectResponse("/")

    @app.post("/api/auth/logout")
    def logout(request: Request) -> dict[str, bool]:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)) -> dict[str, Any]:
        return {"user": user_dict(user), "auth_mode": settings.auth_mode}

    @app.get("/api/diagnoses")
    def list_diagnoses(_: User = Depends(current_user), session: Session = Depends(database_session)) -> dict[str, list[dict[str, Any]]]:
        jobs = session.scalars(select(DiagnosisJob).order_by(DiagnosisJob.created_at.desc()).limit(50)).all()
        return {"items": [diagnosis_dict(job) for job in jobs]}

    @app.post("/api/diagnoses", status_code=status.HTTP_202_ACCEPTED)
    def create_diagnosis_endpoint(payload: DiagnosisCreate, user: User = Depends(require_role(Role.ADMIN, Role.ONCALL)), session: Session = Depends(database_session)) -> dict[str, Any]:
        job = create_diagnosis(session, actor=user, query=payload.query, no_remote=payload.no_remote, ttl_days=settings.diagnosis_ttl_days)
        try:
            app.state.enqueue_diagnosis(job.id)
        except Exception as exc:  # noqa: BLE001 - persist the job but make queue failure visible.
            job = session.get(DiagnosisJob, job.id)
            assert job is not None
            job.status = DiagnosisStatus.FAILED
            job.error_text = "diagnosis queue unavailable"
            session.commit()
            raise HTTPException(status_code=503, detail="diagnosis queue unavailable") from exc
        return {"job": diagnosis_dict(job)}

    @app.get("/api/diagnoses/{job_id}")
    def get_diagnosis(job_id: str, _: User = Depends(current_user), session: Session = Depends(database_session)) -> dict[str, Any]:
        job = session.get(DiagnosisJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="diagnosis not found")
        return {"job": diagnosis_dict(job, include_report=True)}

    @app.get("/api/incidents")
    def list_incidents(_: User = Depends(current_user), session: Session = Depends(database_session)) -> dict[str, list[dict[str, Any]]]:
        incidents = session.scalars(select(Incident).order_by(Incident.updated_at.desc()).limit(100)).all()
        return {"items": [incident_dict(incident) for incident in incidents]}

    @app.post("/api/incidents", status_code=status.HTTP_201_CREATED)
    def create_incident(payload: IncidentCreate, user: User = Depends(require_role(Role.ADMIN, Role.ONCALL)), session: Session = Depends(database_session)) -> dict[str, Any]:
        diagnosis = session.get(DiagnosisJob, payload.diagnosis_id)
        if not diagnosis:
            raise HTTPException(status_code=404, detail="diagnosis not found")
        try:
            incident = create_incident_from_diagnosis(session, actor=user, diagnosis=diagnosis, title=payload.title)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        queue_notification(session, incident, "created")
        return {"incident": incident_dict(incident)}

    @app.get("/api/incidents/{incident_id}")
    def get_incident(incident_id: str, _: User = Depends(current_user), session: Session = Depends(database_session)) -> dict[str, Any]:
        incident = session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="incident not found")
        diagnosis = session.get(DiagnosisJob, incident.diagnosis_id)
        comments = session.scalars(select(IncidentComment).where(IncidentComment.incident_id == incident.id).order_by(IncidentComment.created_at)).all()
        audit_events = session.scalars(select(AuditEvent).where(AuditEvent.target_type == "incident", AuditEvent.target_id == incident.id).order_by(AuditEvent.created_at)).all()
        return {
            "incident": incident_dict(incident),
            "diagnosis": diagnosis_dict(diagnosis, include_report=True) if diagnosis else None,
            "comments": [{"id": item.id, "author_id": item.author_id, "body": item.body, "created_at": item.created_at.isoformat()} for item in comments],
            "audit": [{"action": item.action, "actor_id": item.actor_id, "metadata": item.metadata_json, "created_at": item.created_at.isoformat()} for item in audit_events],
        }

    @app.patch("/api/incidents/{incident_id}")
    def update_incident(incident_id: str, payload: IncidentPatch, user: User = Depends(require_role(Role.ADMIN, Role.ONCALL)), session: Session = Depends(database_session)) -> dict[str, Any]:
        incident = session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="incident not found")
        try:
            updated = transition_incident(
                session,
                actor=user,
                incident=incident,
                status=payload.status or incident.status,
                assignee_id=user.id if payload.assign_to_me else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        queue_notification(session, updated, "updated")
        return {"incident": incident_dict(updated)}

    @app.post("/api/incidents/{incident_id}/comments", status_code=status.HTTP_201_CREATED)
    def comment(incident_id: str, payload: CommentCreate, user: User = Depends(require_role(Role.ADMIN, Role.ONCALL)), session: Session = Depends(database_session)) -> dict[str, Any]:
        incident = session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="incident not found")
        try:
            item = add_comment(session, actor=user, incident=incident, body=payload.body, ttl_days=settings.diagnosis_ttl_days)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"comment": {"id": item.id, "body": item.body, "created_at": item.created_at.isoformat()}}

    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    def workspace() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
