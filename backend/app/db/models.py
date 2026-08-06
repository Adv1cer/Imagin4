"""SQLAlchemy ORM models for every table in the platform.

All primary keys are UUIDs (server-generated via gen_random_uuid() at the DB level,
provisioned by the pgcrypto/pg_uuid extension in the initial migration). All timestamps
are timezone-aware (timestamptz). Binary image data is never stored here -- only
object-storage references (see `assets.object_key`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def UUID_PK():
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def TS():
    return mapped_column(server_default=text("now()"))


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = UUID_PK()
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")
    plan_code: Mapped[str] = mapped_column(String, nullable=False, server_default="standard")
    created_at: Mapped[datetime] = TS()
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), onupdate=text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint("status in ('active','suspended','deleted')", name="ck_users_status"),
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = UUID_PK()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = TS()
    last_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_auth_sessions_user_expires", "user_id", "expires_at", postgresql_using="btree"),
        Index(
            "ix_auth_sessions_active",
            "user_id",
            postgresql_where=text("revoked_at is null"),
        ),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = UUID_PK()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False, server_default="New conversation")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")
    created_at: Mapped[datetime] = TS()
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), onupdate=text("now()")
    )
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('active','archived','deleted')", name="ck_conversations_status"
        ),
        Index(
            "ix_conversations_user_updated",
            "user_id",
            text("updated_at desc"),
            postgresql_where=text("deleted_at is null"),
        ),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = UUID_PK()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="complete")
    model_name: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = TS()

    __table_args__ = (
        CheckConstraint(
            "role in ('user','assistant','system','tool')", name="ck_chat_messages_role"
        ),
        UniqueConstraint("conversation_id", "sequence_no", name="uq_chat_messages_conv_seq"),
        Index(
            "uq_chat_messages_conv_client_id",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id is not null"),
        ),
        Index("ix_chat_messages_conv_seq", "conversation_id", "sequence_no"),
    )


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[uuid.UUID] = UUID_PK()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    request_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    model_family: Mapped[str] = mapped_column(String, nullable=False)
    workflow_version: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    effective_priority: Mapped[float] = mapped_column(nullable=False, server_default="0")
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    input_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    assigned_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("comfy_workers.id", ondelete="SET NULL"), nullable=True
    )
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    queued_at: Mapped[datetime] = TS()
    available_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail_sanitized: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state in ('queued','admitted','dispatched','running','retry_wait',"
            "'cancelling','cancelled','succeeded','failed')",
            name="ck_generation_jobs_state",
        ),
        UniqueConstraint(
            "user_id", "idempotency_key", "kind", name="uq_generation_jobs_user_idem_kind"
        ),
        Index(
            "ix_generation_jobs_queue",
            text("effective_priority desc"),
            "queued_at",
            postgresql_where=text("state in ('queued','retry_wait')"),
        ),
        Index("ix_generation_jobs_user_history", "user_id", text("queued_at desc")),
        Index("ix_generation_jobs_conversation_history", "conversation_id", text("queued_at desc")),
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"

    id: Mapped[uuid.UUID] = UUID_PK()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("comfy_workers.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(nullable=False)
    comfy_prompt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    submitted_at: Mapped[datetime] = TS()
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (UniqueConstraint("job_id", "attempt_no", name="uq_job_attempts_job_attempt"),)


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = TS()

    __table_args__ = (
        UniqueConstraint("job_id", "sequence_no", name="uq_job_events_job_seq"),
        Index("ix_job_events_job_seq", "job_id", "sequence_no"),
    )


class ComfyWorker(Base):
    __tablename__ = "comfy_workers"

    id: Mapped[uuid.UUID] = UUID_PK()
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    endpoint_ref: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="offline")
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    max_slots: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    reserved_slots: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    running_slots: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    draining_at: Mapped[datetime | None] = mapped_column(nullable=True)
    recent_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    recent_failure_window_started_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('online','draining','offline')", name="ck_comfy_workers_status"
        ),
        CheckConstraint(
            "reserved_slots >= 0 and reserved_slots <= max_slots", name="ck_reserved_slots"
        ),
        CheckConstraint(
            "running_slots >= 0 and running_slots <= max_slots", name="ck_running_slots"
        ),
    )


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = UUID_PK()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    object_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = TS()
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (Index("ix_assets_owner", "owner_user_id", text("created_at desc")),)


class PendingAction(Base):
    """Server-side pending paid-action record for the agentic chat intent router (see
    app/domain/chat/routing.py, app/api/v1/chat_router.py). POSTER/INFOGRAPHIC requests
    never call the paid Gemini image API directly off an LLM routing decision -- they
    create a row here first (status="pending") and only actually enqueue a generation
    job once the authenticated owner confirms via POST /v1/pending-actions/{id}/confirm,
    which does a conditional UPDATE ... WHERE status='pending' AND expires_at > now()
    (see confirm_pending_action) so the transition is atomic: two concurrent confirm
    clicks/retries can only ever have one winner, and job admission itself is additionally
    idempotency-keyed off this row's id as defense in depth."""

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = UUID_PK()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # "poster" | "infographic" -- display/audit distinction only; both currently execute
    # through the same allowlisted "poster_infographic" workflow (backend=gemini) since
    # that's the one real pipeline this repo has for paid image generation today. See
    # app/domain/jobs/workflow_registry.py.
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    # Always "paid" today (derived server-side by
    # app.domain.chat.routing.derive_billing_category, never trusted from the LLM) --
    # stored explicitly so it's visible in an audit query without re-deriving it, and so
    # a future local-billing pending-action type doesn't require a schema change.
    billing_category: Mapped[str] = mapped_column(String, nullable=False, server_default="paid")
    normalized_params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # sha256 of normalized_params (see compute_params_fingerprint) -- confirm() re-derives
    # this from the row's own stored params, so this column is mostly a defensive
    # assertion aid / audit trail rather than load-bearing on its own.
    params_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = TS()
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # The in-memory JobQueue's job id once execution actually starts (see
    # app/domain/jobs/admission.py) -- plain string, not a FK, because the in-memory
    # queue's job ids are not currently backed by rows in `generation_jobs` (that table
    # exists for the future Postgres-backed queue; see README "Known limitations").
    # Populated atomically alongside status="confirmed" so a replayed confirm() request
    # can return the same job instead of erroring or double-enqueueing.
    resulting_job_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "action_type in ('poster','infographic')", name="ck_pending_actions_action_type"
        ),
        CheckConstraint("billing_category in ('local','paid')", name="ck_pending_actions_billing"),
        CheckConstraint(
            "status in ('pending','confirmed','cancelled','expired')",
            name="ck_pending_actions_status",
        ),
        Index("ix_pending_actions_user_status", "user_id", "status"),
        Index(
            "ix_pending_actions_conversation_pending",
            "conversation_id",
            postgresql_where=text("status = 'pending'"),
        ),
    )


class SchedulerLease(Base):
    """Small fairness/coordination table so multiple scheduler replicas can safely
    round-robin admission without a distributed lock service."""

    __tablename__ = "scheduler_leases"

    id: Mapped[uuid.UUID] = UUID_PK()
    lease_name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    holder: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), onupdate=text("now()")
    )
