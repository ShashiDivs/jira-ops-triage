"""agent_runs table for per-node observability (Phase 6)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-29

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("ticket_id", sa.String, nullable=False),
        sa.Column("node_name", sa.String, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("duration_ms", sa.Float, nullable=False),
        sa.Column("tokens_in", sa.Integer, nullable=True),
        sa.Column("tokens_out", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_agent_runs_ticket_id", "agent_runs", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_ticket_id", table_name="agent_runs")
    op.drop_table("agent_runs")
