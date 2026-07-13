"""Local account lifecycle: bootstrap, invitations, login and password resets."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from team_app.auth import (
    hash_password,
    new_one_time_token,
    normalize_username,
    password_needs_rehash,
    source_hash,
    token_hash,
    validate_password,
    verify_password,
)
from team_app.config import TeamSettings
from team_app.models import InviteToken, LoginAudit, PasswordResetToken, Role, User, utcnow
from team_app.services import audit


GENERIC_LOGIN_ERROR = "用户名或密码错误"


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def bootstrap_admin(session: Session, *, username: str, display_name: str, password: str) -> User:
    username = normalize_username(username)
    if session.scalar(select(func.count(User.id))) != 0:
        raise ValueError("系统中已经存在账号，不能再次初始化首位管理员")
    user = User(
        subject=f"local:{username}",
        username=username,
        display_name=display_name.strip() or username,
        role=Role.ADMIN,
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(user)
    session.flush()
    audit(session, actor_id=user.id, action="user.bootstrap", target_type="user", target_id=user.id)
    session.commit()
    session.refresh(user)
    return user


def create_invite(
    session: Session,
    *,
    actor: User,
    username: str,
    display_name: str,
    role: Role,
    settings: TeamSettings,
) -> tuple[InviteToken, str]:
    username = normalize_username(username)
    if session.scalar(select(User.id).where(User.username == username)):
        raise ValueError("该用户名已经存在")
    now = utcnow()
    pending = session.scalars(
        select(InviteToken).where(InviteToken.username == username, InviteToken.consumed_at.is_(None))
    ).all()
    for item in pending:
        item.consumed_at = now
    raw_token = new_one_time_token()
    invite = InviteToken(
        username=username,
        display_name=display_name.strip() or username,
        role=role,
        token_hash=token_hash(raw_token),
        created_by_id=actor.id,
        expires_at=now + timedelta(hours=settings.invite_ttl_hours),
    )
    session.add(invite)
    session.flush()
    audit(session, actor_id=actor.id, action="invite.created", target_type="invite", target_id=invite.id, metadata={"username": username, "role": role.value})
    session.commit()
    session.refresh(invite)
    return invite, raw_token


def accept_invite(session: Session, *, raw_token: str, password: str) -> User:
    validate_password(password)
    invite = session.scalar(
        select(InviteToken).where(InviteToken.token_hash == token_hash(raw_token)).with_for_update()
    )
    now = utcnow()
    if not invite or invite.consumed_at is not None or (_aware(invite.expires_at) or now) <= now:
        raise ValueError("邀请链接无效、已使用或已过期")
    if session.scalar(select(User.id).where(User.username == invite.username)):
        invite.consumed_at = now
        session.commit()
        raise ValueError("邀请链接已失效")
    user = User(
        subject=f"local:{invite.username}",
        username=invite.username,
        display_name=invite.display_name,
        role=invite.role,
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(user)
    session.flush()
    invite.consumed_at = now
    audit(session, actor_id=user.id, action="invite.accepted", target_type="user", target_id=user.id, metadata={"invite_id": invite.id})
    session.commit()
    session.refresh(user)
    return user


def authenticate(
    session: Session,
    *,
    username: str,
    password: str,
    source: str,
    settings: TeamSettings,
) -> User:
    try:
        normalized = normalize_username(username)
    except ValueError:
        normalized = username.strip().lower()[:64]
    user = session.scalar(select(User).where(User.username == normalized))
    now = utcnow()
    locked = bool(user and _aware(user.locked_until) and _aware(user.locked_until) > now)
    valid = bool(user and user.is_active and not locked and verify_password(user.password_hash, password))
    reason = "success" if valid else "invalid_credentials"
    if locked:
        reason = "locked"
    elif user and not user.is_active:
        reason = "disabled"
    session.add(LoginAudit(
        user_id=user.id if user else None,
        username=normalized,
        source_hash=source_hash(settings.session_secret, source),
        success=valid,
        reason=reason,
    ))
    if not valid:
        if user and not locked:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= settings.login_failure_limit:
                user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
                user.failed_login_attempts = 0
        session.commit()
        raise ValueError(GENERIC_LOGIN_ERROR)
    assert user is not None
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    session.commit()
    session.refresh(user)
    return user


def change_password(session: Session, *, user: User, current_password: str, new_password: str) -> User:
    if not verify_password(user.password_hash, current_password):
        raise ValueError("当前密码错误")
    validate_password(new_password)
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    audit(session, actor_id=user.id, action="password.changed", target_type="user", target_id=user.id)
    session.commit()
    session.refresh(user)
    return user


def create_reset_token(
    session: Session, *, actor: User, user: User, settings: TeamSettings
) -> tuple[PasswordResetToken, str]:
    now = utcnow()
    pending = session.scalars(
        select(PasswordResetToken).where(PasswordResetToken.user_id == user.id, PasswordResetToken.consumed_at.is_(None))
    ).all()
    for item in pending:
        item.consumed_at = now
    raw_token = new_one_time_token()
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash(raw_token),
        created_by_id=actor.id,
        expires_at=now + timedelta(minutes=settings.reset_ttl_minutes),
    )
    session.add(reset)
    session.flush()
    audit(session, actor_id=actor.id, action="password.reset_link_created", target_type="user", target_id=user.id)
    session.commit()
    session.refresh(reset)
    return reset, raw_token


def reset_password(session: Session, *, raw_token: str, new_password: str) -> User:
    validate_password(new_password)
    reset = session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash(raw_token)).with_for_update()
    )
    now = utcnow()
    if not reset or reset.consumed_at is not None or (_aware(reset.expires_at) or now) <= now:
        raise ValueError("密码重置链接无效、已使用或已过期")
    user = session.get(User, reset.user_id)
    if not user or not user.is_active:
        reset.consumed_at = now
        session.commit()
        raise ValueError("密码重置链接已失效")
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    user.failed_login_attempts = 0
    user.locked_until = None
    reset.consumed_at = now
    audit(session, actor_id=user.id, action="password.reset", target_type="user", target_id=user.id)
    session.commit()
    session.refresh(user)
    return user


def update_user(session: Session, *, actor: User, user: User, role: Role | None, is_active: bool | None, display_name: str | None) -> User:
    new_role = role or user.role
    new_active = user.is_active if is_active is None else is_active
    removing_admin = user.is_active and user.role == Role.ADMIN and (not new_active or new_role != Role.ADMIN)
    if removing_admin:
        session.execute(select(User.id).where(User.role == Role.ADMIN, User.is_active.is_(True)).with_for_update())
        active_admins = session.scalar(select(func.count(User.id)).where(User.role == Role.ADMIN, User.is_active.is_(True))) or 0
        if active_admins <= 1:
            raise ValueError("不能禁用或降级系统中最后一名有效管理员")
    access_changed = user.role != new_role or user.is_active != new_active
    user.role = new_role
    user.is_active = new_active
    if display_name is not None:
        user.display_name = display_name.strip() or user.username
    if access_changed:
        user.session_version += 1
    audit(session, actor_id=actor.id, action="user.updated", target_type="user", target_id=user.id, metadata={"role": user.role.value, "is_active": user.is_active})
    session.commit()
    session.refresh(user)
    return user
