"""controlled Agent execution, traces, and feedback

Revision ID: 20260716_0002
Revises: 20260713_0001
Create Date: 2026-07-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260716_0002"
down_revision = "20260713_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE diagnosisstatus ADD VALUE IF NOT EXISTS 'PLANNED' BEFORE 'QUEUED'")
    op.add_column("diagnosis_jobs", sa.Column("plan_json", sa.JSON(), nullable=True))
    op.add_column("diagnosis_jobs", sa.Column("plan_digest", sa.String(length=64), server_default="", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("execution_mode", sa.String(length=32), server_default="rules", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("external_ai_approved_by_id", sa.String(length=36), nullable=True))
    op.add_column("diagnosis_jobs", sa.Column("external_ai_approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("diagnosis_jobs", sa.Column("report_schema_version", sa.String(length=16), server_default="1.0", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("result_status", sa.String(length=32), server_default="", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("model_id", sa.String(length=128), server_default="", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("prompt_version", sa.String(length=64), server_default="", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("tool_call_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False))
    op.add_column("diagnosis_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_diagnosis_jobs_external_ai_approved_by",
        "diagnosis_jobs",
        "team_users",
        ["external_ai_approved_by_id"],
        ["id"],
    )
    op.create_index("ix_diagnosis_jobs_execution_mode", "diagnosis_jobs", ["execution_mode"])
    op.create_index("ix_diagnosis_jobs_external_ai_approved_by_id", "diagnosis_jobs", ["external_ai_approved_by_id"])
    op.create_index("ix_diagnosis_jobs_result_status", "diagnosis_jobs", ["result_status"])

    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("diagnosis_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["diagnosis_id"], ["diagnosis_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("diagnosis_id", "sequence", name="uq_agent_tool_call_sequence"),
    )
    op.create_index("ix_agent_tool_calls_diagnosis_id", "agent_tool_calls", ["diagnosis_id"])
    op.create_index("ix_agent_tool_calls_expires_at", "agent_tool_calls", ["expires_at"])
    op.create_index("ix_agent_tool_calls_status", "agent_tool_calls", ["status"])
    op.create_index("ix_agent_tool_calls_tool_name", "agent_tool_calls", ["tool_name"])

    op.create_table(
        "diagnosis_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("diagnosis_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("rating", sa.String(length=32), nullable=False),
        sa.Column("evidence_correct", sa.Boolean(), nullable=True),
        sa.Column("corrected_incident_types_json", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["diagnosis_id"], ["diagnosis_jobs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["team_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("diagnosis_id", "user_id", name="uq_diagnosis_feedback_user"),
    )
    op.create_index("ix_diagnosis_feedback_diagnosis_id", "diagnosis_feedback", ["diagnosis_id"])
    op.create_index("ix_diagnosis_feedback_rating", "diagnosis_feedback", ["rating"])
    op.create_index("ix_diagnosis_feedback_user_id", "diagnosis_feedback", ["user_id"])


def downgrade() -> None:
    op.drop_table("diagnosis_feedback")
    op.drop_table("agent_tool_calls")
    op.drop_index("ix_diagnosis_jobs_result_status", table_name="diagnosis_jobs")
    op.drop_index("ix_diagnosis_jobs_external_ai_approved_by_id", table_name="diagnosis_jobs")
    op.drop_index("ix_diagnosis_jobs_execution_mode", table_name="diagnosis_jobs")
    op.drop_constraint("fk_diagnosis_jobs_external_ai_approved_by", "diagnosis_jobs", type_="foreignkey")
    for column in [
        "started_at",
        "duration_ms",
        "tool_call_count",
        "output_tokens",
        "input_tokens",
        "prompt_version",
        "model_id",
        "result_status",
        "report_schema_version",
        "external_ai_approved_at",
        "external_ai_approved_by_id",
        "execution_mode",
        "plan_digest",
        "plan_json",
    ]:
        op.drop_column("diagnosis_jobs", column)
