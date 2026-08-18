"""Smoke test for RAG Phase 2: run a real ticket through the pipeline and confirm each RAG
search call along the way got logged into retrieval_log with a ticket_id/node_name and
plausible scores. Requires a live DB + LLM provider, same as agents/test_graph.py.
"""
import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy import select

from agents.graph import build_graph
from agents.integrations.mcp_client import shutdown_mcp_client
from config import get_settings
from db.models import RetrievalLog
from db.session import SessionLocal
from mcp_server import jira_store
from rag.retriever import search_similar_tickets

settings = get_settings()


def _retrieval_rows(ticket_id: str) -> list[RetrievalLog]:
    db = SessionLocal()
    try:
        return list(db.execute(select(RetrievalLog).where(RetrievalLog.ticket_id == ticket_id)).scalars())
    finally:
        db.close()


def test_untagged_call_is_not_logged() -> None:
    # A call with no ticket_id (e.g. from a script or the eval harness) shouldn't write a
    # meaningless row — this is the default when the param is omitted entirely.
    db = SessionLocal()
    try:
        before = db.query(RetrievalLog).count()
    finally:
        db.close()

    search_similar_tickets("some ad hoc query with no pipeline context", k=1)

    db = SessionLocal()
    try:
        after = db.query(RetrievalLog).count()
    finally:
        db.close()
    assert after == before, "an untagged retrieval call should not write a retrieval_log row"
    print("test_untagged_call_is_not_logged: PASS")


async def main() -> None:
    async with AsyncPostgresSaver.from_conn_string(settings.psycopg_dsn) as checkpointer:
        await checkpointer.setup()
        graph = build_graph().compile(checkpointer=checkpointer)

        summary = "Complete outage of order-service, all customers affected"
        description = "order-service is returning 500s for every request."
        ticket = jira_store.create_ticket(summary=summary, description=description, component="order-service", status="pending")
        ticket_id = ticket["id"]
        config = {"configurable": {"thread_id": ticket_id}}

        result = await graph.ainvoke(
            {
                "ticket_id": ticket_id,
                "summary": summary,
                "description": description,
                "component": "order-service",
                "reporter": "smoke-test",
            },
            config=config,
        )
        if result.get("__interrupt__"):
            await graph.ainvoke(Command(resume={"decision": "approved", "notes": "smoke test"}), config=config)

        rows = _retrieval_rows(ticket_id)
        # generic path: 1x search_similar_tickets + 2x search_runbooks (triage) + 1x search_runbooks (routing) = 4
        assert len(rows) == 4, f"expected 4 retrieval_log rows for the generic path, got {len(rows)}"

        node_names = {r.node_name for r in rows}
        assert node_names == {"triage", "routing"}, f"unexpected node_names: {node_names}"

        for row in rows:
            assert row.query_text, "query_text should never be empty"
            assert row.result_count >= 0
            if row.result_count > 0:
                assert row.top_similarity is not None and row.avg_similarity is not None

        print(f"retrieval_log for {ticket_id}: {len(rows)} rows")
        for row in sorted(rows, key=lambda r: r.created_at):
            print(f"  {row.node_name:10s} {row.tool_name:24s} n={row.result_count} top={row.top_similarity}")

    await shutdown_mcp_client()
    print("test_pipeline_calls_are_logged_with_ticket_and_node: PASS")


if __name__ == "__main__":
    test_untagged_call_is_not_logged()
    asyncio.run(main())
