"""An item's detected language, and the languages a reader reads.

``feed_items.language`` is nullable and stays ``NULL`` for every row that
exists when this runs. That is correct rather than incomplete: ``NULL``
means "no confident answer", the feed never hides such an item, so an
upgrade changes nothing any reader sees until ``sre-tab detect-languages``
has been run over the retained window — and even then, only for readers
who have chosen languages.

``user_preference_languages`` has the shape ``user_preference_topics``
has, a composite primary key over the user and the value, so a client
that retries a save cannot create duplicates. No CHECK on the code: the
valid set is the model's label set, which belongs to a dependency and is
validated in ``app.services.preferences``, not frozen into the schema.

The downgrade drops both. Nothing older reads either, and a detection is
re-derivable from the item's own text by running the command again.

Revision ID: b9e4d2c7a310
Revises: a6d3f0c81b27
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b9e4d2c7a310"
down_revision: str | Sequence[str] | None = "a6d3f0c81b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feed_items", sa.Column("language", sa.String(length=8), nullable=True))
    op.create_table(
        "user_preference_languages",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_preference_languages_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "language", name=op.f("pk_user_preference_languages")),
    )


def downgrade() -> None:
    op.drop_table("user_preference_languages")
    op.drop_column("feed_items", "language")
