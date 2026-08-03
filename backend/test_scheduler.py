"""Smoke test for scheduled ticket generation: verifies a single generated tick actually runs
a sample ticket through the real pipeline (same as test_graph.py's style), and that
pause/resume correctly stops and restarts the APScheduler job without needing to wait out a
real interval. Requires a live DB + LLM provider.
"""
import asyncio

from sqlalchemy import select

import scheduler
from agents.runtime import init_graph, shutdown_graph
from agents.mcp_client import shutdown_mcp_client
from db.models import AgentRun
from db.session import SessionLocal
from mcp_server import jira_store


def _agent_run_count(ticket_id: str) -> int:
    db = SessionLocal()
    try:
        return len(list(db.execute(select(AgentRun).where(AgentRun.ticket_id == ticket_id)).scalars()))
    finally:
        db.close()


async def test_generate_and_submit_creates_a_real_ticket() -> None:
    before_ids = {t["id"] for t in jira_store.list_tickets()}

    await scheduler._generate_and_submit_ticket()

    after = jira_store.list_tickets()
    new_tickets = [t for t in after if t["id"] not in before_ids]
    assert len(new_tickets) == 1, f"expected exactly 1 new ticket, got {len(new_tickets)}"

    ticket = new_tickets[0]
    assert ticket["reporter"] == "scheduler"
    assert ticket["status"] in ("pending", "auto_approved", "approved", "rejected", "blocked")
    assert _agent_run_count(ticket["id"]) > 0, "expected agent_runs rows for the scheduled ticket"
    print(f"test_generate_and_submit_creates_a_real_ticket: PASS (ticket {ticket['id']}, status={ticket['status']})")


def test_pause_and_resume_toggle_job_state() -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler._scheduler = AsyncIOScheduler()
    scheduler._scheduler.add_job(
        scheduler._generate_and_submit_ticket, "interval", seconds=3600, id=scheduler._JOB_ID
    )
    scheduler._scheduler.start()

    status = scheduler.scheduler_status()
    assert status["running"] is True
    assert status["paused"] is False

    scheduler.pause_scheduler()
    assert scheduler.scheduler_status()["paused"] is True

    scheduler.resume_scheduler()
    assert scheduler.scheduler_status()["paused"] is False

    scheduler.shutdown_scheduler()
    assert scheduler.scheduler_status()["running"] is False
    print("test_pause_and_resume_toggle_job_state: PASS")


async def main() -> None:
    await init_graph()
    try:
        await test_generate_and_submit_creates_a_real_ticket()
    finally:
        await shutdown_graph()
        await shutdown_mcp_client()

    test_pause_and_resume_toggle_job_state()


if __name__ == "__main__":
    asyncio.run(main())
