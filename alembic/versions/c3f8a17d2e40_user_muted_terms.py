"""Muted words and tags.

``user_muted_terms`` holds the terms whose items a user does not want in
their feed. A table rather than a JSON column on ``user_preferences``
because the predicate that reads it runs in SQL inside the feed's own
statement, and a JSON array is not something the two supported engines
agree about how to join.

One table, one revision, generated deliberately — the distinction
AGENTS.md draws, and the same one ``api_tokens`` was added under.

The primary key is composite over all three columns, which is the
idempotence ``user_preference_topics`` has: saving the same mute twice is
one row rather than two, so a client retrying a save cannot duplicate
anything. That also means no surrogate id and no separate unique
constraint to keep in step with it.

``kind`` carries a CHECK constraint for the reason ``api_tokens.scope``
records: ``Enum(native_enum=False)`` renders as ``VARCHAR`` and emits no
constraint of its own, so without it a restore could store a kind the
application does not know and the row would fail to materialise with
``LookupError`` on the next feed request. The constraint's name comes
from the naming convention in ``app.db.models`` via ``op.f()``, so
``downgrade`` names the same one ``upgrade`` created on both engines.

No index beyond the primary key. Every read is "this user's terms", which
the leading ``user_id`` column of the primary key already serves, and the
rows per user are bounded by ``MAX_MUTED_TERMS`` in the API schema.

Hand-corrected after autogenerate the same way the two revisions before
it were: autogenerate wraps the CHECK constraint in ``batch_alter_table``,
which is a SQLite workaround with nothing to work around on a table being
created in the same migration.

Revision ID: c3f8a17d2e40
Revises: b7c1e0a94f6d
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f8a17d2e40"
down_revision: str | Sequence[str] | None = "b7c1e0a94f6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_muted_terms",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "word",
                "tag",
                name="mute_kind",
                native_enum=False,
                length=16,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("term", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_muted_terms_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "kind", "term", name=op.f("pk_user_muted_terms")),
    )


def downgrade() -> None:
    op.drop_table("user_muted_terms")
