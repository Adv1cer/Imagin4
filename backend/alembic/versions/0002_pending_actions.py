"""pending_actions: server-side confirmation state for the agentic chat intent router's
paid (POSTER/INFOGRAPHIC) generation flow

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-06
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("billing_category", sa.String(), nullable=False, server_default="paid"),
        sa.Column("normalized_params", postgresql.JSONB(), nullable=False),
        sa.Column("params_fingerprint", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resulting_job_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "action_type in ('poster','infographic')", name="ck_pending_actions_action_type"
        ),
        sa.CheckConstraint(
            "billing_category in ('local','paid')", name="ck_pending_actions_billing"
        ),
        sa.CheckConstraint(
            "status in ('pending','confirmed','cancelled','expired')",
            name="ck_pending_actions_status",
        ),
    )
    op.create_index(
        "ix_pending_actions_user_status", "pending_actions", ["user_id", "status"]
    )
    op.execute(
        "CREATE INDEX ix_pending_actions_conversation_pending ON pending_actions "
        "(conversation_id) WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.drop_table("pending_actions")
