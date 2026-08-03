"""pgvector-backed retrieval, replacing the reference project's TF-IDF retriever.
Same function signatures the MCP server exposes as tools, so callers don't change.
"""
from agents.llm import current_embedding_model, embed_text
from config import get_settings
from db.models import HistoricalTicket, RetrievalLog, Runbook
from db.session import SessionLocal

settings = get_settings()


def _log_retrieval(ticket_id: str | None, node_name: str | None, tool_name: str, query: str, results: list[dict]) -> None:
    # Called from every retrieval, including ones with no ticket context (seed.py, the eval
    # harness) — skip logging in those cases rather than writing a meaningless row.
    if not ticket_id:
        return
    scores = [r["similarity"] for r in results]
    db = SessionLocal()
    try:
        db.add(
            RetrievalLog(
                ticket_id=ticket_id,
                node_name=node_name or "",
                tool_name=tool_name,
                query_text=query,
                result_count=len(results),
                top_similarity=max(scores) if scores else None,
                avg_similarity=(sum(scores) / len(scores)) if scores else None,
            )
        )
        db.commit()
    finally:
        db.close()


def search_similar_tickets(
    query: str, k: int = 3, ticket_id: str | None = None, node_name: str | None = None
) -> list[dict]:
    query_embedding = embed_text(query)
    db = SessionLocal()
    try:
        distance = HistoricalTicket.embedding.cosine_distance(query_embedding)
        rows = (
            db.query(HistoricalTicket, distance.label("distance"))
            .order_by(distance)
            .limit(k)
            .all()
        )
        results = []
        for row, dist in rows:
            similarity = 1 - dist
            if similarity < settings.rag_min_similarity:
                continue
            results.append(
                {
                    "id": row.id,
                    "summary": row.summary,
                    "description": row.description,
                    "component": row.component,
                    "severity": row.severity,
                    "resolution": row.resolution,
                    "similarity": round(similarity, 4),
                }
            )
    finally:
        db.close()
    _log_retrieval(ticket_id, node_name, "search_similar_tickets", query, results)
    return results


def search_runbooks(
    query: str,
    k: int = 3,
    doc_type: str | None = None,
    ticket_id: str | None = None,
    node_name: str | None = None,
) -> list[dict]:
    query_embedding = embed_text(query)
    db = SessionLocal()
    try:
        distance = Runbook.embedding.cosine_distance(query_embedding)
        q = db.query(Runbook, distance.label("distance"))
        if doc_type:
            q = q.filter(Runbook.doc_type == doc_type)
        rows = q.order_by(distance).limit(k).all()
        results = []
        for row, dist in rows:
            similarity = 1 - dist
            if similarity < settings.rag_min_similarity:
                continue
            results.append(
                {
                    "id": row.id,
                    "doc_type": row.doc_type,
                    "title": row.title,
                    "content": row.content,
                    "similarity": round(similarity, 4),
                }
            )
    finally:
        db.close()
    _log_retrieval(ticket_id, node_name, "search_runbooks", query, results)
    return results


def check_corpus_embedding_consistency() -> list[str]:
    """Warns if the corpus contains embeddings from a different model than the one currently
    configured — same dimension doesn't mean same vector space, and pgvector can't detect
    that mismatch on its own. Call at startup; never fatal, since a fresh/unseeded corpus
    legitimately has no embedding_model recorded yet."""
    active_model = current_embedding_model()
    db = SessionLocal()
    try:
        warnings = []
        for model_cls, label in [(HistoricalTicket, "historical_tickets"), (Runbook, "runbooks")]:
            models_in_use = {
                row[0] for row in db.query(model_cls.embedding_model).distinct().all() if row[0]
            }
            if models_in_use and models_in_use != {active_model}:
                warnings.append(
                    f"{label}: corpus contains embeddings from {sorted(models_in_use)}, but the "
                    f"configured embedding model is '{active_model}' — similarity scores will be "
                    f"meaningless until this corpus is re-seeded/re-embedded."
                )
        return warnings
    finally:
        db.close()
