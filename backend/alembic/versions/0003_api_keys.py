"""api_keys: machine-to-machine bearer credentials, distinct from human auth_sessions
(see app/domain/auth/api_keys.py, app/api/deps.py:get_current_user). Also adds
conversations.external_ref so an external caller (e.g. a university chatbot workflow
forwarding many different real end users through one shared API key) can map its own
per-end-user identifier onto a distinct conversation in this system rather than every
forwarded message sharing one conversation/history.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
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
        # sha256 hex digest, same scheme as auth_sessions.token_hash -- the raw key is
        # shown to the operator exactly once at creation time (see
        # backend/scripts/create_api_key.py) and never persisted or logged anywhere.
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_user", "api_keys", ["user_id"])
    op.execute("CREATE INDEX ix_api_keys_active ON api_keys (key_hash) WHERE revoked_at IS NULL")

    op.add_column("conversations", sa.Column("external_ref", sa.String(), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX uq_conversations_user_external_ref ON conversations "
        "(user_id, external_ref) WHERE external_ref IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_conversations_user_external_ref")
    op.drop_column("conversations", "external_ref")
    op.execute("DROP INDEX IF EXISTS ix_api_keys_active")
    op.drop_index("ix_api_keys_user", table_name="api_keys")
    op.drop_table("api_keys")
