"""Smoke test for the supervisor's conditional routing (Phase 3): a security-flagged ticket,
a cosmetic ticket, and a generic outage ticket should each traverse a different triage path.
Requires a live DB + LLM provider, same as test_graph.py.
"""
import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from agents.graph import build_graph
from agents.integrations.mcp_client import shutdown_mcp_client
from config import get_settings
from mcp_server import jira_store

settings = get_settings()


async def run_ticket(graph, summary: str, description: str, component: str, decision_if_hitl: str = "approved") -> dict:
    ticket = jira_store.create_ticket(summary=summary, description=description, component=component, status="pending")
    ticket_id = ticket["id"]
    config = {"configurable": {"thread_id": ticket_id}}

    result = await graph.ainvoke(
        {
            "ticket_id": ticket_id,
            "summary": summary,
            "description": description,
            "component": component,
            "reporter": "smoke-test",
        },
        config=config,
    )
    if result.get("__interrupt__"):
        await graph.ainvoke(Command(resume={"decision": decision_if_hitl, "notes": "smoke test"}), config=config)
    return jira_store.get_ticket(ticket_id)


async def main() -> None:
    async with AsyncPostgresSaver.from_conn_string(settings.psycopg_dsn) as checkpointer:
        await checkpointer.setup()
        graph = build_graph().compile(checkpointer=checkpointer)

        print("--- Case 1: security-flagged -> security path ---")
        security = await run_ticket(
            graph,
            "Unauthorized access to internal admin panel detected",
            "Repeated logins from an unrecognized IP using a decommissioned service account.",
            "admin-panel",
        )
        reasoning = security["reasoning"].get("triage", "") if security["reasoning"] else ""
        assert "[security path]" in reasoning, f"expected security path tag, got: {reasoning!r}"
        print(f"[{security['id']}] took security path as expected: {reasoning[:80]}...")

        print("--- Case 2: cosmetic -> trivial/fast path ---")
        trivial = await run_ticket(
            graph,
            "Footer copyright year is wrong",
            "The marketing site footer still shows last year's copyright year — just a typo fix.",
            "marketing-site",
        )
        reasoning = trivial["reasoning"].get("triage", "") if trivial["reasoning"] else ""
        assert "[trivial/fast path]" in reasoning, f"expected trivial path tag, got: {reasoning!r}"
        assert trivial["status"] == "auto_approved", f"expected auto_approved, got {trivial['status']}"
        print(f"[{trivial['id']}] took trivial path as expected: {reasoning[:80]}...")

        print("--- Case 3: generic outage -> generic path (unchanged behavior) ---")
        generic = await run_ticket(
            graph,
            "Complete outage of order-service, all customers affected",
            "order-service is returning 500s for every request. No orders are completing.",
            "order-service",
        )
        reasoning = generic["reasoning"].get("triage", "") if generic["reasoning"] else ""
        assert "[generic path]" in reasoning, f"expected generic path tag, got: {reasoning!r}"
        assert generic["severity"] == "S1", f"expected S1, got {generic['severity']}"
        print(f"[{generic['id']}] took generic path as expected: {reasoning[:80]}...")

    await shutdown_mcp_client()


if __name__ == "__main__":
    asyncio.run(main())
