"""initial schema: users, auth_sessions, conversations, chat_messages,
generation_jobs, job_attempts, job_events, comfy_workers, assets, scheduler_leases

Revision ID: 0001
Revises:
Create Date: 2026-08-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')
    op.execute('CREATE EXTENSION IF NOT EXISTS citext')

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("plan_code", sa.String(), nullable=False, server_default="standard"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('active','suspended','deleted')", name="ck_users_status"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_hash", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index("ix_auth_sessions_user_expires", "auth_sessions", ["user_id", sa.text("expires_at desc")])
    op.execute(
        "CREATE INDEX ix_auth_sessions_active ON auth_sessions (user_id) WHERE revoked_at IS NULL"
    )

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default="New conversation"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('active','archived','deleted')", name="ck_conversations_status"),
    )
    op.execute(
        "CREATE INDEX ix_conversations_user_updated ON conversations (user_id, updated_at desc) "
        "WHERE deleted_at IS NULL"
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("client_message_id", sa.String(), nullable=True),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="complete"),
        sa.Column("model_name", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role in ('user','assistant','system','tool')", name="ck_chat_messages_role"),
        sa.UniqueConstraint("conversation_id", "sequence_no", name="uq_chat_messages_conv_seq"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_chat_messages_conv_client_id ON chat_messages (conversation_id, client_message_id) "
        "WHERE client_message_id IS NOT NULL"
    )
    op.create_index("ix_chat_messages_conv_seq", "chat_messages", ["conversation_id", "sequence_no"])

    op.create_table(
        "comfy_workers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("endpoint_ref", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="offline"),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("max_slots", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reserved_slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("running_slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draining_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recent_failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recent_failure_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('online','draining','offline')", name="ck_comfy_workers_status"),
        sa.CheckConstraint("reserved_slots >= 0 and reserved_slots <= max_slots", name="ck_reserved_slots"),
        sa.CheckConstraint("running_slots >= 0 and running_slots <= max_slots", name="ck_running_slots"),
        sa.UniqueConstraint("name", name="uq_comfy_workers_name"),
    )

    op.create_table(
        "generation_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("request_message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("model_family", sa.String(), nullable=False),
        sa.Column("workflow_version", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effective_priority", sa.Float(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("input_payload", postgresql.JSONB(), nullable=False),
        sa.Column("assigned_worker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("comfy_workers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("current_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_detail_sanitized", sa.String(), nullable=True),
        sa.CheckConstraint(
            "state in ('queued','admitted','dispatched','running','retry_wait',"
            "'cancelling','cancelled','succeeded','failed')",
            name="ck_generation_jobs_state",
        ),
        sa.UniqueConstraint("user_id", "idempotency_key", "kind", name="uq_generation_jobs_user_idem_kind"),
    )
    op.execute(
        "CREATE INDEX ix_generation_jobs_queue ON generation_jobs (effective_priority desc, queued_at) "
        "WHERE state in ('queued','retry_wait')"
    )
    op.execute("CREATE INDEX ix_generation_jobs_user_history ON generation_jobs (user_id, queued_at desc)")
    op.execute(
        "CREATE INDEX ix_generation_jobs_conversation_history ON generation_jobs (conversation_id, queued_at desc)"
    )

    op.create_table(
        "job_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("comfy_workers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comfy_prompt_id", sa.String(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_job_attempts_job_attempt"),
    )

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("job_id", "sequence_no", name="uq_job_events_job_seq"),
    )
    op.create_index("ix_job_events_job_seq", "job_events", ["job_id", "sequence_no"])

    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("object_key", name="uq_assets_object_key"),
    )
    op.create_index("ix_assets_owner", "assets", ["owner_user_id", sa.text("created_at desc")])

    op.create_table(
        "scheduler_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lease_name", sa.String(), nullable=False),
        sa.Column("holder", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("lease_name", name="uq_scheduler_leases_name"),
    )


def downgrade() -> None:
    op.drop_table("scheduler_leases")
    op.drop_table("assets")
    op.drop_table("job_events")
    op.drop_table("job_attempts")
    op.drop_table("generation_jobs")
    op.drop_table("chat_messages")
    op.drop_table("conversations")
    op.drop_table("auth_sessions")
    op.drop_table("comfy_workers")
    op.drop_table("users")
