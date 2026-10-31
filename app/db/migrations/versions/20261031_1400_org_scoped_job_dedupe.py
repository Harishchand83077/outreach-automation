"""Job deduplication is scoped to the workspace (ADR-032).

Revision ID: a91c5d47f3b2
Revises: f2a4c81b9e07
Create Date: 2026-10-31

`uq_jobs_kind_dedupe_live` has been unique on `(kind, dedupe_key)` since migration 1, with no
`org_id`. So a live job in one workspace refused the same key in **every other workspace** — and
`queue.enqueue` reports a refusal by returning `None`, which every caller treats as "an identical
job is already queued, nothing to do". The second workspace's work simply did not happen, with no
error raised and nothing in the log.

It has been harmless so far only because every dedupe key in the tree is built from a UUID
(`draft:{lead.id}`, `lead:{lead.id}`), which cannot collide across orgs. It stops being harmless
the moment a key is built from an identifier two workspaces share — `feed:greenhouse:{token}`,
`company:{domain}`, `poll:{ats_slug}` — which is exactly what the job-post intake and signal
polling of phases 12 and 15 introduce. Hence this lands before any new job kind.

**Index only; no row is read, altered or moved.** The new index is created before the old one is
dropped, so there is no window in which two identical jobs could be enqueued.
"""

from __future__ import annotations

from alembic import op

revision = "a91c5d47f3b2"
down_revision = "f2a4c81b9e07"
branch_labels = None
depends_on = None

SCHEMA = "app"
TABLE = "jobs"
OLD = "uq_jobs_kind_dedupe_live"
NEW = "uq_jobs_org_kind_dedupe_live"
WHERE = "dedupe_key IS NOT NULL AND status IN ('queued', 'running')"


def upgrade() -> None:
    # Created first, dropped second: for the moment both exist the stricter old index still
    # holds, so the guarantee is never weaker than it was at any point during the migration.
    op.create_index(
        NEW,
        TABLE,
        ["org_id", "kind", "dedupe_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=WHERE,
    )
    op.drop_index(OLD, table_name=TABLE, schema=SCHEMA)


def downgrade() -> None:
    # The reverse needs care the forward direction does not. The old index is *stricter*, so if
    # two workspaces currently hold a live job with the same (kind, dedupe_key) — the very case
    # this migration exists to allow — recreating it would fail. Those rows are duplicates only
    # under the old rule, so the revert cancels the newer ones rather than deleting them: a
    # cancelled job is visible and re-enqueueable, where a deleted one is gone.
    op.execute(
        f"""
        UPDATE {SCHEMA}.{TABLE} AS j
           SET status = 'cancelled',
               last_error = coalesce(j.last_error, '')
                            || ' | cancelled by the ADR-032 downgrade: another workspace holds '
                            || 'this (kind, dedupe_key) and the old index is not org-scoped'
         WHERE j.dedupe_key IS NOT NULL
           AND j.status IN ('queued', 'running')
           AND EXISTS (
                 SELECT 1
                   FROM {SCHEMA}.{TABLE} AS keeper
                  WHERE keeper.dedupe_key = j.dedupe_key
                    AND keeper.kind = j.kind
                    AND keeper.dedupe_key IS NOT NULL
                    AND keeper.status IN ('queued', 'running')
                    AND (keeper.created_at, keeper.id) < (j.created_at, j.id)
               )
        """
    )
    op.create_index(
        OLD,
        TABLE,
        ["kind", "dedupe_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=WHERE,
    )
    op.drop_index(NEW, table_name=TABLE, schema=SCHEMA)
