from typing import Any

from pydantic import BaseModel


class TicketSubmitRequest(BaseModel):
    summary: str
    description: str
    component: str = ""
    reporter: str = ""


class TicketSubmitResponse(BaseModel):
    ticket_id: str
    status: str


class CommentOut(BaseModel):
    author: str
    body: str
    created_at: str


class TicketOut(BaseModel):
    id: str
    summary: str
    description: str
    component: str
    reporter: str
    severity: str | None = None
    confidence: float | None = None
    owning_team: str | None = None
    status: str
    reasoning: dict[str, Any] = {}
    created_at: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    comments: list[CommentOut] = []


class PendingTicketOut(BaseModel):
    ticket_id: str
    summary: str
    description: str
    component: str
    severity: str | None = None
    confidence: float | None = None
    owning_team: str | None = None
    triage_reasoning: str | None = None
    routing_reasoning: str | None = None
    hitl_reasons: list[str] = []
    similar_tickets: list[dict[str, Any]] = []


class ApprovalRequest(BaseModel):
    approver: str
    notes: str = ""
