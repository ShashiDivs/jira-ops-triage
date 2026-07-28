from fastapi import APIRouter, HTTPException

from agents.runtime import get_graph
from api.schemas import PendingTicketOut, TicketOut, TicketSubmitRequest, TicketSubmitResponse
from mcp_server import jira_store

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.post("", response_model=TicketSubmitResponse)
async def submit_ticket(payload: TicketSubmitRequest) -> TicketSubmitResponse:
    ticket = jira_store.create_ticket(
        summary=payload.summary,
        description=payload.description,
        component=payload.component,
        reporter=payload.reporter,
        status="pending",
    )
    ticket_id = ticket["id"]

    graph = get_graph()
    config = {"configurable": {"thread_id": ticket_id}}
    initial_state = {
        "ticket_id": ticket_id,
        "summary": payload.summary,
        "description": payload.description,
        "component": payload.component,
        "reporter": payload.reporter,
    }
    result = await graph.ainvoke(initial_state, config=config)

    if result.get("__interrupt__"):
        return TicketSubmitResponse(ticket_id=ticket_id, status="pending")

    final = jira_store.get_ticket(ticket_id)
    return TicketSubmitResponse(ticket_id=ticket_id, status=final["status"] if final else "unknown")


@router.get("", response_model=list[TicketOut])
async def list_tickets(status: str | None = None) -> list[TicketOut]:
    return [TicketOut(**t) for t in jira_store.list_tickets(status=status)]


@router.get("/pending", response_model=list[PendingTicketOut])
async def list_pending() -> list[PendingTicketOut]:
    pending = jira_store.list_tickets(status="pending")
    graph = get_graph()
    out: list[PendingTicketOut] = []
    for t in pending:
        config = {"configurable": {"thread_id": t["id"]}}
        snapshot = await graph.aget_state(config)
        values = snapshot.values if snapshot else {}
        out.append(
            PendingTicketOut(
                ticket_id=t["id"],
                summary=t["summary"],
                description=t["description"],
                component=t["component"],
                severity=values.get("severity"),
                confidence=values.get("confidence"),
                owning_team=values.get("owning_team"),
                triage_reasoning=values.get("triage_reasoning"),
                routing_reasoning=values.get("routing_reasoning"),
                hitl_reasons=values.get("hitl_reasons", []),
                similar_tickets=values.get("similar_tickets", []),
            )
        )
    return out


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(ticket_id: str) -> TicketOut:
    ticket = jira_store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return TicketOut(**ticket)
