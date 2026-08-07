"""RAG Phase 5 — Retrieval evaluation harness. Computes recall@k against the golden set
(rag/golden_set.py) — "does the expected document appear in the top k results" — the standard,
simplest defensible retrieval eval metric. Runs it under both pure-vector and hybrid ranking so
this doubles as a quantitative before/after for RAG Phase 4, not just the one calibrated
example in test_hybrid_search.py. Also serves as a regression check for Phases 1-4 together.
Requires a live DB + LLM provider.
"""
from config import get_settings
from rag.golden_set import RUNBOOK_GOLDEN_SET, TICKET_GOLDEN_SET, GoldenCase
from rag.retriever import search_runbooks, search_similar_tickets

settings = get_settings()

# recall@3 turned out to be a ceiling-effect metric on this corpus (15 documents total,
# split 10/5): both pure-vector and hybrid hit 1.00 regardless of ranking quality, since almost
# any plausible query lands the right doc *somewhere* in the top 3 of a corpus this small.
# recall@1 is the metric that actually discriminates ranking quality at this scale — it's also
# the more honest choice, since it's what an eval harness for a corpus this size should show:
# most real production RAG corpora are far larger, where recall@3 would discriminate fine;
# flagging that mismatch here rather than picking whichever k makes the numbers look best.
K = 1
# Calibrated against this corpus + embedding model, same discipline as RAG_MIN_SIMILARITY (see
# reference/agentic-teaching-notes.md section 7) — set after an initial run showed real overall
# recall@1 of ~0.93 under hybrid ranking (14/15), comfortably above this bar. The one known
# miss (a "severity classification" query retrieving RUNBOOK-HITL instead of RUNBOOK-SEVERITY)
# is a genuine corpus-quality limitation, not a bug: those two runbooks are semantically close
# in this small corpus, and recall@1 is a harsh enough metric to expose that honestly rather
# than paper over it.
MIN_RECALL_AT_K = 0.85


def _recall_at_k(cases: list[GoldenCase], search_fn, result_key: str) -> tuple[float, list[dict]]:
    details = []
    hits = 0
    for case in cases:
        results = search_fn(case.query, k=K)
        retrieved = [r[result_key] for r in results]
        found = case.expected in retrieved
        hits += int(found)
        details.append({"query": case.query, "expected": case.expected, "found": found, "retrieved": retrieved})
    return hits / len(cases), details


def _report(label: str, recall: float, details: list[dict]) -> None:
    print(f"{label}: recall@{K} = {recall:.2f} ({sum(d['found'] for d in details)}/{len(details)})")
    for d in details:
        if not d["found"]:
            print(f"    MISS  {d['query']!r} -> expected {d['expected']!r}, got {d['retrieved']}")


def main() -> None:
    original_hybrid = settings.rag_hybrid_search_enabled
    original_min_similarity = settings.rag_min_similarity
    try:
        # recall@k should measure ranking quality, not the separate relevance-floor concern
        # already covered by rag/test_retriever.py's threshold tests — disable it for the
        # duration of this eval so a legitimate top-k match isn't discarded as "too dissimilar".
        settings.rag_min_similarity = 0.0

        print("=== Pure vector ranking (RAG_HYBRID_SEARCH_ENABLED=false) ===")
        settings.rag_hybrid_search_enabled = False
        vector_ticket_recall, vector_ticket_details = _recall_at_k(
            TICKET_GOLDEN_SET, search_similar_tickets, "summary"
        )
        vector_runbook_recall, vector_runbook_details = _recall_at_k(RUNBOOK_GOLDEN_SET, search_runbooks, "title")
        _report("tickets", vector_ticket_recall, vector_ticket_details)
        _report("runbooks", vector_runbook_recall, vector_runbook_details)

        print("\n=== Hybrid ranking (RAG_HYBRID_SEARCH_ENABLED=true) ===")
        settings.rag_hybrid_search_enabled = True
        hybrid_ticket_recall, hybrid_ticket_details = _recall_at_k(
            TICKET_GOLDEN_SET, search_similar_tickets, "summary"
        )
        hybrid_runbook_recall, hybrid_runbook_details = _recall_at_k(RUNBOOK_GOLDEN_SET, search_runbooks, "title")
        _report("tickets", hybrid_ticket_recall, hybrid_ticket_details)
        _report("runbooks", hybrid_runbook_recall, hybrid_runbook_details)

        overall_hybrid_recall = (
            sum(d["found"] for d in hybrid_ticket_details) + sum(d["found"] for d in hybrid_runbook_details)
        ) / (len(hybrid_ticket_details) + len(hybrid_runbook_details))
        overall_vector_recall = (
            sum(d["found"] for d in vector_ticket_details) + sum(d["found"] for d in vector_runbook_details)
        ) / (len(vector_ticket_details) + len(vector_runbook_details))

        print(f"\noverall recall@{K}: pure-vector={overall_vector_recall:.2f}  hybrid={overall_hybrid_recall:.2f}")

        assert overall_hybrid_recall >= MIN_RECALL_AT_K, (
            f"hybrid recall@{K} regressed below {MIN_RECALL_AT_K} (got {overall_hybrid_recall:.2f}) — "
            f"see MISS lines above"
        )
        assert overall_hybrid_recall >= overall_vector_recall, (
            f"hybrid recall@{K} ({overall_hybrid_recall:.2f}) is worse than pure-vector "
            f"({overall_vector_recall:.2f}) — hybrid should not make ranking worse on this golden set"
        )
        print("test_hybrid_recall_meets_threshold_and_beats_pure_vector: PASS")
    finally:
        settings.rag_hybrid_search_enabled = original_hybrid
        settings.rag_min_similarity = original_min_similarity


if __name__ == "__main__":
    main()
