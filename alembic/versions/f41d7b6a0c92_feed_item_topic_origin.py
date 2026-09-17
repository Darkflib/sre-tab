"""Who asserted a topic link: the source, or a URL rule.

``feed_item_topics.origin`` exists so that topics derived from an item's
own URL can be corrected. Before this, every link came from the item's
source and ``upsert_items`` was insert-or-ignore throughout, so nothing
in the system had a way to *remove* a topic link at all. That was fine
while the only writer was a source's configured topic list, which changes
about never. A ruleset over URL paths is a thing an operator iterates on,
and a ruleset that cannot take back what its previous version asserted is
a one-way door.

With the discriminator, a re-tag pass deletes and reinserts exactly the
rows it owns and cannot touch the operator's.

**The backfill is the server default, and it is correct rather than
convenient.** Every row that exists when this runs was written by
``upsert_items`` from a source's topic list, so ``'source'`` is not a
guess about history — it is what every one of those rows is. That is also
why the column is ``NOT NULL`` from the start: there is no third state
meaning "written before origins existed", because there is nothing such a
state would be used to decide.

Two things about the shape of it, and a third at ``CHECK_KEY`` below.
Each of them was a bug first.

**``batch_alter_table`` with an explicit ``copy_from``, not reflection.**
``create_constraint`` is set on the model's ``Enum`` (see the class
docstring in ``app.db.models``) and SQLite cannot add a CHECK to an
existing table through ``ALTER``, so batch mode has to recreate it.
Left to reflect the table itself, the downgrade rebuilds it carrying the
CHECK it is in the middle of removing and fails on a column that no
longer exists. Naming both shapes here is what makes the round trip work.

**A plain ``String`` plus a named CHECK, not ``sa.Enum``.** They render
identically on both engines — ``native_enum=False`` *is* a VARCHAR and a
CHECK — but an ``Enum`` constructed here carries no naming convention, so
it names its constraint ``topic_origin`` where the model's metadata names
the same constraint ``ck_feed_item_topics_topic_origin``. A migration
that builds a differently-named constraint from the one the models
declare is a schema that only looks like the one the tests check.

Verified up and down on SQLite and on PostgreSQL 18, against an empty and
a populated table.

Revision ID: f41d7b6a0c92
Revises: e2a9c4b06d13
Create Date: 2026-09-17
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "f41d7b6a0c92"
down_revision: str | Sequence[str] | None = "e2a9c4b06d13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The two halves of one name, and they are not interchangeable.
#:
#: ``CHECK_KEY`` is what the batch operations are given, because
#: ``env.py`` hands Alembic ``Base.metadata`` and its
#: ``NAMING_CONVENTION`` — so ``create_check_constraint`` and
#: ``drop_constraint`` render ``ck_%(table_name)s_%(constraint_name)s``
#: around whatever they are passed. Give them the finished name and it
#: is prefixed a second time, into
#: ``ck_feed_item_topics_ck_feed_item_topics_topic_origin``, which the
#: downgrade then cannot find.
#:
#: ``CHECK_NAME`` is that same rendering, spelled out, because the
#: ``sa.MetaData()`` in :func:`_table` is a bare one with no convention
#: attached and takes the name literally. The model declares the same
#: constraint through ``Enum(name="topic_origin", create_constraint=True)``.
CHECK_KEY = "topic_origin"
CHECK_NAME = f"ck_feed_item_topics_{CHECK_KEY}"
CHECK_CONDITION = "origin IN ('source', 'rule')"


def _table(*, with_origin: bool) -> sa.Table:
    """``feed_item_topics`` as it is on either side of this revision.

    Batch mode needs the *starting* shape to rebuild from, and reflection
    cannot supply it on the way down (see the module docstring).
    """
    columns: list[sa.Column[Any]] = [
        sa.Column("feed_item_id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
    ]
    constraints: list[sa.schema.SchemaItem] = [
        sa.ForeignKeyConstraint(
            ["feed_item_id"],
            ["feed_items.id"],
            name="fk_feed_item_topics_feed_item_id_feed_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name="fk_feed_item_topics_topic_id_topics",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("feed_item_id", "topic_id", name="pk_feed_item_topics"),
    ]
    if with_origin:
        columns.append(
            sa.Column("origin", sa.String(length=16), nullable=False, server_default="source")
        )
        constraints.append(sa.CheckConstraint(CHECK_CONDITION, name=CHECK_NAME))
    return sa.Table("feed_item_topics", sa.MetaData(), *columns, *constraints)


def upgrade() -> None:
    with op.batch_alter_table("feed_item_topics", copy_from=_table(with_origin=False)) as batch:
        batch.add_column(
            sa.Column("origin", sa.String(length=16), nullable=False, server_default="source")
        )
        batch.create_check_constraint(CHECK_KEY, CHECK_CONDITION)


def downgrade() -> None:
    with op.batch_alter_table("feed_item_topics", copy_from=_table(with_origin=True)) as batch:
        batch.drop_constraint(CHECK_KEY, type_="check")
        batch.drop_column("origin")
