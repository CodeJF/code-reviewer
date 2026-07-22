"""Configuration for the team web workspace.

Secrets are read from environment variables or the deployment-only .env file;
they are deliberately never read from the repository's tracked configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TeamSettings:
    app_env: str = "development"
    app_url: str = "http://127.0.0.1:8780"
    database_url: str = "sqlite:///./.team/sl100-team.db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    session_secret: str = "development-only-change-me"
    auth_mode: str = "dev"
    session_cookie_name: str = "sl100_session"
    session_ttl_seconds: int = 8 * 60 * 60
    invite_ttl_hours: int = 24
    reset_ttl_minutes: int = 30
    login_failure_limit: int = 5
    login_lock_minutes: int = 15
    diagnosis_rate_limit: int = 10
    diagnosis_rate_window_seconds: int = 10 * 60
    diagnosis_max_active: int = 3
    feishu_webhook_url: str = ""
    diagnosis_ttl_days: int = 90
    audit_ttl_days: int = 365
    app_version: str = "local"
    ai_assisted_enabled: bool = False
    agent_model_id: str = "claude-sonnet-4-6"
    agent_prompt_version: str = "controlled-ops-v1"
    agent_max_turns: int = 3
    agent_max_tool_calls: int = 6
    agent_timeout_seconds: int = 120
    agent_max_input_tokens: int = 32000
    agent_max_output_tokens: int = 4096
    agent_max_tool_result_chars: int = 20000

    @classmethod
    def from_env(cls) -> "TeamSettings":
        return cls(
            app_env=os.environ.get("APP_ENV", "development"),
            app_url=os.environ.get("APP_URL", "http://127.0.0.1:8780").rstrip("/"),
            database_url=os.environ.get("DATABASE_URL", "sqlite:///./.team/sl100-team.db"),
            redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            session_secret=os.environ.get("SESSION_SECRET", "development-only-change-me"),
            auth_mode=os.environ.get("AUTH_MODE", "dev").lower(),
            session_cookie_name=os.environ.get("SESSION_COOKIE_NAME", "sl100_session"),
            session_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", str(8 * 60 * 60))),
            invite_ttl_hours=int(os.environ.get("INVITE_TTL_HOURS", "24")),
            reset_ttl_minutes=int(os.environ.get("RESET_TTL_MINUTES", "30")),
            login_failure_limit=int(os.environ.get("LOGIN_FAILURE_LIMIT", "5")),
            login_lock_minutes=int(os.environ.get("LOGIN_LOCK_MINUTES", "15")),
            diagnosis_rate_limit=int(os.environ.get("DIAGNOSIS_RATE_LIMIT", "10")),
            diagnosis_rate_window_seconds=int(os.environ.get("DIAGNOSIS_RATE_WINDOW_SECONDS", "600")),
            diagnosis_max_active=int(os.environ.get("DIAGNOSIS_MAX_ACTIVE", "3")),
            feishu_webhook_url=os.environ.get("FEISHU_WEBHOOK_URL", ""),
            diagnosis_ttl_days=int(os.environ.get("DIAGNOSIS_TTL_DAYS", "90")),
            audit_ttl_days=int(os.environ.get("AUDIT_TTL_DAYS", "365")),
            app_version=os.environ.get("APP_VERSION", "local"),
            ai_assisted_enabled=os.environ.get("AI_ASSISTED_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            agent_model_id=os.environ.get("AGENT_MODEL_ID", "claude-sonnet-4-6"),
            agent_prompt_version=os.environ.get("AGENT_PROMPT_VERSION", "controlled-ops-v1"),
            agent_max_turns=int(os.environ.get("AGENT_MAX_TURNS", "3")),
            agent_max_tool_calls=int(os.environ.get("AGENT_MAX_TOOL_CALLS", "6")),
            agent_timeout_seconds=int(os.environ.get("AGENT_TIMEOUT_SECONDS", "120")),
            agent_max_input_tokens=int(os.environ.get("AGENT_MAX_INPUT_TOKENS", "32000")),
            agent_max_output_tokens=int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "4096")),
            agent_max_tool_result_chars=int(os.environ.get("AGENT_MAX_TOOL_RESULT_CHARS", "20000")),
        )

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def validate_runtime(self) -> None:
        if self.auth_mode not in {"dev", "local"}:
            raise ValueError("AUTH_MODE must be dev or local")
        if not self.is_production:
            return
        if self.auth_mode != "local":
            raise ValueError("production requires AUTH_MODE=local")
        if not self.app_url.startswith("https://"):
            raise ValueError("production requires an HTTPS APP_URL")
        if not self.database_url.startswith("postgresql+"):
            raise ValueError("production requires PostgreSQL DATABASE_URL")
        if not self.redis_url.startswith("redis://"):
            raise ValueError("production requires Redis REDIS_URL")
        required = {
            "SESSION_SECRET": self.session_secret != "development-only-change-me" and len(self.session_secret) >= 32,
        }
        missing = [name for name, configured in required.items() if not configured]
        if missing:
            raise ValueError(f"production configuration missing: {', '.join(missing)}")
        if self.ai_assisted_enabled and not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("AI_ASSISTED_ENABLED requires ANTHROPIC_API_KEY")
