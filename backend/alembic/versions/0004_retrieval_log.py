"""retrieval_log table for RAG retrieval observability (RAG Phase 2)

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-03

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retrieval_log",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("ticket_id", sa.String, nullable=False),
        sa.Column("node_name", sa.String, nullable=False, server_default=""),
        sa.Column("tool_name", sa.String, nullable=False),
        sa.Column("query_text", sa.Text, nullable=False),
        sa.Column("result_count", sa.Integer, nullable=False),
        sa.Column("top_similarity", sa.Float, nullable=True),
        sa.Column("avg_similarity", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_retrieval_log_ticket_id", "retrieval_log", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_retrieval_log_ticket_id", table_name="retrieval_log")
    op.drop_table("retrieval_log")
