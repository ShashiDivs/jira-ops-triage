"""source_ticket_id on historical_tickets for corpus-growth provenance/dedup (RAG Phase 3)

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-03

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("historical_tickets", sa.Column("source_ticket_id", sa.String, nullable=True))
    op.create_index("ix_historical_tickets_source_ticket_id", "historical_tickets", ["source_ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_historical_tickets_source_ticket_id", table_name="historical_tickets")
    op.drop_column("historical_tickets", "source_ticket_id")
