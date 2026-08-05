import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config import get_settings
from db.session import Base

EMBEDDING_DIM = get_settings().azure_embedding_dim


def _uuid() -> str:
    return str(uuid.uuid4())


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    summary: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    component: Mapped[str] = mapped_column(String, default="")
    reporter: Mapped[str] = mapped_column(String, default="")

    severity: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    owning_team: Mapped[str | None] = mapped_column(String, nullable=True)

    # pending: waiting at HITL gate | auto_approved | approved | rejected
    status: Mapped[str] = mapped_column(String, default="pending")

    reasoning: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)

    comments: Mapped[list["Comment"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"))
    author: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped["Ticket"] = relationship(back_populates="comments")


class HistoricalTicket(Base):
    __tablename__ = "historical_tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    summary: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    component: Mapped[str] = mapped_column(String, default="")
    severity: Mapped[str] = mapped_column(String, default="")
    resolution: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    # Which embedding model produced `embedding` — lets us detect a corpus that's silently
    # become a mix of incompatible vector spaces after an embedding-model change.
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    # NULL for the original curated seed corpus; set to the originating ticket's id once a
    # resolved ticket is promoted into the corpus (RAG Phase 3) — doubles as both provenance
    # (seed vs. organically grown) and the duplicate-promotion check.
    source_ticket_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


class Runbook(Base):
    __tablename__ = "runbooks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    doc_type: Mapped[str] = mapped_column(String)  # severity_policy | security_runbook | outage_runbook | ownership_map | hitl_policy
    title: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)


class AgentRun(Base):
    """One row per graph-node execution — the Observability hardening pattern. ticket_id
    doubles as the trace id; node_name + started_at + duration_ms give a per-node timeline."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(String, index=True)
    node_name: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    duration_ms: Mapped[float] = mapped_column(Float)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RetrievalLog(Base):
    """One row per RAG search call (RAG Phase 2 — Retrieval Observability). Answers "which
    past tickets/runbooks actually grounded this classification" by ticket_id, without having
    to dig through application logs."""

    __tablename__ = "retrieval_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(String, index=True)
    node_name: Mapped[str] = mapped_column(String, default="")
    tool_name: Mapped[str] = mapped_column(String)  # search_similar_tickets | search_runbooks
    query_text: Mapped[str] = mapped_column(Text)
    result_count: Mapped[int] = mapped_column(Integer)
    top_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
