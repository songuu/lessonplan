"""PostgreSQL 业务事实与 SQLite 便携验证共用模型。

JSON 字段采用完整重新赋值，避免 ORM 漏掉嵌套修改；业务提交由 service 负责。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class UTCDateTime(TypeDecorator):
    """SQLite drops timezone metadata; restore UTC on read consistently with PostgreSQL."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class IdentityMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class User(IdentityMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    name: Mapped[str] = mapped_column(String(80), default="教师")
    tenant_id: Mapped[str] = mapped_column(String(36), default=new_id, index=True)
    role: Mapped[str] = mapped_column(String(20), default="teacher")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (CheckConstraint("role IN ('teacher', 'admin')", name="ck_user_role"),)


class TokenRevocation(Base):
    __tablename__ = "auth_token_revocations"

    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class Draft(IdentityMixin, Base):
    __tablename__ = "ai_lesson_drafts"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    input_hash: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(200))
    school_stage: Mapped[str] = mapped_column(String(30), default="primary")
    grade: Mapped[str] = mapped_column(String(30), default="grade_3")
    subject: Mapped[str] = mapped_column(String(40), default="math")
    lesson_type: Mapped[str] = mapped_column(String(40), default="new_lesson")
    total_duration_min: Mapped[int] = mapped_column(Integer, default=40)
    requirements: Mapped[str] = mapped_column(Text, default="")
    knowledge_base_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    knowledge_context_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    generation_mode: Mapped[str] = mapped_column(String(30), default="one_click")
    status: Mapped[str] = mapped_column(String(30), default="draft")
    completion_status: Mapped[str] = mapped_column(String(30), default="incomplete")
    content_version: Mapped[int] = mapped_column(Integer, default=1)
    module_config_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    modules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    current_module_version_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    module_state_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_blueprint_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    style_intent_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hard_gate_status: Mapped[str] = mapped_column(String(30), default="pending")
    semantic_review_status: Mapped[str] = mapped_column(String(30), default="not_started")
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_draft_user_request"),
        CheckConstraint("content_version >= 1", name="ck_draft_version_positive"),
        CheckConstraint("total_duration_min > 0", name="ck_draft_duration_positive"),
    )


class ModuleConfig(IdentityMixin, Base):
    __tablename__ = "ai_lesson_module_config_versions"
    draft_id: Mapped[str] = mapped_column(ForeignKey("ai_lesson_drafts.id"), index=True)
    modules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class Blueprint(IdentityMixin, Base):
    __tablename__ = "ai_lesson_blueprint_versions"
    draft_id: Mapped[str] = mapped_column(ForeignKey("ai_lesson_drafts.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))


class ModuleVersion(IdentityMixin, Base):
    __tablename__ = "ai_lesson_module_versions"
    draft_id: Mapped[str] = mapped_column(ForeignKey("ai_lesson_drafts.id"), index=True)
    module_id: Mapped[str] = mapped_column(String(80))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    parent_version_id: Mapped[str | None] = mapped_column(ForeignKey("ai_lesson_module_versions.id"), nullable=True)
    canonical_content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    teacher_view_content: Mapped[str] = mapped_column(Text, default="")
    teacher_view_format: Mapped[str] = mapped_column(String(20), default="markdown")
    source: Mapped[str] = mapped_column(String(40), default="ai_initial")
    confirmation_status: Mapped[str] = mapped_column(String(20), default="unconfirmed")
    lifecycle_status: Mapped[str] = mapped_column(String(20), default="committed")
    actual_dependency_version_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    semantic_invalidation_sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    unresolved_dependency_types: Mapped[list[Any]] = mapped_column(JSON, default=list)
    teacher_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    hard_gate_status: Mapped[str] = mapped_column(String(30), default="pending")
    __table_args__ = (
        UniqueConstraint("draft_id", "module_id", "version_no", name="uq_module_version_no"),
        CheckConstraint("version_no >= 1", name="ck_module_version_positive"),
    )


class Run(IdentityMixin, Base):
    __tablename__ = "ai_generation_runs"
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("ai_lesson_drafts.id"), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    workflow_type: Mapped[str] = mapped_column(String(50))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    input_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    result_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    job_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    queue_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    execution_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_run_user_idempotency"),
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'cancelled')", name="ck_run_status"),
        Index(
            "uq_active_foreground_run", "draft_id", unique=True,
            sqlite_where=text("status IN ('queued', 'running') AND workflow_type IN ('one_click', 'interactive_step', 'module_polish_message', 'reflection_polish_message')"),
            postgresql_where=text("status IN ('queued', 'running') AND workflow_type IN ('one_click', 'interactive_step', 'module_polish_message', 'reflection_polish_message')"),
        ),
    )


class RunCheckpoint(IdentityMixin, Base):
    __tablename__ = "ai_run_checkpoints"
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_generation_runs.id"), index=True)
    checkpoint_key: Mapped[str] = mapped_column(String(180))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("run_id", "checkpoint_key", name="uq_run_checkpoint_key"),)


class LLMInvocation(IdentityMixin, Base):
    __tablename__ = "llm_invocations"
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_generation_runs.id"), index=True)
    invocation_key: Mapped[str] = mapped_column(String(64), unique=True)
    step_key: Mapped[str] = mapped_column(String(200))
    attempt: Mapped[int] = mapped_column(Integer)
    context_fingerprint: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    call_id: Mapped[str] = mapped_column(String(80))
    call_index: Mapped[int] = mapped_column(Integer)
    module_ids: Mapped[list] = mapped_column(JSON, default=list)
    route: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(30), default="in_flight", index=True)
    capture_epoch: Mapped[int] = mapped_column(Integer)
    capture_owner: Mapped[str] = mapped_column(String(36))
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_log: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    response_saved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "step_key", "attempt", name="uq_llm_invocation_step_attempt"),
        UniqueConstraint("run_id", "call_id", name="uq_llm_invocation_call"),
        CheckConstraint("attempt >= 1 AND call_index >= 1 AND capture_epoch >= 0", name="ck_llm_invocation_counts"),
        CheckConstraint("status IN ('in_flight', 'response_saved', 'applied', 'failed', 'outcome_unknown')", name="ck_llm_invocation_status"),
        CheckConstraint("status NOT IN ('response_saved', 'applied') OR (response_body IS NOT NULL AND response_hash IS NOT NULL)",
            name="ck_llm_invocation_response"),
    )


class KnowledgeBase(IdentityMixin, Base):
    __tablename__ = "knowledge_bases"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class KnowledgeDocument(IdentityMixin, Base):
    __tablename__ = "knowledge_documents"
    base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="uploaded", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("ai_generation_runs.id"), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class KnowledgeChunk(IdentityMixin, Base):
    __tablename__ = "knowledge_chunks"
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    search_text: Mapped[str] = mapped_column(Text)
    token_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    token_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("document_id", "ordinal", name="uq_knowledge_chunk_ordinal"),)


class Event(IdentityMixin, Base):
    __tablename__ = "ai_generation_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_generation_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_event_run_sequence"),
        Index("uq_event_terminal_run", "run_id", unique=True,
              sqlite_where=text("event_type IN ('run_completed', 'run_failed', 'run_cancelled')"),
              postgresql_where=text("event_type IN ('run_completed', 'run_failed', 'run_cancelled')")),
    )


class Outbox(IdentityMixin, Base):
    __tablename__ = "domain_outbox_events"
    run_id: Mapped[str | None] = mapped_column(ForeignKey("ai_generation_runs.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    dispatch_deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class RecoveryAlert(IdentityMixin, Base):
    __tablename__ = "recovery_alerts"
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("ai_generation_runs.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    message: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (CheckConstraint("status IN ('open', 'acknowledged', 'resolved')", name="ck_recovery_alert_status"),)


class Snapshot(IdentityMixin, Base):
    __tablename__ = "lesson_assembly_snapshots"
    draft_id: Mapped[str] = mapped_column(ForeignKey("ai_lesson_drafts.id"), index=True)
    module_version_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)


class Export(IdentityMixin, Base):
    __tablename__ = "lesson_export_records"
    draft_id: Mapped[str] = mapped_column(ForeignKey("ai_lesson_drafts.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("lesson_assembly_snapshots.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Reflection(IdentityMixin, Base):
    __tablename__ = "teacher_reflection_records"
    draft_id: Mapped[str] = mapped_column(ForeignKey("ai_lesson_drafts.id"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class PolishSession(IdentityMixin, Base):
    __tablename__ = "ai_module_polish_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("ai_lesson_drafts.id"), index=True)
    module_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), default="module")
    scope: Mapped[str] = mapped_column(String(30), default="full")
    base_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    selection_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selection_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate: Mapped[str | None] = mapped_column(Text, nullable=True)


class PolishMessage(IdentityMixin, Base):
    __tablename__ = "ai_module_polish_messages"
    session_id: Mapped[str] = mapped_column(ForeignKey("ai_module_polish_sessions.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_generation_runs.id"), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    working_content: Mapped[str] = mapped_column(Text, default="")
    candidate: Mapped[str | None] = mapped_column(Text, nullable=True)


class PersonalLesson(IdentityMixin, Base):
    __tablename__ = "personal_lesson_documents"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="uploaded", index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    chunks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    school_stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(30), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lesson_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    style_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class QualityResult(IdentityMixin, Base):
    __tablename__ = "quality_check_results"
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_generation_runs.id"), index=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("ai_lesson_drafts.id"), index=True)
    module_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    gate: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30))
    findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), default="")


class CallLog(IdentityMixin, Base):
    __tablename__ = "ai_call_logs"
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_generation_runs.id"), index=True)
    component_name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(80))
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80), default="v1")
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="success")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Issue(IdentityMixin, Base):
    __tablename__ = "system_issue_records"
    run_id: Mapped[str | None] = mapped_column(ForeignKey("ai_generation_runs.id"), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    subsystem: Mapped[str] = mapped_column(String(80))
    issue_type: Mapped[str] = mapped_column(String(80))
    issue_code: Mapped[str] = mapped_column(String(100), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    record_kind: Mapped[str] = mapped_column(String(20), default="issue", server_default="issue")
    sample_space: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hour_bucket: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    sample_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    success_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    degraded_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cancelled_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    slow_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duration_ms_sum: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    observation_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    __table_args__ = (
        Index("ix_issue_sample_space_hour", "record_kind", "sample_space", "hour_bucket"),
        Index("uq_issue_sample_key", "sample_key", unique=True),
    )


class Audit(IdentityMixin, Base):
    __tablename__ = "admin_audit_records"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AnalyticsEvent(IdentityMixin, Base):
    __tablename__ = "analytics_events"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    event_name: Mapped[str] = mapped_column(String(100), index=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ExecutionLog(Base):
    """Append-only diagnostic events, independent of worker ownership and business commits."""

    __tablename__ = "ai_execution_logs"

    sequence: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_generation_runs.id"))
    execution_epoch: Mapped[int] = mapped_column(Integer)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    span_id: Mapped[str] = mapped_column(String(36))
    parent_span_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    phase: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(120))
    run_type: Mapped[str] = mapped_column(String(20))
    level: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    __table_args__ = (
        Index("ix_execution_logs_run_sequence", "run_id", "sequence"),
        CheckConstraint("execution_epoch >= 0 AND (elapsed_ms IS NULL OR elapsed_ms >= 0)", name="ck_execution_log_counts"),
        CheckConstraint("phase IN ('start', 'end', 'event')", name="ck_execution_log_phase"),
        CheckConstraint("level IN ('info', 'warning', 'error')", name="ck_execution_log_level"),
        CheckConstraint("status IN ('running', 'completed', 'failed', 'cancelled', 'lease_lost', 'interrupted')", name="ck_execution_log_status"),
        {"sqlite_autoincrement": True},
    )
