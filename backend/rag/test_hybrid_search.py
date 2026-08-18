"""Smoke test for RAG Phase 4 (Hybrid search). Calibrated against the actual seed corpus: the
query below is a real, reproducible case where pure vector similarity misranks an exact
component-name match below a semantically-similar-but-wrong document, and hybrid fixes it —
found via manual exploration against the live corpus, same discipline as the RAG_MIN_SIMILARITY
threshold calibration in rag/test_retriever.py. Requires a live DB + LLM provider.
"""
from agents.llm.client import embed_text
from config import get_settings
from db.models import HistoricalTicket
from db.session import SessionLocal
from rag.retriever import search_similar_tickets

settings = get_settings()

_QUERY = "checkout-service issue"
_EXACT_MATCH = "Complete outage of checkout-service, all customers affected"
_SEMANTIC_DISTRACTOR = "order-service intermittent 502s under peak load"


def _top_two_by_pure_vector() -> list[str]:
    db = SessionLocal()
    try:
        vector_similarity = 1 - HistoricalTicket.embedding.cosine_distance(embed_text(_QUERY))
        rows = db.query(HistoricalTicket, vector_similarity.label("v")).order_by(vector_similarity.desc()).limit(2).all()
        return [row.summary for row, _ in rows]
    finally:
        db.close()


def main() -> None:
    original_enabled = settings.rag_hybrid_search_enabled
    original_weight = settings.rag_hybrid_vector_weight
    original_min_similarity = settings.rag_min_similarity
    try:
        print("--- Case 1: pure vector search misranks the exact keyword match ---")
        top_summaries = _top_two_by_pure_vector()
        assert top_summaries[0] == _SEMANTIC_DISTRACTOR, (
            f"expected pure vector search's #1 result to still be the distractor "
            f"({_SEMANTIC_DISTRACTOR!r}), got {top_summaries[0]!r} — corpus or embeddings may "
            f"have changed; recalibrate this test's query"
        )
        print(f"confirmed: pure vector ranks {top_summaries[0]!r} above the exact keyword match")
        print("test_pure_vector_misranks_exact_keyword_match: PASS")

        print("--- Case 2: hybrid search correctly promotes the exact keyword match to #1 ---")
        settings.rag_hybrid_search_enabled = True
        settings.rag_hybrid_vector_weight = original_weight
        top = search_similar_tickets(_QUERY, k=1)[0]["summary"]
        assert top == _EXACT_MATCH, f"expected hybrid's #1 result to be {_EXACT_MATCH!r}, got {top!r}"
        print(f"confirmed: hybrid ranks {top!r} first")
        print("test_hybrid_promotes_exact_keyword_match: PASS")

        print("--- Case 3: with hybrid disabled, ranking reverts to pure vector (the toggle works) ---")
        settings.rag_hybrid_search_enabled = False
        top = search_similar_tickets(_QUERY, k=1)[0]["summary"]
        assert top == _SEMANTIC_DISTRACTOR, f"expected the toggle-off result to match Case 1, got {top!r}"
        print("test_hybrid_toggle_reverts_to_pure_vector: PASS")

        print("--- Case 4: RAG_MIN_SIMILARITY still filters on raw vector similarity, not the blended score ---")
        settings.rag_hybrid_search_enabled = True
        settings.rag_min_similarity = 0.99  # nothing in this tiny corpus scores that high
        results = search_similar_tickets(_QUERY, k=5)
        assert results == [], "an unreasonably high vector-similarity floor should still drop every result"
        print("test_relevance_floor_still_applies_under_hybrid: PASS")
    finally:
        settings.rag_hybrid_search_enabled = original_enabled
        settings.rag_hybrid_vector_weight = original_weight
        settings.rag_min_similarity = original_min_similarity


if __name__ == "__main__":
    main()
