from typing import Any, TypedDict


class TicketState(TypedDict, total=False):
    ticket_id: str
    summary: str
    description: str
    component: str
    reporter: str

    blocked: bool
    block_reason: str

    route: str  # "security" | "trivial" | "generic" — set by the supervisor node

    similar_tickets: list[dict[str, Any]]
    severity: str
    confidence: float
    is_security_related: bool
    triage_reasoning: str

    ownership_doc: str
    owning_team: str
    routing_reasoning: str

    requires_hitl: bool
    hitl_reasons: list[str]

    human_decision: str  # "approved" | "rejected" | ""
    human_notes: str
    human_approver: str  # reviewer identity (RBAC) — "" for auto-approved (no human involved)

    final_status: str
