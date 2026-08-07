"""Hand-written golden set for RAG Phase 5 (Retrieval evaluation harness) — one query per
document in the seed corpus, phrased as a human would describe the issue, not copied verbatim
from the document's own summary/title (that would trivially always match and prove nothing).
Used by rag/test_eval_harness.py's recall@k check.
"""
from dataclasses import dataclass


@dataclass
class GoldenCase:
    query: str
    expected: str  # exact `summary` (tickets) or `title` (runbooks) that should be retrieved


# One paraphrased query per historical ticket in data/historical_tickets.json.
TICKET_GOLDEN_SET: list[GoldenCase] = [
    GoldenCase("checkout-service issue", "Complete outage of checkout-service, all customers affected"),
    GoldenCase("someone broke into the admin panel", "Unauthorized access to internal admin panel detected"),
    GoldenCase("we leaked customer personal data in an export", "Customer PII exposed in support ticket export"),
    GoldenCase(
        "order service throwing intermittent errors during high traffic",
        "order-service intermittent 502s under peak load",
    ),
    GoldenCase("customers got billed twice for the same order", "Billing double-charged a batch of customers"),
    GoldenCase(
        "product search results seem less relevant lately",
        "Search results ranking noticeably degraded for product queries",
    ),
    GoldenCase("website footer shows an outdated year", "Footer copyright year still shows last year"),
    GoldenCase("password reset email has a typo in the subject", "Typo in password reset email subject line"),
    GoldenCase(
        "profile page is slow for accounts with lots of data",
        "Slow page load on user profile page for large accounts",
    ),
    GoldenCase("notification emails arriving late", "Notification emails delayed by up to 20 minutes"),
]

# One paraphrased query per runbook in data/runbooks_sla.json.
RUNBOOK_GOLDEN_SET: list[GoldenCase] = [
    GoldenCase("how do we classify severity for a new ticket", "RUNBOOK-SEVERITY: Severity classification policy"),
    GoldenCase("steps for handling a security incident", "RUNBOOK-SECURITY: Security incident handling"),
    GoldenCase("how to handle an outage", "RUNBOOK-OUTAGE: Outage handling"),
    GoldenCase("which team owns this component", "RUNBOOK-OWNERSHIP: Component to team ownership map"),
    GoldenCase("when does a ticket need human approval", "RUNBOOK-HITL: Human-in-the-loop approval policy"),
]
