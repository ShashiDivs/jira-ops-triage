from fastapi import APIRouter
from sqlalchemy import text

from db.session import engine
from mcp_server import jira_store
from scheduler import pause_scheduler, resume_scheduler, scheduler_status

router = APIRouter(prefix="/api", tags=["admin"])


@router.post("/reset")
async def reset_demo_state() -> dict:
    jira_store.reset_all()
    with engine.begin() as conn:
        for table in ("checkpoint_writes", "checkpoints", "checkpoint_blobs"):
            conn.execute(text(f"TRUNCATE TABLE {table}"))
    return {"status": "ok"}


@router.get("/scheduler/status")
async def get_scheduler_status() -> dict:
    return scheduler_status()


@router.post("/scheduler/pause")
async def pause_ticket_scheduler() -> dict:
    pause_scheduler()
    return scheduler_status()


@router.post("/scheduler/resume")
async def resume_ticket_scheduler() -> dict:
    resume_scheduler()
    return scheduler_status()
