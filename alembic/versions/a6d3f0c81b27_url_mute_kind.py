"""A third kind of mute: by URL.

``user_muted_terms.kind`` gains ``'url'`` beside ``'word'`` and ``'tag'``.
Nothing else about the table changes — a URL term is reduced to a host
and at most one path segment before it is stored, which is what lets it
fit the sixty-four characters a phrase was given — so this revision is
the CHECK constraint and nothing more. The model's ``Enum`` carries
``create_constraint=True`` (see ``UserMutedTerm``), and without widening
the constraint every URL mute would be an ``IntegrityError``.

Built the way ``f41d7b6a0c92`` is, for the reasons it records, and both
were bugs there first:

**``batch_alter_table`` with an explicit ``copy_from``.** SQLite cannot
alter a CHECK in place, so batch mode rebuilds the table, and it has to
be told the shape it is rebuilding *from* rather than left to reflect it.

**The bare constraint key to the batch operations, the full name to the
table.** ``env.py`` hands Alembic the models' ``NAMING_CONVENTION``, so
``drop_constraint`` and ``create_check_constraint`` render
``ck_user_muted_terms_<key>`` themselves; the ``sa.MetaData()`` in
:func:`_table` is a bare one and takes its name literally. The name both
arrive at is the one ``c3f8a17d2e40`` created, which was read back from
SQLite and PostgreSQL 18 before this was written rather than assumed.

**The downgrade deletes every URL mute, and that is the only honest
option.** The older application cannot materialise a row whose kind it
does not know — ``LookupError`` on the next feed request, which is the
failure the CHECK exists to prevent — and the narrower constraint would
refuse to be created over those rows anyway. A reader who downgrades
loses their URL mutes and keeps every word and tag.

Verified up and down on SQLite and on PostgreSQL 18, against an empty and
a populated table.

Revision ID: a6d3f0c81b27
Revises: f41d7b6a0c92
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6d3f0c81b27"
down_revision: str | Sequence[str] | None = "f41d7b6a0c92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: See the module docstring: the key for the batch operations, which apply
#: the naming convention, and the name for :func:`_table`, which does not.
CHECK_KEY = "mute_kind"
CHECK_NAME = f"ck_user_muted_terms_{CHECK_KEY}"
NARROW = "kind IN ('word', 'tag')"
WIDE = "kind IN ('word', 'tag', 'url')"


def _table(condition: str) -> sa.Table:
    """``user_muted_terms`` as ``c3f8a17d2e40`` created it, with *condition*
    as its CHECK — which is the only thing that differs either side of
    this revision."""
    return sa.Table(
        "user_muted_terms",
        sa.MetaData(),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("term", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_muted_terms_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "kind", "term", name="pk_user_muted_terms"),
        sa.CheckConstraint(condition, name=CHECK_NAME),
    )


def upgrade() -> None:
    with op.batch_alter_table("user_muted_terms", copy_from=_table(NARROW)) as batch:
        batch.drop_constraint(CHECK_KEY, type_="check")
        batch.create_check_constraint(CHECK_KEY, WIDE)


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM user_muted_terms WHERE kind = 'url'"))
    with op.batch_alter_table("user_muted_terms", copy_from=_table(WIDE)) as batch:
        batch.drop_constraint(CHECK_KEY, type_="check")
        batch.create_check_constraint(CHECK_KEY, NARROW)
