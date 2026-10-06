"""model + prompt_version on agent_runs (Governance — Auditability / prompt versioning)

Lets any classification be traced to the exact prompt wording and model that produced it.
Nullable: nodes that never call an LLM (intake, guardrail, hitl_gate, action) leave both NULL,
and rows written before this migration stay NULL.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("model", sa.String, nullable=True))
    op.add_column("agent_runs", sa.Column("prompt_version", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "prompt_version")
    op.drop_column("agent_runs", "model")
