"""Durable raw model receipts, independently committed before lesson application."""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("llm_invocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("ai_generation_runs.id"), nullable=False),
        sa.Column("invocation_key", sa.String(64), nullable=False, unique=True),
        sa.Column("step_key", sa.String(200), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("call_id", sa.String(80), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("module_ids", sa.JSON(), nullable=False),
        sa.Column("route", sa.String(60), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("capture_epoch", sa.Integer(), nullable=False),
        sa.Column("capture_owner", sa.String(36), nullable=False),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("response_hash", sa.String(64), nullable=True),
        sa.Column("response_log", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("response_saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "step_key", "attempt", name="uq_llm_invocation_step_attempt"),
        sa.UniqueConstraint("run_id", "call_id", name="uq_llm_invocation_call"),
        sa.CheckConstraint("attempt >= 1 AND call_index >= 1 AND capture_epoch >= 0", name="ck_llm_invocation_counts"),
        sa.CheckConstraint("status IN ('in_flight', 'response_saved', 'applied', 'failed', 'outcome_unknown')", name="ck_llm_invocation_status"),
        sa.CheckConstraint("status NOT IN ('response_saved', 'applied') OR (response_body IS NOT NULL AND response_hash IS NOT NULL)",
            name="ck_llm_invocation_response"))
    op.create_index("ix_llm_invocations_run_id", "llm_invocations", ["run_id"])
    op.create_index("ix_llm_invocations_status", "llm_invocations", ["status"])


def downgrade():
    op.drop_index("ix_llm_invocations_status", table_name="llm_invocations")
    op.drop_index("ix_llm_invocations_run_id", table_name="llm_invocations")
    op.drop_table("llm_invocations")
