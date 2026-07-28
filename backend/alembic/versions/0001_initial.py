"""initial schema: pgvector extension, tickets, comments, historical_tickets, runbooks

Revision ID: 0001
Revises:
Create Date: 2026-07-27

"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1536  # must match AZURE_EMBEDDING_DIM in .env


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "tickets",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("component", sa.String, nullable=False, server_default=""),
        sa.Column("reporter", sa.String, nullable=False, server_default=""),
        sa.Column("severity", sa.String, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("owning_team", sa.String, nullable=True),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("reasoning", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String, nullable=True),
    )

    op.create_table(
        "comments",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("ticket_id", sa.String, sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("author", sa.String, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "historical_tickets",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("component", sa.String, nullable=False, server_default=""),
        sa.Column("severity", sa.String, nullable=False, server_default=""),
        sa.Column("resolution", sa.Text, nullable=False, server_default=""),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    )
    op.execute(
        "CREATE INDEX historical_tickets_embedding_idx ON historical_tickets "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "runbooks",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("doc_type", sa.String, nullable=False),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    )
    op.execute(
        "CREATE INDEX runbooks_embedding_idx ON runbooks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_table("runbooks")
    op.drop_table("historical_tickets")
    op.drop_table("comments")
    op.drop_table("tickets")
    op.execute("DROP EXTENSION IF EXISTS vector")
