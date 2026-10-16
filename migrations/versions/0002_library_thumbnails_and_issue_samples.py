"""Persist parsed library chunks, thumbnail receipts and reliability sample denominators.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("personal_lesson_documents", sa.Column("chunks", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("personal_lesson_documents", sa.Column("thumbnail_path", sa.Text(), nullable=True))
    op.add_column("personal_lesson_documents", sa.Column("thumbnail_kind", sa.String(40), nullable=True))
    op.add_column("system_issue_records", sa.Column("record_kind", sa.String(20), nullable=False, server_default="issue"))
    op.add_column("system_issue_records", sa.Column("sample_space", sa.String(100), nullable=True))
    op.add_column("system_issue_records", sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=True))
    op.add_column("system_issue_records", sa.Column("sample_key", sa.String(200), nullable=True))
    for name in ("sample_count", "success_count", "failure_count", "degraded_count", "cancelled_count", "slow_count"):
        op.add_column("system_issue_records", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    op.add_column("system_issue_records", sa.Column("duration_ms_sum", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("system_issue_records", sa.Column("observation_count", sa.Integer(), nullable=False, server_default="1"))
    # A unique index works with nullable sample keys on SQLite and PostgreSQL, avoiding table rebuilds.
    op.create_index("uq_issue_sample_key", "system_issue_records", ["sample_key"], unique=True)
    op.create_index("ix_issue_sample_space_hour", "system_issue_records", ["record_kind", "sample_space", "hour_bucket"])


def downgrade() -> None:
    op.drop_index("ix_issue_sample_space_hour", table_name="system_issue_records")
    op.drop_index("uq_issue_sample_key", table_name="system_issue_records")
    for name in ("observation_count", "duration_ms_sum", "slow_count", "cancelled_count", "degraded_count", "failure_count", "success_count", "sample_count", "sample_key", "hour_bucket", "sample_space", "record_kind"):
        op.drop_column("system_issue_records", name)
    for name in ("thumbnail_kind", "thumbnail_path", "chunks"):
        op.drop_column("personal_lesson_documents", name)
