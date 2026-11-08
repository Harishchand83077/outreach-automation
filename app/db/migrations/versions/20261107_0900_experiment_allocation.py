"""Experiment allocation: a lead's arm, one active experiment, and a floor under exploration.

Revision ID: 9b3e5c81d7a4
Revises: 6a2d47c9b8e1
Create Date: 2026-11-07

**`experiments` has existed since ADR-026 and has never had a producer**, so it has no rows and
can be reshaped rather than migrated. That is also the reason the slice is cheap now and would
not have been later: the arms column changes shape, and a table with real rows in it would need
a backfill that guessed what an arm's weight used to be.

Three changes, and the second is the one that matters:

1. `leads.variant` -- the lead's arm, stored once. ADR-026 specified stickiness as a
   deterministic hash of (lead, experiment), and building it showed that cannot work: a hash is
   taken modulo the number of arms, so adding a third arm moves most leads already in flight
   from one arm to the other, silently, after their first email has gone out. A stored column
   cannot change under rows that already exist.
2. `uq_experiments_one_active` -- at most one active experiment per campaign. Two would both
   allocate and the second would overwrite `leads.variant`, so every message would carry
   whichever label ran last. That is the quietest available way to make a result meaningless, and
   it belongs in the database rather than in a route's memory.
3. The exploration share gains a **floor** of 0.1, replacing a constraint that allowed zero. A
   weighted allocator with nothing held back can drive an arm to zero, and the arm that gets
   starved is the one whose result would have surprised somebody.

Additive for every existing row: `leads.variant` is NULL for every lead in the live database,
which is a lead with no experiment, which is every lead today. Nothing in the send path treats
NULL as ineligible -- the same care ADR-044 took with `leads.mailbox_id`, where treating NULL as
"not yet eligible" would have stopped an in-flight campaign at deploy.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9b3e5c81d7a4"
down_revision = "6a2d47c9b8e1"
branch_labels = None
depends_on = None

SCHEMA = "app"
#: Mirrors `MIN_EXPLORATION_SHARE` in `models.py`. Written out here because a migration must
#: keep working against the code of its own day; the model holds the authority for new rows.
MIN_EXPLORATION_SHARE = 0.1


def upgrade() -> None:
    op.add_column(
        "leads", sa.Column("variant", sa.String(length=40), nullable=True), schema=SCHEMA
    )
    op.add_column(
        "experiments",
        # Needed to read a funnel: messages sent after an experiment stopped carry no arm, and
        # without the date that looks like an allocator that quietly broke.
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )

    # `experiments.arms` is already JSONB and only its *contents* change shape, from
    # `["a","b"]` to a list of objects. No statement is needed and none is written: the table
    # has never had a producer, so there is nothing to convert.

    # At most one active experiment per campaign.
    op.create_index(
        "uq_experiments_one_active",
        "experiments",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text("active"),
        schema=SCHEMA,
    )
    op.create_index("ix_experiments_org", "experiments", ["org_id"], schema=SCHEMA)

    # The floor. Dropped and recreated rather than altered, because Postgres has no ALTER for a
    # check constraint's expression.
    op.drop_constraint(
        "ck_experiments_exploration_share", "experiments", type_="check", schema=SCHEMA
    )
    op.create_check_constraint(
        "ck_experiments_exploration_floor",
        "experiments",
        f"exploration_share >= {MIN_EXPLORATION_SHARE} AND exploration_share <= 1",
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Reversible, and it drops the arms with the column.

    `leads.variant` is the only record of which arm a lead was in -- the messages keep their
    label, so a funnel built from `messages.variant` survives this, which is the one that
    matters. A lead that comes back through drafting after a downgrade and re-upgrade would be
    allocated afresh, and that is said here rather than discovered: it is why the downgrade is
    for a bad deploy and not for turning the feature off. Stopping an experiment is
    `active = false`, which keeps every assignment.
    """
    op.drop_constraint(
        "ck_experiments_exploration_floor", "experiments", type_="check", schema=SCHEMA
    )
    op.create_check_constraint(
        "ck_experiments_exploration_share",
        "experiments",
        "exploration_share >= 0 AND exploration_share <= 1",
        schema=SCHEMA,
    )
    op.drop_index("ix_experiments_org", table_name="experiments", schema=SCHEMA)
    op.drop_index("uq_experiments_one_active", table_name="experiments", schema=SCHEMA)
    op.drop_column("experiments", "stopped_at", schema=SCHEMA)
    op.drop_column("leads", "variant", schema=SCHEMA)
