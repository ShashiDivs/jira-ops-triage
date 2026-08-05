"""Promotes a resolved ticket into the RAG corpus (RAG Phase 3 — Corpus growth). Deliberately a
separate, explicit action rather than something that happens automatically on approval — see
reference/agentic-teaching-notes.md section 7: unreviewed corpus growth degrades RAG quality,
so growing the corpus should be visible and intentional, not a side effect of approving a ticket.
"""
from agents.llm import current_embedding_model, embed_text
from config import get_settings
from db.models import HistoricalTicket
from db.session import SessionLocal
from mcp_server import jira_store
from rag.retriever import find_duplicate_ticket

settings = get_settings()


class PromotionError(ValueError):
    """Ticket isn't eligible, has already been promoted, or is a near-duplicate of an
    existing corpus entry."""


def promote_ticket_to_rag(ticket_id: str) -> dict:
    ticket = jira_store.get_ticket(ticket_id)
    if ticket is None:
        raise PromotionError(f"ticket {ticket_id} not found")

    # Only tickets a human actually reviewed and approved are eligible — not auto_approved
    # (never reviewed by anyone), not rejected (this classification was wrong), not
    # pending/blocked (not resolved at all). See the design note in the teaching notes for why
    # this is deliberately the conservative option.
    if ticket["status"] != "approved":
        raise PromotionError(
            f"only approved tickets can be promoted to the RAG corpus (status is '{ticket['status']}')"
        )

    db = SessionLocal()
    try:
        existing = db.query(HistoricalTicket).filter(HistoricalTicket.source_ticket_id == ticket_id).first()
        if existing is not None:
            raise PromotionError(f"ticket {ticket_id} was already promoted (historical_tickets id={existing.id})")

        # Live tickets don't have a dedicated "resolution" field the way the seed corpus does —
        # the closest equivalent is the audit comment, which already summarizes the triage/
        # routing/HITL decision (and the human's approval notes, if they left any).
        resolution = (
            ticket["comments"][-1]["body"]
            if ticket["comments"]
            else f"Classified as {ticket['severity']} and routed to {ticket['owning_team']}."
        )

        text = f"{ticket['summary']}\n{ticket['description']}\nResolution: {resolution}"
        embedding = embed_text(text)

        # Semantic duplicate guard (RAG Phase 3.5): the source_ticket_id check above only
        # stops re-promoting the *same* ticket. This stops two *different* tickets describing
        # the same recurring incident from both landing as near-identical vectors — reuses the
        # embedding just computed above rather than re-embedding the same text.
        near_dup = find_duplicate_ticket(embedding, settings.rag_duplicate_similarity_threshold)
        if near_dup is not None:
            raise PromotionError(
                f"a near-duplicate already exists in the corpus (historical_tickets id={near_dup['id']}, "
                f"\"{near_dup['summary']}\", similarity={near_dup['similarity']:.2f}) — not promoting"
            )

        row = HistoricalTicket(
            summary=ticket["summary"],
            description=ticket["description"],
            component=ticket.get("component") or "",
            severity=ticket.get("severity") or "",
            resolution=resolution,
            embedding=embedding,
            embedding_model=current_embedding_model(),
            source_ticket_id=ticket_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "source_ticket_id": ticket_id,
            "summary": row.summary,
            "resolution": row.resolution,
        }
    finally:
        db.close()
