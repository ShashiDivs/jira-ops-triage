"""pgvector-backed retrieval, replacing the reference project's TF-IDF retriever.
Same function signatures the MCP server exposes as tools, so callers don't change.
"""
from agents.llm import embed_text
from db.models import HistoricalTicket, Runbook
from db.session import SessionLocal


def search_similar_tickets(query: str, k: int = 3) -> list[dict]:
    query_embedding = embed_text(query)
    db = SessionLocal()
    try:
        rows = (
            db.query(HistoricalTicket)
            .order_by(HistoricalTicket.embedding.cosine_distance(query_embedding))
            .limit(k)
            .all()
        )
        return [
            {
                "id": row.id,
                "summary": row.summary,
                "description": row.description,
                "component": row.component,
                "severity": row.severity,
                "resolution": row.resolution,
            }
            for row in rows
        ]
    finally:
        db.close()


def search_runbooks(query: str, k: int = 3, doc_type: str | None = None) -> list[dict]:
    query_embedding = embed_text(query)
    db = SessionLocal()
    try:
        q = db.query(Runbook)
        if doc_type:
            q = q.filter(Runbook.doc_type == doc_type)
        rows = q.order_by(Runbook.embedding.cosine_distance(query_embedding)).limit(k).all()
        return [
            {"id": row.id, "doc_type": row.doc_type, "title": row.title, "content": row.content}
            for row in rows
        ]
    finally:
        db.close()
