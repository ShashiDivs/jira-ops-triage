"""Full-text search GIN indexes on historical_tickets and runbooks (RAG Phase 4 — Hybrid
search). Expression indexes, not new columns — retriever.py recomputes the identical
to_tsvector(...) expression at query time, so Postgres can use these without any stored/
generated column to keep in sync on writes.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-07

"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_HISTORICAL_TICKETS_FTS_EXPR = (
    "to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(description, '') "
    "|| ' ' || coalesce(resolution, ''))"
)
_RUNBOOKS_FTS_EXPR = "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX historical_tickets_fts_idx ON historical_tickets "
        f"USING GIN ({_HISTORICAL_TICKETS_FTS_EXPR})"
    )
    op.execute(f"CREATE INDEX runbooks_fts_idx ON runbooks USING GIN ({_RUNBOOKS_FTS_EXPR})")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS historical_tickets_fts_idx")
    op.execute("DROP INDEX IF EXISTS runbooks_fts_idx")
