"""The Intake -> Triage -> Routing -> HITL Gate -> Action LangGraph pipeline, ported from
the reference design. Logic is unchanged; only the LLM backend (Azure AI Foundry), RAG
retrieval (pgvector), and Jira store (Postgres) underneath it have moved."""
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.safety.guardrails import check_guardrails
from agents.llm.client import (
    COSMETIC_KEYWORDS,
    SECURITY_KEYWORDS,
    LLMContractError,
    classify_severity,
    classify_severity_fast,
    classify_severity_security,
    classify_team,
    format_runbook_doc,
)
from agents.integrations.mcp_client import get_mcp_client
from agents.integrations.observability import traced_node
from agents.state import TicketState

_SENSITIVE_KEYWORDS = ["payment", "billing", "charge", "charged", "pii", "personal data"]


@traced_node("intake")
async def intake_node(state: TicketState) -> TicketState:
    return state


@traced_node("guardrail")
async def guardrail_node(state: TicketState) -> TicketState:
    reason = check_guardrails(state["summary"], state["description"])
    if reason:
        return {**state, "blocked": True, "block_reason": reason}
    return {**state, "blocked": False}


def _route_after_guardrail(state: TicketState) -> str:
    return "blocked_action" if state.get("blocked") else "supervisor"


@traced_node("blocked_action")
async def blocked_action_node(state: TicketState) -> TicketState:
    client = await get_mcp_client()
    await client.call_tool("update_ticket", {"ticket_id": state["ticket_id"], "status": "blocked"})
    await client.call_tool(
        "add_comment",
        {
            "ticket_id": state["ticket_id"],
            "author": "ops-triage-agent",
            "body": f"Blocked by input guardrail before triage: {state.get('block_reason', '')}",
        },
    )
    return {**state, "final_status": "blocked"}


# ---------------------------------------------------------------------------
# Supervisor: routes each ticket to a specialized triage path based on what it received,
# rather than running the same generic triage on every ticket (Orchestrator/Supervisor
# pattern). Reuses llm.py's own keyword lists so this routing decision and the
# keyword-heuristic fallback's decision can't drift apart.
# ---------------------------------------------------------------------------

@traced_node("supervisor")
async def supervisor_node(state: TicketState) -> TicketState:
    text = f"{state['summary']} {state['description']}".lower()
    if any(kw in text for kw in SECURITY_KEYWORDS):
        route = "security"
    elif any(kw in text for kw in COSMETIC_KEYWORDS):
        route = "trivial"
    else:
        route = "generic"
    return {**state, "route": route}


def _route_from_supervisor(state: TicketState) -> str:
    return {"security": "triage_security", "trivial": "triage_trivial", "generic": "triage"}[state["route"]]


@traced_node("triage_security")
async def triage_security_node(state: TicketState) -> TicketState:
    client = await get_mcp_client()
    query = f"{state['summary']} {state['description']}"

    similar = await client.call_tool(
        "search_similar_tickets", {"query": query, "k": 3, "ticket_id": state["ticket_id"], "node_name": "triage_security"}
    ) or []
    security_docs = await client.call_tool(
        "search_runbooks",
        {
            "query": "security incident handling",
            "k": 2,
            "doc_type": "security_runbook",
            "ticket_id": state["ticket_id"],
            "node_name": "triage_security",
        },
    ) or []
    security_text = "\n".join(format_runbook_doc(d) for d in security_docs)

    try:
        result = classify_severity_security(
            {
                "summary": state["summary"],
                "description": state["description"],
                "component": state.get("component", ""),
            },
            similar,
            security_text,
        )
    except LLMContractError as e:
        result = {
            "severity": "S1",
            "confidence": 0.0,
            "is_security_related": True,
            "reasoning": f"Security triage contract violation, escalating for human review: {e}",
        }
    return {
        **state,
        "similar_tickets": similar,
        "severity": result["severity"],
        "confidence": result["confidence"],
        "is_security_related": result.get("is_security_related", True),
        "triage_reasoning": f"[security path] {result['reasoning']}",
    }


@traced_node("triage_trivial")
async def triage_trivial_node(state: TicketState) -> TicketState:
    try:
        result = classify_severity_fast(
            {
                "summary": state["summary"],
                "description": state["description"],
                "component": state.get("component", ""),
            }
        )
    except LLMContractError as e:
        result = {
            "severity": "S1",
            "confidence": 0.0,
            "is_security_related": False,
            "reasoning": f"Fast-path triage contract violation, escalating for human review: {e}",
        }
    return {
        **state,
        "similar_tickets": [],
        "severity": result["severity"],
        "confidence": result["confidence"],
        "is_security_related": result.get("is_security_related", False),
        "triage_reasoning": f"[trivial/fast path] {result['reasoning']}",
    }


@traced_node("triage")
async def triage_generic_node(state: TicketState) -> TicketState:
    client = await get_mcp_client()
    query = f"{state['summary']} {state['description']}"

    similar = await client.call_tool(
        "search_similar_tickets", {"query": query, "k": 3, "ticket_id": state["ticket_id"], "node_name": "triage"}
    ) or []
    policy_docs = await client.call_tool(
        "search_runbooks",
        {
            "query": "severity classification policy",
            "k": 1,
            "doc_type": "severity_policy",
            "ticket_id": state["ticket_id"],
            "node_name": "triage",
        },
    ) or []
    security_docs = await client.call_tool(
        "search_runbooks",
        {
            "query": "security incident handling",
            "k": 1,
            "doc_type": "security_runbook",
            "ticket_id": state["ticket_id"],
            "node_name": "triage",
        },
    ) or []
    policy_text = "\n".join(format_runbook_doc(d) for d in [*policy_docs, *security_docs])

    try:
        result = classify_severity(
            {
                "summary": state["summary"],
                "description": state["description"],
                "component": state.get("component", ""),
            },
            similar,
            policy_text,
        )
    except LLMContractError as e:
        # Fail-safe: an LLM that won't produce valid structured output gets treated as the
        # most severe, least-trusted case, guaranteeing a human reviews it (via both the
        # S1 and confidence<0.7 HITL triggers) rather than silently crashing the ticket.
        result = {
            "severity": "S1",
            "confidence": 0.0,
            "is_security_related": False,
            "reasoning": f"Triage contract violation, escalating for human review: {e}",
        }
    return {
        **state,
        "similar_tickets": similar,
        "severity": result["severity"],
        "confidence": result["confidence"],
        "is_security_related": result.get("is_security_related", False),
        "triage_reasoning": f"[generic path] {result['reasoning']}",
    }


@traced_node("routing")
async def routing_node(state: TicketState) -> TicketState:
    client = await get_mcp_client()
    ownership_docs = await client.call_tool(
        "search_runbooks",
        {
            "query": f"team ownership for {state.get('component', '')}",
            "k": 1,
            "doc_type": "ownership_map",
            "ticket_id": state["ticket_id"],
            "node_name": "routing",
        },
    ) or []
    ownership_text = "\n".join(format_runbook_doc(d) for d in ownership_docs)

    try:
        result = classify_team({"component": state.get("component", "")}, ownership_text)
    except LLMContractError as e:
        # Fail-safe default team, same rationale as triage_node — a HITL-required ticket
        # gets human eyes anyway; an auto-approved one still gets a reasonable routing.
        result = {
            "team": "Platform",
            "reasoning": f"Routing contract violation, defaulted to Platform: {e}",
        }
    return {
        **state,
        "ownership_doc": ownership_text,
        "owning_team": result["team"],
        "routing_reasoning": result["reasoning"],
    }


def _hitl_reasons(state: TicketState) -> list[str]:
    reasons = []
    if state.get("severity") == "S1":
        reasons.append("Severity is S1")
    if state.get("is_security_related"):
        reasons.append("Ticket is security-related")
    if (state.get("confidence") if state.get("confidence") is not None else 1.0) < 0.7:
        reasons.append("Triage confidence is below 0.7")
    text = f"{state['summary']} {state['description']}".lower()
    if any(kw in text for kw in _SENSITIVE_KEYWORDS):
        reasons.append("Ticket involves payment, billing, or PII")
    return reasons


@traced_node("hitl_gate")
async def hitl_gate_node(state: TicketState) -> TicketState:
    reasons = _hitl_reasons(state)
    if not reasons:
        return {
            **state,
            "requires_hitl": False,
            "hitl_reasons": [],
            "human_decision": "auto_approved",
            "final_status": "auto_approved",
        }

    decision = interrupt(
        {
            "ticket_id": state["ticket_id"],
            "summary": state["summary"],
            "severity": state["severity"],
            "confidence": state["confidence"],
            "owning_team": state["owning_team"],
            "triage_reasoning": state["triage_reasoning"],
            "routing_reasoning": state["routing_reasoning"],
            "similar_tickets": state["similar_tickets"],
            "hitl_reasons": reasons,
        }
    )
    return {
        **state,
        "requires_hitl": True,
        "hitl_reasons": reasons,
        "human_decision": decision["decision"],
        "human_notes": decision.get("notes", ""),
        "human_approver": decision.get("approver", ""),
        "final_status": decision["decision"],
    }


def _audit_body(state: TicketState) -> str:
    lines = [
        f"Triage: severity={state['severity']} confidence={state['confidence']:.2f} - {state['triage_reasoning']}",
        f"Routing: team={state['owning_team']} - {state['routing_reasoning']}",
    ]
    if state.get("requires_hitl"):
        approver = state.get("human_approver") or "unknown"
        lines.append(f"HITL: required ({', '.join(state['hitl_reasons'])}) -> {state['human_decision']} by {approver}")
        if state.get("human_notes"):
            lines.append(f"Human notes: {state['human_notes']}")
    else:
        lines.append("HITL: not required, auto-approved")
    return "\n".join(lines)


@traced_node("action")
async def action_node(state: TicketState) -> TicketState:
    client = await get_mcp_client()

    # decided_by is only meaningful when a human actually made the call — auto_approved
    # tickets never went through hitl_gate_node's interrupt, so there's no reviewer identity.
    decided_by = state.get("human_approver") or None

    if state["final_status"] == "rejected":
        await client.call_tool(
            "update_ticket", {"ticket_id": state["ticket_id"], "status": "rejected", "decided_by": decided_by}
        )
        await client.call_tool(
            "add_comment",
            {"ticket_id": state["ticket_id"], "author": "ops-triage-agent", "body": _audit_body(state)},
        )
        return state

    await client.call_tool(
        "update_ticket",
        {
            "ticket_id": state["ticket_id"],
            "severity": state["severity"],
            "confidence": state["confidence"],
            "owning_team": state["owning_team"],
            "status": state["final_status"],
            "decided_by": decided_by,
            "reasoning": {
                "triage": state["triage_reasoning"],
                "routing": state["routing_reasoning"],
                "hitl_reasons": state.get("hitl_reasons", []),
            },
        },
    )
    await client.call_tool(
        "add_comment",
        {"ticket_id": state["ticket_id"], "author": "ops-triage-agent", "body": _audit_body(state)},
    )
    return state


def build_graph() -> StateGraph:
    graph = StateGraph(TicketState)
    graph.add_node("intake", intake_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("blocked_action", blocked_action_node)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("triage", triage_generic_node)
    graph.add_node("triage_security", triage_security_node)
    graph.add_node("triage_trivial", triage_trivial_node)
    graph.add_node("routing", routing_node)
    graph.add_node("hitl_gate", hitl_gate_node)
    graph.add_node("action", action_node)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "guardrail")
    graph.add_conditional_edges(
        "guardrail", _route_after_guardrail, {"blocked_action": "blocked_action", "supervisor": "supervisor"}
    )
    graph.add_edge("blocked_action", END)
    graph.add_conditional_edges(
        "supervisor",
        _route_from_supervisor,
        {"triage_security": "triage_security", "triage_trivial": "triage_trivial", "triage": "triage"},
    )
    graph.add_edge("triage", "routing")
    graph.add_edge("triage_security", "routing")
    graph.add_edge("triage_trivial", "routing")
    graph.add_edge("routing", "hitl_gate")
    graph.add_edge("hitl_gate", "action")
    graph.add_edge("action", END)

    return graph
