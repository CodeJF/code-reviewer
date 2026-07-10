"""Configuration for the team web workspace.

Secrets are read from environment variables or the deployment-only .env file;
they are deliberately never read from the repository's tracked configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _csv(name: str) -> set[str]:
    return {item.strip() for item in os.environ.get(name, "").split(",") if item.strip()}


@dataclass(frozen=True)
class TeamSettings:
    app_env: str = "development"
    app_url: str = "http://127.0.0.1:8780"
    database_url: str = "sqlite:///./.team/sl100-team.db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    session_secret: str = "development-only-change-me"
    auth_mode: str = "dev"
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_groups_claim: str = "groups"
    admin_groups: frozenset[str] = frozenset()
    oncall_groups: frozenset[str] = frozenset()
    feishu_webhook_url: str = ""
    diagnosis_ttl_days: int = 90
    audit_ttl_days: int = 365

    @classmethod
    def from_env(cls) -> "TeamSettings":
        return cls(
            app_env=os.environ.get("APP_ENV", "development"),
            app_url=os.environ.get("APP_URL", "http://127.0.0.1:8780").rstrip("/"),
            database_url=os.environ.get("DATABASE_URL", "sqlite:///./.team/sl100-team.db"),
            redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            session_secret=os.environ.get("SESSION_SECRET", "development-only-change-me"),
            auth_mode=os.environ.get("AUTH_MODE", "dev").lower(),
            oidc_discovery_url=os.environ.get("OIDC_DISCOVERY_URL", ""),
            oidc_client_id=os.environ.get("OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET", ""),
            oidc_groups_claim=os.environ.get("OIDC_GROUPS_CLAIM", "groups"),
            admin_groups=frozenset(_csv("OIDC_ADMIN_GROUPS")),
            oncall_groups=frozenset(_csv("OIDC_ONCALL_GROUPS")),
            feishu_webhook_url=os.environ.get("FEISHU_WEBHOOK_URL", ""),
            diagnosis_ttl_days=int(os.environ.get("DIAGNOSIS_TTL_DAYS", "90")),
            audit_ttl_days=int(os.environ.get("AUDIT_TTL_DAYS", "365")),
        )

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def validate_runtime(self) -> None:
        if self.auth_mode not in {"dev", "oidc"}:
            raise ValueError("AUTH_MODE must be dev or oidc")
        if not self.is_production:
            return
        if self.auth_mode != "oidc":
            raise ValueError("production requires AUTH_MODE=oidc")
        if not self.app_url.startswith("https://"):
            raise ValueError("production requires an HTTPS APP_URL")
        required = {
            "SESSION_SECRET": self.session_secret != "development-only-change-me",
            "OIDC_DISCOVERY_URL": bool(self.oidc_discovery_url),
            "OIDC_CLIENT_ID": bool(self.oidc_client_id),
            "OIDC_CLIENT_SECRET": bool(self.oidc_client_secret),
        }
        missing = [name for name, configured in required.items() if not configured]
        if missing:
            raise ValueError(f"production configuration missing: {', '.join(missing)}")
