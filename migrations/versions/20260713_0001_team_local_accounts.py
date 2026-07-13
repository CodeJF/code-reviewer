"""initial team workspace with local accounts

Revision ID: 20260713_0001
Revises:
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260713_0001"
down_revision = None
branch_labels = None
depends_on = None


role_enum = sa.Enum("ADMIN", "ONCALL", "VIEWER", name="role")
diagnosis_status_enum = sa.Enum("QUEUED", "RUNNING", "COMPLETED", "FAILED", "EXPIRED", name="diagnosisstatus")
incident_status_enum = sa.Enum("OPEN", "INVESTIGATING", "MITIGATED", "RESOLVED", name="incidentstatus")


def upgrade() -> None:
    op.create_table(
        "team_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_team_users_is_active", "team_users", ["is_active"])
    op.create_index("ix_team_users_subject", "team_users", ["subject"], unique=True)
    op.create_index("ix_team_users_username", "team_users", ["username"], unique=True)

    op.create_table(
        "diagnosis_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("retry_of_id", sa.String(length=36), nullable=True),
        sa.Column("query", sa.String(length=1000), nullable=False),
        sa.Column("no_remote", sa.Boolean(), nullable=False),
        sa.Column("status", diagnosis_status_enum, nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["team_users.id"]),
        sa.ForeignKeyConstraint(["retry_of_id"], ["diagnosis_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_diagnosis_jobs_created_at", "diagnosis_jobs", ["created_at"])
    op.create_index("ix_diagnosis_jobs_created_by_id", "diagnosis_jobs", ["created_by_id"])
    op.create_index("ix_diagnosis_jobs_expires_at", "diagnosis_jobs", ["expires_at"])
    op.create_index("ix_diagnosis_jobs_retry_of_id", "diagnosis_jobs", ["retry_of_id"])
    op.create_index("ix_diagnosis_jobs_status", "diagnosis_jobs", ["status"])

    op.create_table(
        "incidents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("diagnosis_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("service", sa.String(length=100), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("status", incident_status_enum, nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("assignee_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["assignee_id"], ["team_users.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["team_users.id"]),
        sa.ForeignKeyConstraint(["diagnosis_id"], ["diagnosis_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incidents_assignee_id", "incidents", ["assignee_id"])
    op.create_index("ix_incidents_created_at", "incidents", ["created_at"])
    op.create_index("ix_incidents_created_by_id", "incidents", ["created_by_id"])
    op.create_index("ix_incidents_diagnosis_id", "incidents", ["diagnosis_id"], unique=True)
    op.create_index("ix_incidents_risk_level", "incidents", ["risk_level"])
    op.create_index("ix_incidents_status", "incidents", ["status"])

    op.create_table(
        "incident_comments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("incident_id", sa.String(length=36), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["team_users.id"]),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incident_comments_author_id", "incident_comments", ["author_id"])
    op.create_index("ix_incident_comments_expires_at", "incident_comments", ["expires_at"])
    op.create_index("ix_incident_comments_incident_id", "incident_comments", ["incident_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["team_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_expires_at", "audit_events", ["expires_at"])
    op.create_index("ix_audit_events_target_id", "audit_events", ["target_id"])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("incident_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notification_deliveries_incident_id", "notification_deliveries", ["incident_id"])
    op.create_index("ix_notification_deliveries_next_attempt_at", "notification_deliveries", ["next_attempt_at"])
    op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"])

    op.create_table(
        "invite_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["team_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invite_tokens_created_by_id", "invite_tokens", ["created_by_id"])
    op.create_index("ix_invite_tokens_expires_at", "invite_tokens", ["expires_at"])
    op.create_index("ix_invite_tokens_token_hash", "invite_tokens", ["token_hash"], unique=True)
    op.create_index("ix_invite_tokens_username", "invite_tokens", ["username"])

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["team_users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["team_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_password_reset_tokens_created_by_id", "password_reset_tokens", ["created_by_id"])
    op.create_index("ix_password_reset_tokens_expires_at", "password_reset_tokens", ["expires_at"])
    op.create_index("ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True)
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["team_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_index("ix_user_sessions_revoked_at", "user_sessions", ["revoked_at"])
    op.create_index("ix_user_sessions_session_id_hash", "user_sessions", ["session_id_hash"], unique=True)
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    op.create_table(
        "login_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["team_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_login_audits_created_at", "login_audits", ["created_at"])
    op.create_index("ix_login_audits_expires_at", "login_audits", ["expires_at"])
    op.create_index("ix_login_audits_success", "login_audits", ["success"])
    op.create_index("ix_login_audits_user_id", "login_audits", ["user_id"])
    op.create_index("ix_login_audits_username", "login_audits", ["username"])


def downgrade() -> None:
    op.drop_table("login_audits")
    op.drop_table("user_sessions")
    op.drop_table("password_reset_tokens")
    op.drop_table("invite_tokens")
    op.drop_table("notification_deliveries")
    op.drop_table("audit_events")
    op.drop_table("incident_comments")
    op.drop_table("incidents")
    op.drop_table("diagnosis_jobs")
    op.drop_table("team_users")
    bind = op.get_bind()
    incident_status_enum.drop(bind, checkfirst=True)
    diagnosis_status_enum.drop(bind, checkfirst=True)
    role_enum.drop(bind, checkfirst=True)
