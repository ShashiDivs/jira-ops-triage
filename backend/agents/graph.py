"""The Intake -> Triage -> Routing -> HITL Gate -> Action LangGraph pipeline, ported from
the reference design. Logic is unchanged; only the LLM backend (Azure AI Foundry), RAG
retrieval (pgvector), and Jira store (Postgres) underneath it have moved."""
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.guardrails import check_guardrails
from agents.llm import LLMContractError, classify_severity, classify_team
from agents.mcp_client import get_mcp_client
from agents.state import TicketState

_SENSITIVE_KEYWORDS = ["payment", "billing", "charge", "charged", "pii", "personal data"]


async def intake_node(state: TicketState) -> TicketState:
    return state


async def guardrail_node(state: TicketState) -> TicketState:
    reason = check_guardrails(state["summary"], state["description"])
    if reason:
        return {**state, "blocked": True, "block_reason": reason}
    return {**state, "blocked": False}


def _route_after_guardrail(state: TicketState) -> str:
    return "blocked_action" if state.get("blocked") else "triage"


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


async def triage_node(state: TicketState) -> TicketState:
    client = await get_mcp_client()
    query = f"{state['summary']} {state['description']}"

    similar = await client.call_tool("search_similar_tickets", {"query": query, "k": 3}) or []
    policy_docs = await client.call_tool(
        "search_runbooks", {"query": "severity classification policy", "k": 1, "doc_type": "severity_policy"}
    ) or []
    security_docs = await client.call_tool(
        "search_runbooks", {"query": "security incident handling", "k": 1, "doc_type": "security_runbook"}
    ) or []
    policy_text = "\n".join(d["content"] for d in [*policy_docs, *security_docs])

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
        "triage_reasoning": result["reasoning"],
    }


async def routing_node(state: TicketState) -> TicketState:
    client = await get_mcp_client()
    ownership_docs = await client.call_tool(
        "search_runbooks",
        {"query": f"team ownership for {state.get('component', '')}", "k": 1, "doc_type": "ownership_map"},
    ) or []
    ownership_text = "\n".join(d["content"] for d in ownership_docs)

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
        "final_status": decision["decision"],
    }


def _audit_body(state: TicketState) -> str:
    lines = [
        f"Triage: severity={state['severity']} confidence={state['confidence']:.2f} - {state['triage_reasoning']}",
        f"Routing: team={state['owning_team']} - {state['routing_reasoning']}",
    ]
    if state.get("requires_hitl"):
        lines.append(f"HITL: required ({', '.join(state['hitl_reasons'])}) -> {state['human_decision']}")
        if state.get("human_notes"):
            lines.append(f"Human notes: {state['human_notes']}")
    else:
        lines.append("HITL: not required, auto-approved")
    return "\n".join(lines)


async def action_node(state: TicketState) -> TicketState:
    client = await get_mcp_client()

    if state["final_status"] == "rejected":
        await client.call_tool("update_ticket", {"ticket_id": state["ticket_id"], "status": "rejected"})
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
    graph.add_node("triage", triage_node)
    graph.add_node("routing", routing_node)
    graph.add_node("hitl_gate", hitl_gate_node)
    graph.add_node("action", action_node)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "guardrail")
    graph.add_conditional_edges(
        "guardrail", _route_after_guardrail, {"blocked_action": "blocked_action", "triage": "triage"}
    )
    graph.add_edge("blocked_action", END)
    graph.add_edge("triage", "routing")
    graph.add_edge("routing", "hitl_gate")
    graph.add_edge("hitl_gate", "action")
    graph.add_edge("action", END)

    return graph
