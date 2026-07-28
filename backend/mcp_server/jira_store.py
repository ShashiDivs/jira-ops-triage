"""Mock Jira backend, Postgres-persisted (replaces the reference project's JSON-file store).
Same function signatures the MCP server exposes as tools, so callers don't change.
"""
from datetime import datetime, timezone

from db.models import Comment, Ticket
from db.session import SessionLocal


def _ticket_to_dict(t: Ticket, comments: list[Comment] | None = None) -> dict:
    return {
        "id": t.id,
        "summary": t.summary,
        "description": t.description,
        "component": t.component,
        "reporter": t.reporter,
        "severity": t.severity,
        "confidence": t.confidence,
        "owning_team": t.owning_team,
        "status": t.status,
        "reasoning": t.reasoning,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "decided_at": t.decided_at.isoformat() if t.decided_at else None,
        "decided_by": t.decided_by,
        "comments": [
            {"author": c.author, "body": c.body, "created_at": c.created_at.isoformat()}
            for c in (comments if comments is not None else t.comments)
        ],
    }


def create_ticket(
    summary: str,
    description: str,
    component: str = "",
    reporter: str = "",
    severity: str | None = None,
    confidence: float | None = None,
    owning_team: str | None = None,
    status: str = "pending",
    reasoning: dict | None = None,
) -> dict:
    db = SessionLocal()
    try:
        ticket = Ticket(
            summary=summary,
            description=description,
            component=component,
            reporter=reporter,
            severity=severity,
            confidence=confidence,
            owning_team=owning_team,
            status=status,
            reasoning=reasoning or {},
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        return _ticket_to_dict(ticket)
    finally:
        db.close()


def update_ticket(ticket_id: str, **fields) -> dict:
    db = SessionLocal()
    try:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            raise ValueError(f"ticket {ticket_id} not found")
        for key, value in fields.items():
            if hasattr(ticket, key):
                setattr(ticket, key, value)
        if fields.get("status") in ("approved", "rejected", "auto_approved") and ticket.decided_at is None:
            ticket.decided_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(ticket)
        return _ticket_to_dict(ticket)
    finally:
        db.close()


def add_comment(ticket_id: str, author: str, body: str) -> dict:
    db = SessionLocal()
    try:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            raise ValueError(f"ticket {ticket_id} not found")
        comment = Comment(ticket_id=ticket_id, author=author, body=body)
        db.add(comment)
        db.commit()
        db.refresh(comment)
        return {"author": comment.author, "body": comment.body, "created_at": comment.created_at.isoformat()}
    finally:
        db.close()


def get_ticket(ticket_id: str) -> dict | None:
    db = SessionLocal()
    try:
        ticket = db.get(Ticket, ticket_id)
        return _ticket_to_dict(ticket) if ticket else None
    finally:
        db.close()


def list_tickets(status: str | None = None) -> list[dict]:
    db = SessionLocal()
    try:
        q = db.query(Ticket)
        if status:
            q = q.filter(Ticket.status == status)
        tickets = q.order_by(Ticket.created_at.desc()).all()
        return [_ticket_to_dict(t) for t in tickets]
    finally:
        db.close()


def reset_all() -> None:
    db = SessionLocal()
    try:
        db.query(Comment).delete()
        db.query(Ticket).delete()
        db.commit()
    finally:
        db.close()
