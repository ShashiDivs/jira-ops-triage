from fastapi import APIRouter, HTTPException
from langgraph.types import Command

from agents.safety.access_control import AuthorizationError, authorize_approver
from agents.runtime import get_graph, get_pending_interrupt_payload
from api.schemas import ApprovalRequest, TicketOut
from mcp_server import jira_store

router = APIRouter(prefix="/api/tickets", tags=["approvals"])


async def _resume(ticket_id: str, decision: str, approver: str, notes: str) -> TicketOut:
    ticket = jira_store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if ticket["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"ticket is not pending approval (status={ticket['status']})")

    # RBAC gate (Governance — Access Control): checked against this ticket's actual hitl_reasons
    # *before* the decision is applied, never after — an unauthorized approver must not be able
    # to move the ticket state at all.
    payload = await get_pending_interrupt_payload(ticket_id)
    try:
        authorize_approver(approver, payload.get("hitl_reasons", []))
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))

    graph = get_graph()
    config = {"configurable": {"thread_id": ticket_id}}
    await graph.ainvoke(Command(resume={"decision": decision, "notes": notes, "approver": approver}), config=config)

    updated = jira_store.get_ticket(ticket_id)
    return TicketOut(**updated)


@router.post("/{ticket_id}/approve", response_model=TicketOut)
async def approve_ticket(ticket_id: str, payload: ApprovalRequest) -> TicketOut:
    return await _resume(ticket_id, "approved", payload.approver, payload.notes)


@router.post("/{ticket_id}/reject", response_model=TicketOut)
async def reject_ticket(ticket_id: str, payload: ApprovalRequest) -> TicketOut:
    return await _resume(ticket_id, "rejected", payload.approver, payload.notes)
