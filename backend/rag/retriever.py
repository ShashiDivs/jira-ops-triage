"""pgvector-backed retrieval, replacing the reference project's TF-IDF retriever.
Same function signatures the MCP server exposes as tools, so callers don't change.
"""
from sqlalchemy import Text, cast, func

from agents.llm import current_embedding_model, embed_text
from config import get_settings
from db.models import HistoricalTicket, RetrievalLog, Runbook
from db.session import SessionLocal

settings = get_settings()


def _fts_rank(query_text: str, *columns):
    """Postgres full-text rank (RAG Phase 4 — Hybrid search). The `||` concatenation below is
    written to match alembic/versions/0006_fts_indexes.py's expression GIN index exactly, so
    Postgres can use it. `ts_rank_cd(..., 32)` normalizes the rank to [0, 1) — rank/(rank+1) —
    the same scale cosine similarity is already on, so the two can be linearly blended below
    without a separate normalization step.

    plainto_tsquery ANDs every lexeme together, which is far too strict for the long,
    full-sentence queries this pipeline actually sends (ticket summary + description, often
    15-30 words) — almost no document contains *every* word of a long query, so the rank would
    be ~0 nearly everywhere. Flipping the AND to OR (a standard, documented trick: cast the
    tsquery to text and swap ' & ' for ' | ') means a document matching *some* significant
    keywords still gets a proportional rank, while ts_rank_cd still weights matching more of
    them / matching them more densely higher than matching just one."""
    doc = func.coalesce(columns[0], "")
    for c in columns[1:]:
        doc = doc + " " + func.coalesce(c, "")
    document = func.to_tsvector("english", doc)
    and_query_text = cast(func.plainto_tsquery("english", query_text), Text)
    or_query = func.to_tsquery("english", func.replace(and_query_text, " & ", " | "))
    return func.ts_rank_cd(document, or_query, 32)


def _hybrid_score(vector_similarity, text_rank):
    if not settings.rag_hybrid_search_enabled:
        return vector_similarity
    weight = settings.rag_hybrid_vector_weight
    return (vector_similarity * weight) + (text_rank * (1 - weight))


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
        vector_similarity = 1 - HistoricalTicket.embedding.cosine_distance(query_embedding)
        text_rank = _fts_rank(query, HistoricalTicket.summary, HistoricalTicket.description, HistoricalTicket.resolution)
        score = _hybrid_score(vector_similarity, text_rank)
        rows = (
            db.query(HistoricalTicket, vector_similarity.label("vsim"), text_rank.label("trank"))
            .order_by(score.desc())
            .limit(k)
            .all()
        )
        results = []
        for row, vsim, trank in rows:
            # The relevance floor stays on raw vector similarity, not the blended score — a
            # keyword-only match with near-zero semantic similarity still isn't a good enough
            # match to hand an LLM as grounding. Hybrid changes *ranking*, not this floor.
            if vsim < settings.rag_min_similarity:
                continue
            results.append(
                {
                    "id": row.id,
                    "summary": row.summary,
                    "description": row.description,
                    "component": row.component,
                    "severity": row.severity,
                    "resolution": row.resolution,
                    "similarity": round(vsim, 4),
                    "text_rank": round(trank, 4),
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
        vector_similarity = 1 - Runbook.embedding.cosine_distance(query_embedding)
        text_rank = _fts_rank(query, Runbook.title, Runbook.content)
        score = _hybrid_score(vector_similarity, text_rank)
        q = db.query(Runbook, vector_similarity.label("vsim"), text_rank.label("trank"))
        if doc_type:
            q = q.filter(Runbook.doc_type == doc_type)
        rows = q.order_by(score.desc()).limit(k).all()
        results = []
        for row, vsim, trank in rows:
            if vsim < settings.rag_min_similarity:
                continue
            results.append(
                {
                    "id": row.id,
                    "doc_type": row.doc_type,
                    "title": row.title,
                    "content": row.content,
                    "similarity": round(vsim, 4),
                    "text_rank": round(trank, 4),
                }
            )
    finally:
        db.close()
    _log_retrieval(ticket_id, node_name, "search_runbooks", query, results)
    return results


def find_duplicate_ticket(embedding: list[float], threshold: float) -> dict | None:
    """Given an already-computed embedding, return the single most similar existing
    historical ticket if its similarity is at or above `threshold`, else None. Used by
    rag/promotion.py's semantic-duplicate guard (RAG Phase 3.5) — takes a precomputed
    embedding rather than a query string so it doesn't re-embed text that was just embedded
    for insertion (no extra API call). Not logged to retrieval_log: this is a write-path
    integrity check, not a retrieval serving a triage/routing decision."""
    db = SessionLocal()
    try:
        distance = HistoricalTicket.embedding.cosine_distance(embedding)
        top = db.query(HistoricalTicket, distance.label("distance")).order_by(distance).limit(1).first()
        if top is None:
            return None
        row, dist = top
        similarity = 1 - dist
        if similarity < threshold:
            return None
        return {"id": row.id, "summary": row.summary, "similarity": round(similarity, 4)}
    finally:
        db.close()


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
