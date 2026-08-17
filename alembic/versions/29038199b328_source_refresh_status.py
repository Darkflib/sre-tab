"""Per-source refresh status.

``source_status`` is scheduler-written runtime state, 1:1 with ``sources``
and deliberately *not* columns on it: ``sources`` is operator-managed
configuration, and keeping the two apart means the operator and the
refresh loop never contend, while ``sources.updated_at`` keeps meaning
"the operator changed the configuration".

Persisting ``last_fetched_at`` is what lets a restarted process, or a
second replica, know when a source was last attempted instead of treating
the whole catalogue as due at once.

The other Phase 2 retention change — bookmarked items are never pruned —
needed no DDL at all. Immunity is a ``NOT EXISTS`` predicate on the delete
in ``app/ingest/store.py``; the ``bookmarks`` table already holds
everything the query needs.

Revision ID: 29038199b328
Revises: d25a61924953
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "29038199b328"
down_revision: str | Sequence[str] | None = "d25a61924953"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_status",
        sa.Column("source_id", sa.Integer(), nullable=False),
        # Last attempt, successful or not.
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(length=64), nullable=True),
        sa.Column("last_error_detail", sa.String(length=500), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_status_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_source_status")),
    )


def downgrade() -> None:
    op.drop_table("source_status")
