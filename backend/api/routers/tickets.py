from fastapi import APIRouter, Depends, HTTPException, Request

from agents.runtime import get_graph, get_pending_interrupt_payload
from api.demo_guard import SubmissionLimiter, client_address
from api.schemas import PendingTicketOut, TicketOut, TicketSubmitRequest, TicketSubmitResponse
from config import get_settings
from mcp_server import jira_store
from rag.promotion import PromotionError, promote_ticket_to_rag

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

_settings = get_settings()
_limiter = SubmissionLimiter(_settings.ticket_rate_limit_per_minute, _settings.ticket_submission_cap)


def limit_submissions(request: Request) -> None:
    _limiter.check(client_address(request))


@router.post("", response_model=TicketSubmitResponse, dependencies=[Depends(limit_submissions)])
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
    out: list[PendingTicketOut] = []
    for t in pending:
        payload = await get_pending_interrupt_payload(t["id"])
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


@router.post("/{ticket_id}/promote-to-rag")
async def promote_ticket(ticket_id: str) -> dict:
    """Manually promote a resolved (approved) ticket into the RAG corpus. Not automatic on
    approval — see rag/promotion.py for why."""
    try:
        return promote_ticket_to_rag(ticket_id)
    except PromotionError as e:
        raise HTTPException(status_code=409, detail=str(e))
