"""Persist dispatch retry budgets and recovery alert lifecycle.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("domain_outbox_events", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("domain_outbox_events", sa.Column("last_error_code", sa.String(80), nullable=True))
    op.add_column("domain_outbox_events", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("domain_outbox_events", sa.Column("dispatch_deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table("recovery_alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False, unique=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("ai_generation_runs.id"), nullable=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'acknowledged', 'resolved')", name="ck_recovery_alert_status"))
    for column in ("run_id", "code", "status", "last_seen_at"):
        op.create_index(f"ix_recovery_alerts_{column}", "recovery_alerts", [column])


def downgrade():
    for column in ("last_seen_at", "status", "code", "run_id"):
        op.drop_index(f"ix_recovery_alerts_{column}", table_name="recovery_alerts")
    op.drop_table("recovery_alerts")
    for column in ("dispatch_deadline_at", "last_error_at", "last_error_code", "last_attempt_at"):
        op.drop_column("domain_outbox_events", column)
