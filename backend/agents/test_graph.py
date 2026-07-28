"""Standalone agent graph smoke test: runs 3 tickets through the full pipeline —
an auto-approved case, a HITL-approved case, and a HITL-rejected case — mirroring
the reference README's test_graph.py."""
import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from agents.graph import build_graph
from agents.mcp_client import shutdown_mcp_client
from config import get_settings
from mcp_server import jira_store

settings = get_settings()


async def run_ticket(graph, summary: str, description: str, component: str, decision_if_hitl: str = "approved") -> None:
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
        print(f"[{ticket_id}] paused for HITL -> resuming with decision={decision_if_hitl}")
        result = await graph.ainvoke(
            Command(resume={"decision": decision_if_hitl, "notes": "smoke test decision"}), config=config
        )

    final = jira_store.get_ticket(ticket_id)
    print(
        f"[{ticket_id}] final status={final['status']} severity={final['severity']} "
        f"team={final['owning_team']} confidence={final['confidence']}"
    )


async def main() -> None:
    async with AsyncPostgresSaver.from_conn_string(settings.psycopg_dsn) as checkpointer:
        await checkpointer.setup()
        graph = build_graph().compile(checkpointer=checkpointer)

        print("--- Case 1: auto-approved (low severity, non-sensitive) ---")
        await run_ticket(
            graph,
            "Footer copyright year is wrong",
            "The marketing site footer still shows last year's copyright year.",
            "marketing-site",
        )

        print("--- Case 2: HITL required, then approved (S1 outage) ---")
        await run_ticket(
            graph,
            "Complete outage of order-service, all customers affected",
            "order-service is returning 500s for every request. No orders are completing.",
            "order-service",
            decision_if_hitl="approved",
        )

        print("--- Case 3: HITL required, then rejected (security incident) ---")
        await run_ticket(
            graph,
            "Unauthorized access to admin panel detected",
            "Repeated successful logins from an unrecognized IP using a decommissioned service account.",
            "admin-panel",
            decision_if_hitl="rejected",
        )

    await shutdown_mcp_client()


if __name__ == "__main__":
    asyncio.run(main())
