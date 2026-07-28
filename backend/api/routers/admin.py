from fastapi import APIRouter
from sqlalchemy import text

from db.session import engine
from mcp_server import jira_store

router = APIRouter(prefix="/api", tags=["admin"])


@router.post("/reset")
async def reset_demo_state() -> dict:
    jira_store.reset_all()
    with engine.begin() as conn:
        for table in ("checkpoint_writes", "checkpoints", "checkpoint_blobs"):
            conn.execute(text(f"TRUNCATE TABLE {table}"))
    return {"status": "ok"}
