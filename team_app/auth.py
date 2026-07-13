"""Password, token and server-side session primitives for local authentication."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名须为 3～64 位小写字母、数字、点、下划线或连字符")
    return username


def validate_password(password: str) -> None:
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise ValueError("密码长度须为 12～128 个字符")


def hash_password(password: str) -> str:
    validate_password(password)
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def new_one_time_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def source_hash(secret: str, source: str) -> str:
    return hashlib.sha256(f"{secret}:{source}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionData:
    user_id: str
    session_version: int
    csrf_token: str


class SecurityStore(Protocol):
    def create_session(self, user_id: str, session_version: int, ttl_seconds: int) -> tuple[str, SessionData]: ...
    def get_session(self, session_id: str, ttl_seconds: int) -> SessionData | None: ...
    def delete_session(self, session_id: str) -> None: ...
    def revoke_user_sessions(self, user_id: str) -> None: ...
    def hit_rate_limit(self, key: str, limit: int, window_seconds: int) -> bool: ...
    def ping(self) -> bool: ...


class RedisSecurityStore:
    """Redis-backed opaque sessions and fixed-window rate limits."""

    def __init__(self, redis_url: str):
        from redis import Redis

        self.redis = Redis.from_url(redis_url, decode_responses=True)

    @staticmethod
    def _session_key(session_id: str) -> str:
        return f"sl100:session:{session_id}"

    @staticmethod
    def _user_sessions_key(user_id: str) -> str:
        return f"sl100:user-sessions:{user_id}"

    def create_session(self, user_id: str, session_version: int, ttl_seconds: int) -> tuple[str, SessionData]:
        session_id = secrets.token_urlsafe(32)
        data = SessionData(user_id=user_id, session_version=session_version, csrf_token=secrets.token_urlsafe(24))
        user_key = self._user_sessions_key(user_id)
        pipeline = self.redis.pipeline()
        pipeline.set(self._session_key(session_id), json.dumps(data.__dict__), ex=ttl_seconds)
        pipeline.sadd(user_key, session_id)
        pipeline.expire(user_key, ttl_seconds)
        pipeline.execute()
        return session_id, data

    def get_session(self, session_id: str, ttl_seconds: int) -> SessionData | None:
        raw = self.redis.get(self._session_key(session_id))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            data = SessionData(
                user_id=str(payload["user_id"]),
                session_version=int(payload["session_version"]),
                csrf_token=str(payload["csrf_token"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.redis.delete(self._session_key(session_id))
            return None
        pipeline = self.redis.pipeline()
        pipeline.expire(self._session_key(session_id), ttl_seconds)
        pipeline.expire(self._user_sessions_key(data.user_id), ttl_seconds)
        pipeline.execute()
        return data

    def delete_session(self, session_id: str) -> None:
        raw = self.redis.get(self._session_key(session_id))
        pipeline = self.redis.pipeline()
        pipeline.delete(self._session_key(session_id))
        if raw:
            try:
                user_id = str(json.loads(raw)["user_id"])
                pipeline.srem(self._user_sessions_key(user_id), session_id)
            except (KeyError, TypeError, json.JSONDecodeError):
                pass
        pipeline.execute()

    def revoke_user_sessions(self, user_id: str) -> None:
        user_key = self._user_sessions_key(user_id)
        session_ids = self.redis.smembers(user_key)
        pipeline = self.redis.pipeline()
        for session_id in session_ids:
            pipeline.delete(self._session_key(session_id))
        pipeline.delete(user_key)
        pipeline.execute()

    def hit_rate_limit(self, key: str, limit: int, window_seconds: int) -> bool:
        redis_key = f"sl100:rate:{key}"
        count = self.redis.incr(redis_key)
        if count == 1:
            self.redis.expire(redis_key, window_seconds)
        return int(count) <= limit

    def ping(self) -> bool:
        return bool(self.redis.ping())


class MemorySecurityStore:
    """Small deterministic store used by unit tests; production always uses Redis."""

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[SessionData, float]] = {}
        self._rates: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def create_session(self, user_id: str, session_version: int, ttl_seconds: int) -> tuple[str, SessionData]:
        session_id = secrets.token_urlsafe(32)
        data = SessionData(user_id=user_id, session_version=session_version, csrf_token=secrets.token_urlsafe(24))
        with self._lock:
            self._sessions[session_id] = (data, time.monotonic() + ttl_seconds)
        return session_id, data

    def get_session(self, session_id: str, ttl_seconds: int) -> SessionData | None:
        with self._lock:
            current = self._sessions.get(session_id)
            if not current or current[1] <= time.monotonic():
                self._sessions.pop(session_id, None)
                return None
            self._sessions[session_id] = (current[0], time.monotonic() + ttl_seconds)
            return current[0]

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def revoke_user_sessions(self, user_id: str) -> None:
        with self._lock:
            self._sessions = {key: value for key, value in self._sessions.items() if value[0].user_id != user_id}

    def hit_rate_limit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            count, expires = self._rates.get(key, (0, now + window_seconds))
            if expires <= now:
                count, expires = 0, now + window_seconds
            count += 1
            self._rates[key] = (count, expires)
            return count <= limit

    def ping(self) -> bool:
        return True
