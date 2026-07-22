"""FastAPI workspace for local-account team diagnosis and incident collaboration."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import secrets
import time
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from iot_ops_agent.web.accounts import (
    GENERIC_LOGIN_ERROR,
    accept_invite,
    authenticate,
    change_password,
    create_invite,
    create_reset_token,
    reset_password,
    update_user,
)
from iot_ops_agent.web.auth import MemorySecurityStore, RedisSecurityStore, SecurityStore, source_hash, token_hash
from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.db import initialize_database, make_session_factory, migration_is_current
from iot_ops_agent.web.models import (
    AgentToolCall,
    AuditEvent,
    DiagnosisFeedback,
    DiagnosisJob,
    DiagnosisStatus,
    Incident,
    IncidentComment,
    IncidentStatus,
    NotificationDelivery,
    Role,
    User,
    UserSession,
    utcnow,
)
from iot_ops_agent.web.services import (
    add_comment,
    approve_diagnosis,
    create_diagnosis,
    create_incident_from_diagnosis,
    ensure_user,
    record_diagnosis_feedback,
    transition_incident,
)
from iot_ops_agent.web.tasks import enqueue_diagnosis, enqueue_notification


STATIC_DIR = Path(__file__).with_name("static")
LOGGER = logging.getLogger("iot_ops.web")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class InviteAcceptPayload(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=12, max_length=128)


class PasswordResetPayload(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    new_password: str = Field(min_length=12, max_length=128)


class PasswordChangePayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=128)


class InviteCreatePayload(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=255)
    role: Role


class UserPatchPayload(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: Role | None = None
    is_active: bool | None = None


class DiagnosisCreate(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    no_remote: bool = False


class DiagnosisExecute(BaseModel):
    mode: str = Field(default="rules", pattern="^(rules|ai_assisted)$")
    external_ai_consent: bool = False


class DiagnosisFeedbackCreate(BaseModel):
    rating: str = Field(pattern="^(useful|partial|not_useful)$")
    evidence_correct: bool | None = None
    corrected_incident_types: list[str] = Field(default_factory=list, max_length=12)
    note: str = Field(default="", max_length=2000)


class IncidentCreate(BaseModel):
    diagnosis_id: str
    title: str = Field(default="", max_length=300)


class IncidentPatch(BaseModel):
    status: IncidentStatus | None = None
    assignee_id: str | None = None
    assign_to_me: bool = False


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


def user_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.value,
        "is_active": user.is_active,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat(),
    }


def diagnosis_dict(job: DiagnosisJob, *, include_report: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": job.id,
        "query": job.query,
        "no_remote": job.no_remote,
        "plan": job.plan_json,
        "plan_digest": job.plan_digest,
        "execution_mode": job.execution_mode,
        "status": job.status.value,
        "error": job.error_text,
        "created_by_id": job.created_by_id,
        "retry_of_id": job.retry_of_id,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "expires_at": job.expires_at.isoformat(),
        "result_status": job.result_status,
        "report_schema_version": job.report_schema_version,
        "model_id": job.model_id,
        "prompt_version": job.prompt_version,
        "usage": {
            "input_tokens": job.input_tokens,
            "output_tokens": job.output_tokens,
            "tool_call_count": job.tool_call_count,
            "duration_ms": job.duration_ms,
        },
    }
    if include_report:
        payload["report"] = job.report_json
    return payload


def incident_dict(incident: Incident, users: dict[str, User] | None = None) -> dict[str, Any]:
    users = users or {}
    assignee = users.get(incident.assignee_id or "")
    creator = users.get(incident.created_by_id)
    return {
        "id": incident.id,
        "diagnosis_id": incident.diagnosis_id,
        "title": incident.title,
        "service": incident.service,
        "risk_level": incident.risk_level,
        "status": incident.status.value,
        "assignee_id": incident.assignee_id,
        "assignee_name": assignee.display_name if assignee else "",
        "created_by_name": creator.display_name if creator else "",
        "created_at": incident.created_at.isoformat(),
        "updated_at": incident.updated_at.isoformat(),
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
    }


def create_app(
    settings: TeamSettings | None = None,
    *,
    enqueue_diagnosis_fn: Callable[[str], None] = enqueue_diagnosis,
    enqueue_notification_fn: Callable[[str], None] = enqueue_notification,
    security_store: SecurityStore | None = None,
) -> FastAPI:
    settings = settings or TeamSettings.from_env()
    session_factory = make_session_factory(settings)
    if security_store is None:
        security_store = MemorySecurityStore() if settings.auth_mode == "dev" else RedisSecurityStore(settings.redis_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate_runtime()
        if not settings.is_production:
            initialize_database(session_factory)
        try:
            yield
        finally:
            session_factory.kw["bind"].dispose()

    app = FastAPI(title="IoT Ops Agent 团队值班工作台", version=settings.app_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.security_store = security_store
    app.state.enqueue_diagnosis = enqueue_diagnosis_fn
    app.state.enqueue_notification = enqueue_notification_fn

    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", "")[:64] or secrets.token_hex(8)
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            LOGGER.exception(json.dumps({
                "event": "http.request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": 500,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }, ensure_ascii=False))
            raise
        response.headers["X-Request-ID"] = request_id
        LOGGER.info(json.dumps({
            "event": "http.request",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }, ensure_ascii=False))
        return response

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
                    raise HTTPException(status_code=400, detail="X-Dev-Role 无效") from exc
                return ensure_user(
                    session,
                    subject=request.headers.get("X-Dev-User", "local-admin"),
                    email=request.headers.get("X-Dev-Email", "local-admin@example.invalid"),
                    display_name=request.headers.get("X-Dev-Name", "本地管理员"),
                    role=role,
                )
            session_id = request.cookies.get(settings.session_cookie_name, "")
            session_data = security_store.get_session(session_id, settings.session_ttl_seconds) if session_id else None
            if not session_data:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
            user = session.get(User, session_data.user_id)
            session_record = session.scalar(
                select(UserSession).where(UserSession.session_id_hash == token_hash(session_id))
            )
            if (
                not user
                or not user.is_active
                or user.session_version != session_data.session_version
                or not session_record
                or session_record.revoked_at is not None
                or _aware(session_record.expires_at) <= utcnow()
            ):
                security_store.delete_session(session_id)
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效，请重新登录")
            request.state.session_id = session_id
            request.state.session_data = session_data
            return user

    def require_role(*allowed: Role, write: bool = False):
        def guard(request: Request) -> User:
            user = current_user(request)
            if user.role not in allowed:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前角色无权执行此操作")
            if write and settings.auth_mode == "local":
                csrf = request.headers.get("X-CSRF-Token", "")
                if not csrf or not secrets.compare_digest(csrf, request.state.session_data.csrf_token):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败，请刷新页面后重试")
            return user

        return guard

    authenticated_write = require_role(Role.ADMIN, Role.ONCALL, Role.VIEWER, write=True)
    operator_write = require_role(Role.ADMIN, Role.ONCALL, write=True)
    admin_write = require_role(Role.ADMIN, write=True)

    def queue_notification(session: Session, incident: Incident, event_type: str) -> NotificationDelivery:
        delivery = NotificationDelivery(incident_id=incident.id, event_type=event_type)
        session.add(delivery)
        session.commit()
        try:
            app.state.enqueue_notification(delivery.id)
        except Exception:
            delivery.error_text = "通知队列暂不可用，恢复服务将自动重试"
            session.commit()
        return delivery

    def enforce_diagnosis_limits(session: Session, user: User, *, check_rate: bool = True) -> None:
        if check_rate:
            rate_key = source_hash(settings.session_secret, user.id)
            if not security_store.hit_rate_limit(
                f"diagnosis:{rate_key}", settings.diagnosis_rate_limit, settings.diagnosis_rate_window_seconds
            ):
                raise HTTPException(status_code=429, detail="提交过于频繁：每 10 分钟最多提交 10 次诊断")
        session.execute(select(User.id).where(User.id == user.id).with_for_update())
        active = session.scalar(
            select(func.count(DiagnosisJob.id)).where(
                DiagnosisJob.created_by_id == user.id,
                DiagnosisJob.status.in_([DiagnosisStatus.QUEUED, DiagnosisStatus.RUNNING]),
            )
        ) or 0
        if active >= settings.diagnosis_max_active:
            raise HTTPException(status_code=429, detail="每名成员最多同时运行 3 个诊断")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.app_env, "version": settings.app_version}

    @app.get("/api/ready")
    def ready() -> dict[str, Any]:
        checks = {"database": False, "redis": False, "migration": not settings.is_production}
        try:
            with session_factory() as session:
                session.execute(text("SELECT 1"))
            checks["database"] = True
            checks["redis"] = security_store.ping()
            checks["migration"] = migration_is_current(settings) if settings.is_production else True
            if not all(checks.values()):
                raise RuntimeError("readiness check failed")
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks}) from exc
        return {"status": "ready", "checks": checks}

    @app.post("/api/auth/login")
    def login(payload: LoginPayload, request: Request) -> Response:
        if settings.auth_mode != "local":
            raise HTTPException(status_code=404, detail="本地登录仅在 AUTH_MODE=local 时启用")
        source = request.client.host if request.client else "unknown"
        username_key = source_hash(settings.session_secret, payload.username.strip().lower())
        source_key = source_hash(settings.session_secret, source)
        window = settings.login_lock_minutes * 60
        if not security_store.hit_rate_limit(f"login:user:{username_key}", settings.login_failure_limit * 2, window):
            raise HTTPException(status_code=429, detail=GENERIC_LOGIN_ERROR)
        if not security_store.hit_rate_limit(f"login:source:{source_key}", settings.login_failure_limit * 4, window):
            raise HTTPException(status_code=429, detail=GENERIC_LOGIN_ERROR)
        with session_factory() as session:
            try:
                user = authenticate(session, username=payload.username, password=payload.password, source=source, settings=settings)
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR) from exc
            session_id, _ = security_store.create_session(user.id, user.session_version, settings.session_ttl_seconds)
            session.add(
                UserSession(
                    user_id=user.id,
                    session_id_hash=token_hash(session_id),
                    expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
                )
            )
            session.commit()
            body = {"user": user_dict(user)}
        response = JSONResponse(body)
        response.set_cookie(
            settings.session_cookie_name,
            session_id,
            max_age=settings.session_ttl_seconds,
            secure=settings.is_production,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request, _: User = Depends(authenticated_write)) -> Response:
        session_id = getattr(request.state, "session_id", "")
        if session_id:
            security_store.delete_session(session_id)
            with session_factory() as session:
                record = session.scalar(select(UserSession).where(UserSession.session_id_hash == token_hash(session_id)))
                if record:
                    record.revoked_at = utcnow()
                    session.commit()
        response = JSONResponse({"ok": True})
        response.delete_cookie(settings.session_cookie_name, path="/", secure=settings.is_production, httponly=True, samesite="lax")
        return response

    @app.post("/api/auth/accept-invite", status_code=status.HTTP_201_CREATED)
    def accept_invite_endpoint(payload: InviteAcceptPayload, session: Session = Depends(database_session)) -> dict[str, Any]:
        try:
            user = accept_invite(session, raw_token=payload.token, password=payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"user": user_dict(user), "message": "账号已激活，请登录"}

    @app.post("/api/auth/reset-password")
    def reset_password_endpoint(payload: PasswordResetPayload, session: Session = Depends(database_session)) -> dict[str, bool]:
        try:
            user = reset_password(session, raw_token=payload.token, new_password=payload.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        security_store.revoke_user_sessions(user.id)
        session.query(UserSession).filter(UserSession.user_id == user.id, UserSession.revoked_at.is_(None)).update(
            {UserSession.revoked_at: utcnow()}, synchronize_session=False
        )
        session.commit()
        return {"ok": True}

    @app.post("/api/auth/change-password")
    def change_password_endpoint(
        payload: PasswordChangePayload,
        user: User = Depends(authenticated_write),
        session: Session = Depends(database_session),
    ) -> dict[str, bool]:
        persisted = session.get(User, user.id)
        assert persisted is not None
        try:
            change_password(session, user=persisted, current_password=payload.current_password, new_password=payload.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        security_store.revoke_user_sessions(user.id)
        with session_factory() as session:
            session.query(UserSession).filter(UserSession.user_id == user.id, UserSession.revoked_at.is_(None)).update(
                {UserSession.revoked_at: utcnow()}, synchronize_session=False
            )
            session.commit()
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        csrf_token = "dev-no-csrf" if settings.auth_mode == "dev" else request.state.session_data.csrf_token
        return {
            "user": user_dict(user),
            "auth_mode": settings.auth_mode,
            "csrf_token": csrf_token,
            "notification_enabled": bool(settings.feishu_webhook_url),
            "ai_assisted_enabled": settings.ai_assisted_enabled,
            "app_version": settings.app_version,
        }

    @app.get("/api/admin/users")
    def list_users(_: User = Depends(require_role(Role.ADMIN)), session: Session = Depends(database_session)) -> dict[str, Any]:
        users = session.scalars(select(User).order_by(User.created_at)).all()
        return {"items": [user_dict(user) for user in users]}

    @app.post("/api/admin/invites", status_code=status.HTTP_201_CREATED)
    def create_invite_endpoint(
        payload: InviteCreatePayload,
        user: User = Depends(admin_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        try:
            invite, raw_token = create_invite(
                session,
                actor=user,
                username=payload.username,
                display_name=payload.display_name,
                role=payload.role,
                settings=settings,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "invite": {"id": invite.id, "username": invite.username, "expires_at": invite.expires_at.isoformat()},
            "invite_url": f"{settings.app_url}/#invite={raw_token}",
            "message": "邀请链接只显示本次，请立即复制保存",
        }

    @app.patch("/api/admin/users/{user_id}")
    def update_user_endpoint(
        user_id: str,
        payload: UserPatchPayload,
        actor: User = Depends(admin_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        target = session.get(User, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="成员不存在")
        previous_version = target.session_version
        try:
            updated = update_user(
                session,
                actor=actor,
                user=target,
                role=payload.role,
                is_active=payload.is_active,
                display_name=payload.display_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if updated.session_version != previous_version:
            security_store.revoke_user_sessions(updated.id)
            session.query(UserSession).filter(
                UserSession.user_id == updated.id, UserSession.revoked_at.is_(None)
            ).update({UserSession.revoked_at: utcnow()}, synchronize_session=False)
            session.commit()
        return {"user": user_dict(updated)}

    @app.post("/api/admin/users/{user_id}/reset-link")
    def create_reset_link_endpoint(
        user_id: str,
        actor: User = Depends(admin_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        target = session.get(User, user_id)
        if not target or not target.is_active:
            raise HTTPException(status_code=404, detail="有效成员不存在")
        reset, raw_token = create_reset_token(session, actor=actor, user=target, settings=settings)
        return {
            "expires_at": reset.expires_at.isoformat(),
            "reset_url": f"{settings.app_url}/#reset={raw_token}",
            "message": "重置链接只显示本次，请立即复制保存",
        }

    @app.get("/api/users")
    def assignable_users(_: User = Depends(current_user), session: Session = Depends(database_session)) -> dict[str, Any]:
        users = session.scalars(
            select(User).where(User.is_active.is_(True), User.role.in_([Role.ADMIN, Role.ONCALL])).order_by(User.display_name)
        ).all()
        return {"items": [user_dict(user) for user in users]}

    @app.get("/api/diagnoses")
    def list_diagnoses(
        job_status: DiagnosisStatus | None = Query(default=None, alias="status"),
        created_by: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        _: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        statement = select(DiagnosisJob)
        if job_status:
            statement = statement.where(DiagnosisJob.status == job_status)
        if created_by:
            statement = statement.where(DiagnosisJob.created_by_id == created_by)
        if cursor:
            pivot = session.get(DiagnosisJob, cursor)
            if pivot:
                statement = statement.where(DiagnosisJob.created_at < pivot.created_at)
        jobs = session.scalars(statement.order_by(DiagnosisJob.created_at.desc()).limit(limit + 1)).all()
        next_cursor = jobs[limit - 1].id if len(jobs) > limit else None
        return {"items": [diagnosis_dict(job) for job in jobs[:limit]], "next_cursor": next_cursor}

    @app.post("/api/diagnoses", status_code=status.HTTP_202_ACCEPTED)
    def create_diagnosis_endpoint(
        payload: DiagnosisCreate,
        user: User = Depends(operator_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        enforce_diagnosis_limits(session, user)
        job = create_diagnosis(
            session, actor=user, query=payload.query, no_remote=payload.no_remote, ttl_days=settings.diagnosis_ttl_days
        )
        queue_delayed = False
        try:
            app.state.enqueue_diagnosis(job.id)
        except Exception:
            queue_delayed = True
            job.error_text = "任务已保存，队列恢复后将自动执行"
            session.commit()
        return {"job": diagnosis_dict(job), "queue_delayed": queue_delayed}

    @app.post("/api/v1/diagnoses", status_code=status.HTTP_201_CREATED)
    def create_planned_diagnosis_endpoint(
        payload: DiagnosisCreate,
        user: User = Depends(operator_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        enforce_diagnosis_limits(session, user)
        job = create_diagnosis(
            session,
            actor=user,
            query=payload.query,
            no_remote=payload.no_remote,
            ttl_days=settings.diagnosis_ttl_days,
            planned=True,
            settings=settings,
        )
        return {"job": diagnosis_dict(job), "ai_assisted_available": settings.ai_assisted_enabled}

    @app.post("/api/v1/diagnoses/{job_id}/execute", status_code=status.HTTP_202_ACCEPTED)
    def execute_planned_diagnosis_endpoint(
        job_id: str,
        payload: DiagnosisExecute,
        actor: User = Depends(operator_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        job = session.get(DiagnosisJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="诊断不存在")
        if job.created_by_id != actor.id and actor.role != Role.ADMIN:
            raise HTTPException(status_code=403, detail="只有发起人或管理员可以批准该诊断")
        enforce_diagnosis_limits(session, actor, check_rate=False)
        try:
            approve_diagnosis(
                session,
                actor=actor,
                job=job,
                execution_mode=payload.mode,
                external_ai_consent=payload.external_ai_consent,
                settings=settings,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        queue_delayed = False
        try:
            app.state.enqueue_diagnosis(job.id)
        except Exception:
            queue_delayed = True
            job.error_text = "任务已批准，队列恢复后将自动执行"
            session.commit()
        return {"job": diagnosis_dict(job), "queue_delayed": queue_delayed}

    @app.get("/api/v1/diagnoses/{job_id}")
    def get_v1_diagnosis(
        job_id: str,
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        job = session.get(DiagnosisJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="诊断不存在")
        calls = session.scalars(
            select(AgentToolCall)
            .where(AgentToolCall.diagnosis_id == job.id)
            .order_by(AgentToolCall.sequence)
        ).all()
        feedback = session.scalar(
            select(DiagnosisFeedback).where(
                DiagnosisFeedback.diagnosis_id == job.id,
                DiagnosisFeedback.user_id == user.id,
            )
        )
        return {
            "job": diagnosis_dict(job, include_report=True),
            "tool_calls": [
                {
                    "sequence": item.sequence,
                    "tool_name": item.tool_name,
                    "arguments": item.arguments_json,
                    "evidence_refs": item.evidence_refs_json,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "error": item.error_text,
                }
                for item in calls
            ],
            "feedback": None if feedback is None else {
                "rating": feedback.rating,
                "evidence_correct": feedback.evidence_correct,
                "corrected_incident_types": feedback.corrected_incident_types_json,
                "note": feedback.note,
                "updated_at": feedback.updated_at.isoformat(),
            },
        }

    @app.post("/api/v1/diagnoses/{job_id}/feedback")
    def create_diagnosis_feedback_endpoint(
        job_id: str,
        payload: DiagnosisFeedbackCreate,
        actor: User = Depends(authenticated_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        job = session.get(DiagnosisJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="诊断不存在")
        try:
            feedback = record_diagnosis_feedback(
                session,
                actor=actor,
                diagnosis=job,
                rating=payload.rating,
                evidence_correct=payload.evidence_correct,
                corrected_incident_types=payload.corrected_incident_types,
                note=payload.note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"feedback": {"rating": feedback.rating, "updated_at": feedback.updated_at.isoformat()}}

    @app.get("/api/v1/admin/quality")
    def quality_metrics(
        _: User = Depends(require_role(Role.ADMIN)),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        total = session.scalar(select(func.count(DiagnosisJob.id))) or 0
        completed = session.scalar(
            select(func.count(DiagnosisJob.id)).where(DiagnosisJob.status == DiagnosisStatus.COMPLETED)
        ) or 0
        failed = session.scalar(
            select(func.count(DiagnosisJob.id)).where(DiagnosisJob.status == DiagnosisStatus.FAILED)
        ) or 0
        ai_runs = session.scalar(
            select(func.count(DiagnosisJob.id)).where(DiagnosisJob.execution_mode == "ai_assisted")
        ) or 0
        avg_duration = session.scalar(
            select(func.avg(DiagnosisJob.duration_ms)).where(DiagnosisJob.status == DiagnosisStatus.COMPLETED)
        ) or 0
        feedback_rows = session.execute(
            select(DiagnosisFeedback.rating, func.count(DiagnosisFeedback.id)).group_by(DiagnosisFeedback.rating)
        ).all()
        ratings = {str(name): int(count) for name, count in feedback_rows}
        feedback_total = sum(ratings.values())
        useful = ratings.get("useful", 0) + ratings.get("partial", 0)
        return {
            "diagnoses": {
                "total": total,
                "completed": completed,
                "failed": failed,
                "completion_rate": completed / (completed + failed) if completed + failed else 0,
                "ai_assisted": ai_runs,
                "average_duration_ms": int(avg_duration),
            },
            "feedback": {
                "total": feedback_total,
                "ratings": ratings,
                "useful_rate": useful / feedback_total if feedback_total else 0,
                "coverage_rate": feedback_total / completed if completed else 0,
            },
        }

    @app.get("/internal/metrics", include_in_schema=False, response_class=PlainTextResponse)
    def prometheus_metrics(session: Session = Depends(database_session)) -> str:
        rows = session.execute(
            select(DiagnosisJob.status, DiagnosisJob.execution_mode, func.count(DiagnosisJob.id))
            .group_by(DiagnosisJob.status, DiagnosisJob.execution_mode)
        ).all()
        lines = [
            "# HELP iot_ops_diagnoses_total Persisted diagnosis jobs.",
            "# TYPE iot_ops_diagnoses_total gauge",
        ]
        for job_status, mode, count in rows:
            lines.append(
                f'iot_ops_diagnoses_total{{status="{job_status.value}",mode="{mode}"}} {int(count)}'
            )
        lines.extend([
            "# HELP iot_ops_feedback_total Persisted diagnosis feedback.",
            "# TYPE iot_ops_feedback_total gauge",
        ])
        feedback_rows = session.execute(
            select(DiagnosisFeedback.rating, func.count(DiagnosisFeedback.id)).group_by(DiagnosisFeedback.rating)
        ).all()
        for rating, count in feedback_rows:
            lines.append(f'iot_ops_feedback_total{{rating="{rating}"}} {int(count)}')
        completed_count, duration_sum, input_sum, output_sum = session.execute(
            select(
                func.count(DiagnosisJob.id),
                func.coalesce(func.sum(DiagnosisJob.duration_ms), 0),
                func.coalesce(func.sum(DiagnosisJob.input_tokens), 0),
                func.coalesce(func.sum(DiagnosisJob.output_tokens), 0),
            ).where(DiagnosisJob.status == DiagnosisStatus.COMPLETED)
        ).one()
        lines.extend([
            "# HELP iot_ops_diagnosis_duration_seconds Total completed diagnosis execution time.",
            "# TYPE iot_ops_diagnosis_duration_seconds summary",
            f"iot_ops_diagnosis_duration_seconds_count {int(completed_count)}",
            f"iot_ops_diagnosis_duration_seconds_sum {int(duration_sum) / 1000}",
            "# HELP iot_ops_model_tokens_total Persisted model token usage.",
            "# TYPE iot_ops_model_tokens_total counter",
            f'iot_ops_model_tokens_total{{direction="input"}} {int(input_sum)}',
            f'iot_ops_model_tokens_total{{direction="output"}} {int(output_sum)}',
        ])
        tool_rows = session.execute(
            select(AgentToolCall.status, func.count(AgentToolCall.id)).group_by(AgentToolCall.status)
        ).all()
        lines.extend([
            "# HELP iot_ops_agent_tool_calls_total Persisted bounded Agent tool calls.",
            "# TYPE iot_ops_agent_tool_calls_total gauge",
        ])
        for tool_status, count in tool_rows:
            lines.append(f'iot_ops_agent_tool_calls_total{{status="{tool_status}"}} {int(count)}')
        lines.append("")
        return "\n".join(lines)

    @app.get("/api/diagnoses/{job_id}")
    def get_diagnosis(
        job_id: str, _: User = Depends(current_user), session: Session = Depends(database_session)
    ) -> dict[str, Any]:
        job = session.get(DiagnosisJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="诊断不存在")
        return {"job": diagnosis_dict(job, include_report=True)}

    @app.post("/api/diagnoses/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_diagnosis(
        job_id: str,
        user: User = Depends(operator_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        original = session.get(DiagnosisJob, job_id)
        if not original:
            raise HTTPException(status_code=404, detail="诊断不存在")
        if original.status != DiagnosisStatus.FAILED:
            raise HTTPException(status_code=409, detail="只有失败的诊断可以重试")
        enforce_diagnosis_limits(session, user)
        job = create_diagnosis(
            session,
            actor=user,
            query=original.query,
            no_remote=original.no_remote,
            ttl_days=settings.diagnosis_ttl_days,
            retry_of_id=original.id,
        )
        try:
            app.state.enqueue_diagnosis(job.id)
        except Exception:
            job.error_text = "任务已保存，队列恢复后将自动执行"
            session.commit()
        return {"job": diagnosis_dict(job)}

    @app.get("/api/incidents")
    def list_incidents(
        incident_status: IncidentStatus | None = Query(default=None, alias="status"),
        service: str | None = None,
        assignee_id: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        _: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        statement = select(Incident)
        if incident_status:
            statement = statement.where(Incident.status == incident_status)
        if service:
            statement = statement.where(Incident.service == service)
        if assignee_id:
            statement = statement.where(Incident.assignee_id == assignee_id)
        if cursor:
            pivot = session.get(Incident, cursor)
            if pivot:
                statement = statement.where(Incident.updated_at < pivot.updated_at)
        incidents = session.scalars(statement.order_by(Incident.updated_at.desc()).limit(limit + 1)).all()
        user_ids = {item.created_by_id for item in incidents} | {item.assignee_id for item in incidents if item.assignee_id}
        users = {user.id: user for user in session.scalars(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}
        next_cursor = incidents[limit - 1].id if len(incidents) > limit else None
        return {"items": [incident_dict(item, users) for item in incidents[:limit]], "next_cursor": next_cursor}

    @app.post("/api/incidents", status_code=status.HTTP_201_CREATED)
    def create_incident(
        payload: IncidentCreate,
        user: User = Depends(operator_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        diagnosis = session.get(DiagnosisJob, payload.diagnosis_id)
        if not diagnosis:
            raise HTTPException(status_code=404, detail="诊断不存在")
        existing = session.scalar(select(Incident).where(Incident.diagnosis_id == diagnosis.id))
        try:
            incident = create_incident_from_diagnosis(session, actor=user, diagnosis=diagnosis, title=payload.title)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if existing is None:
            queue_notification(session, incident, "created")
        return {"incident": incident_dict(incident, {user.id: user})}

    @app.get("/api/incidents/{incident_id}")
    def get_incident(
        incident_id: str, _: User = Depends(current_user), session: Session = Depends(database_session)
    ) -> dict[str, Any]:
        incident = session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在")
        diagnosis = session.get(DiagnosisJob, incident.diagnosis_id)
        comments = session.scalars(
            select(IncidentComment).where(IncidentComment.incident_id == incident.id).order_by(IncidentComment.created_at)
        ).all()
        audit_events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.target_type == "incident", AuditEvent.target_id == incident.id)
            .order_by(AuditEvent.created_at)
        ).all()
        notifications = session.scalars(
            select(NotificationDelivery).where(NotificationDelivery.incident_id == incident.id).order_by(NotificationDelivery.created_at)
        ).all()
        user_ids = {incident.created_by_id, incident.assignee_id} | {item.author_id for item in comments} | {
            item.actor_id for item in audit_events
        }
        user_ids.discard(None)
        users = {user.id: user for user in session.scalars(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}
        return {
            "incident": incident_dict(incident, users),
            "diagnosis": diagnosis_dict(diagnosis, include_report=True) if diagnosis else None,
            "comments": [
                {
                    "id": item.id,
                    "author_name": users[item.author_id].display_name if item.author_id in users else "未知成员",
                    "body": item.body,
                    "created_at": item.created_at.isoformat(),
                }
                for item in comments
            ],
            "audit": [
                {
                    "action": item.action,
                    "actor_name": users[item.actor_id].display_name if item.actor_id in users else "系统",
                    "metadata": item.metadata_json,
                    "created_at": item.created_at.isoformat(),
                }
                for item in audit_events
            ],
            "notifications": [
                {
                    "id": item.id,
                    "channel": item.channel,
                    "event_type": item.event_type,
                    "status": item.status,
                    "attempts": item.attempts,
                    "error": item.error_text,
                    "created_at": item.created_at.isoformat(),
                }
                for item in notifications
            ],
        }

    @app.patch("/api/incidents/{incident_id}")
    def update_incident(
        incident_id: str,
        payload: IncidentPatch,
        user: User = Depends(operator_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        incident = session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在")
        assignee_id = user.id if payload.assign_to_me else payload.assignee_id
        if assignee_id:
            assignee = session.get(User, assignee_id)
            if not assignee or not assignee.is_active or assignee.role == Role.VIEWER:
                raise HTTPException(status_code=422, detail="负责人必须是有效的管理员或值班成员")
        try:
            updated = transition_incident(
                session,
                actor=user,
                incident=incident,
                status=payload.status or incident.status,
                assignee_id=assignee_id,
                assign=payload.assign_to_me or "assignee_id" in payload.model_fields_set,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        queue_notification(session, updated, "updated")
        users = session.scalars(select(User).where(User.id.in_({updated.created_by_id, updated.assignee_id}))).all()
        return {"incident": incident_dict(updated, {item.id: item for item in users})}

    @app.post("/api/incidents/{incident_id}/comments", status_code=status.HTTP_201_CREATED)
    def comment(
        incident_id: str,
        payload: CommentCreate,
        user: User = Depends(operator_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        incident = session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在")
        try:
            item = add_comment(
                session, actor=user, incident=incident, body=payload.body, ttl_days=settings.diagnosis_ttl_days
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "comment": {
                "id": item.id,
                "author_name": user.display_name,
                "body": item.body,
                "created_at": item.created_at.isoformat(),
            }
        }

    @app.post("/api/notifications/{delivery_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_notification(
        delivery_id: str,
        _: User = Depends(admin_write),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        delivery = session.get(NotificationDelivery, delivery_id)
        if not delivery:
            raise HTTPException(status_code=404, detail="通知记录不存在")
        if delivery.status == "delivered":
            raise HTTPException(status_code=409, detail="通知已经发送成功，不能重复发送")
        delivery.status = "queued"
        delivery.error_text = ""
        delivery.next_attempt_at = utcnow()
        session.commit()
        try:
            app.state.enqueue_notification(delivery.id)
        except Exception:
            delivery.error_text = "通知队列暂不可用，恢复服务将自动重试"
            session.commit()
        return {"delivery": {"id": delivery.id, "status": delivery.status}}

    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    def workspace() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
