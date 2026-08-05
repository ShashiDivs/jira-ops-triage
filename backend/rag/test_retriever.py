"""Smoke test for RAG Phase 1: similarity scores are returned, a high threshold filters out
weak matches, and the embedding-model consistency check is quiet on a freshly-seeded corpus.
Requires a live DB + LLM provider (real embeddings, same as scripts/seed.py).
"""
from config import get_settings
from rag.retriever import check_corpus_embedding_consistency, search_runbooks, search_similar_tickets

settings = get_settings()


def test_similarity_scores_present_and_ordered() -> None:
    results = search_similar_tickets("Complete outage, all customers affected, order-service down", k=3)
    assert len(results) > 0, "expected at least one result"
    for r in results:
        assert "similarity" in r and 0.0 <= r["similarity"] <= 1.0
    scores = [r["similarity"] for r in results]
    assert scores == sorted(scores, reverse=True), "results should be ordered by descending similarity"
    print(f"test_similarity_scores_present_and_ordered: PASS (top score={scores[0]})")


def test_high_threshold_filters_unrelated_query() -> None:
    original = settings.rag_min_similarity  # restore the real configured value, not a guess
    settings.rag_min_similarity = 0.9  # only near-exact matches should pass
    try:
        results = search_similar_tickets("What is the capital of France and how many bakeries does it have?", k=5)
        assert len(results) == 0, f"expected an unrelated query to be filtered out at 0.9, got {results}"
    finally:
        settings.rag_min_similarity = original
    print("test_high_threshold_filters_unrelated_query: PASS")


def test_runbooks_search_also_scores_and_respects_doc_type() -> None:
    results = search_runbooks("severity classification policy", k=1, doc_type="severity_policy")
    assert len(results) == 1
    assert results[0]["doc_type"] == "severity_policy"
    assert "similarity" in results[0]
    print(f"test_runbooks_search_also_scores_and_respects_doc_type: PASS (score={results[0]['similarity']})")


def test_consistency_check_quiet_after_fresh_seed() -> None:
    warnings = check_corpus_embedding_consistency()
    assert warnings == [], f"expected no warnings right after a fresh seed, got: {warnings}"
    print("test_consistency_check_quiet_after_fresh_seed: PASS")


def test_consistency_check_flags_a_mismatched_model() -> None:
    from db.models import HistoricalTicket
    from db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(HistoricalTicket).first()
        original_model = row.embedding_model
        row.embedding_model = "some-other-embedding-model"
        db.commit()
    finally:
        db.close()

    warnings = check_corpus_embedding_consistency()
    assert any("historical_tickets" in w for w in warnings), f"expected a mismatch warning, got: {warnings}"

    # restore
    db = SessionLocal()
    try:
        db.query(HistoricalTicket).filter(HistoricalTicket.embedding_model == "some-other-embedding-model").update(
            {"embedding_model": original_model}
        )
        db.commit()
    finally:
        db.close()
    print("test_consistency_check_flags_a_mismatched_model: PASS")


if __name__ == "__main__":
    test_similarity_scores_present_and_ordered()
    test_high_threshold_filters_unrelated_query()
    test_runbooks_search_also_scores_and_respects_doc_type()
    test_consistency_check_quiet_after_fresh_seed()
    test_consistency_check_flags_a_mismatched_model()
