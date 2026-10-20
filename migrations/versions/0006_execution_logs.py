"""Persistent structured execution diagnostics with independent append-only cursors."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("ai_execution_logs",
        sa.Column("sequence", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("ai_generation_runs.id"), nullable=False),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(36), nullable=False),
        sa.Column("span_id", sa.String(36), nullable=False),
        sa.Column("parent_span_id", sa.String(36), nullable=True),
        sa.Column("phase", sa.String(10), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("run_type", sa.String(20), nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("execution_epoch >= 0 AND (elapsed_ms IS NULL OR elapsed_ms >= 0)", name="ck_execution_log_counts"),
        sa.CheckConstraint("phase IN ('start', 'end', 'event')", name="ck_execution_log_phase"),
        sa.CheckConstraint("level IN ('info', 'warning', 'error')", name="ck_execution_log_level"),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed', 'cancelled', 'lease_lost', 'interrupted')", name="ck_execution_log_status"),
        sqlite_autoincrement=True)
    op.create_index("ix_execution_logs_run_sequence", "ai_execution_logs", ["run_id", "sequence"])
    op.create_index("ix_ai_execution_logs_trace_id", "ai_execution_logs", ["trace_id"])
    op.create_index("ix_ai_execution_logs_created_at", "ai_execution_logs", ["created_at"])


def downgrade():
    op.drop_index("ix_ai_execution_logs_created_at", table_name="ai_execution_logs")
    op.drop_index("ix_ai_execution_logs_trace_id", table_name="ai_execution_logs")
    op.drop_index("ix_execution_logs_run_sequence", table_name="ai_execution_logs")
    op.drop_table("ai_execution_logs")
