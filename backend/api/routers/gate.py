from fastapi import APIRouter

from config import get_settings

router = APIRouter(prefix="/api", tags=["gate"])


@router.get("/gate")
async def gate_status() -> dict:
    """Open to everyone: tells the browser whether to ask for a passcode before showing anything."""
    return {"required": bool(get_settings().demo_passcode)}


@router.get("/session")
async def check_session() -> dict:
    """Behind the passcode gate: a 200 here means the passcode the browser sent is the right one."""
    return {"status": "ok"}
