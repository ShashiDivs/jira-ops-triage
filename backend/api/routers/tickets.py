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

        # hitl_gate_node is paused mid-execution at interrupt() when a ticket is pending, so
        # snapshot.values only reflects state as of *entering* that node — it doesn't yet have
        # hitl_reasons/etc, since those are only set in the node's (not-yet-executed) return
        # statement. The actual payload passed to interrupt() is what carries them.
        payload: dict = {}
        if snapshot:
            for task in snapshot.tasks:
                if task.interrupts:
                    payload = task.interrupts[0].value
                    break

        out.append(
            PendingTicketOut(
                ticket_id=t["id"],
                summary=t["summary"],
                description=t["description"],
                component=t["component"],
                severity=payload.get("severity"),
                confidence=payload.get("confidence"),
                owning_team=payload.get("owning_team"),
                triage_reasoning=payload.get("triage_reasoning"),
                routing_reasoning=payload.get("routing_reasoning"),
                hitl_reasons=payload.get("hitl_reasons", []),
                similar_tickets=payload.get("similar_tickets", []),
            )
        )
    return out


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(ticket_id: str) -> TicketOut:
    ticket = jira_store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return TicketOut(**ticket)
