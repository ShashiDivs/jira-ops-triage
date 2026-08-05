"""Smoke test for RAG Phase 3 (Corpus growth / promotion): promote a resolved ticket into the
RAG corpus, confirm it's retrievable, confirm duplicate promotion is refused, and confirm an
ineligible (rejected) ticket can't be promoted at all. Requires a live DB + LLM provider.
"""
import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy import select

from agents.graph import build_graph
from agents.mcp_client import shutdown_mcp_client
from config import get_settings
from db.models import HistoricalTicket
from db.session import SessionLocal
from mcp_server import jira_store
from rag.promotion import PromotionError, promote_ticket_to_rag
from rag.retriever import search_similar_tickets

settings = get_settings()


async def _run_to_decision(graph, summary: str, description: str, component: str, decision: str) -> str:
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
        await graph.ainvoke(Command(resume={"decision": decision, "notes": "Rolled back the bad deploy."}), config=config)
    return ticket_id


def _historical_row_count_for(ticket_id: str) -> int:
    db = SessionLocal()
    try:
        return len(list(db.execute(select(HistoricalTicket).where(HistoricalTicket.source_ticket_id == ticket_id)).scalars()))
    finally:
        db.close()


async def main() -> None:
    async with AsyncPostgresSaver.from_conn_string(settings.psycopg_dsn) as checkpointer:
        await checkpointer.setup()
        graph = build_graph().compile(checkpointer=checkpointer)

        print("--- Case 1: promote an approved ticket ---")
        approved_id = await _run_to_decision(
            graph,
            "Complete outage of billing-service, all customers affected",
            "billing-service is returning 500s for every request.",
            "billing-service",
            "approved",
        )
        assert jira_store.get_ticket(approved_id)["status"] == "approved"

        result = promote_ticket_to_rag(approved_id)
        assert result["source_ticket_id"] == approved_id
        assert _historical_row_count_for(approved_id) == 1
        print(f"promoted {approved_id} -> historical_tickets id {result['id']}")

        matches = search_similar_tickets("Complete outage of billing-service, all customers affected", k=5)
        assert any(m["id"] == result["id"] for m in matches), "promoted ticket should be retrievable"
        print("test_promote_approved_ticket_is_retrievable: PASS")

        print("--- Case 2: promoting the same ticket again is refused ---")
        try:
            promote_ticket_to_rag(approved_id)
            raise AssertionError("expected a second promotion of the same ticket to be refused")
        except PromotionError as e:
            assert "already promoted" in str(e)
        assert _historical_row_count_for(approved_id) == 1, "should still be exactly one row, not a duplicate"
        print("test_duplicate_promotion_is_refused: PASS")

        print("--- Case 3: a rejected ticket is not eligible ---")
        rejected_id = await _run_to_decision(
            graph,
            "Unauthorized access to admin panel detected",
            "Repeated logins from an unrecognized IP using a decommissioned account.",
            "admin-panel",
            "rejected",
        )
        assert jira_store.get_ticket(rejected_id)["status"] == "rejected"
        try:
            promote_ticket_to_rag(rejected_id)
            raise AssertionError("expected a rejected ticket to be ineligible for promotion")
        except PromotionError as e:
            assert "only approved tickets" in str(e)
        assert _historical_row_count_for(rejected_id) == 0
        print("test_ineligible_ticket_cannot_be_promoted: PASS")

        print("--- Case 4: a near-duplicate of an already-promoted ticket is refused ---")
        near_dup_id = await _run_to_decision(
            graph,
            "Complete outage of billing-service, all customers affected",
            "billing-service is returning 500s for every request.",
            "billing-service",
            "approved",
        )
        assert near_dup_id != approved_id
        before_count = _historical_row_count_for(near_dup_id)
        assert before_count == 0
        try:
            promote_ticket_to_rag(near_dup_id)
            raise AssertionError("expected a near-duplicate ticket to be refused")
        except PromotionError as e:
            assert "near-duplicate" in str(e)
        assert _historical_row_count_for(near_dup_id) == 0, "the near-duplicate itself must not be inserted"
        print("test_near_duplicate_promotion_is_refused: PASS")

    await shutdown_mcp_client()


if __name__ == "__main__":
    asyncio.run(main())
