import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from agents.integrations.mcp_client import get_mcp_client, shutdown_mcp_client
from agents.runtime import init_graph, shutdown_graph
from api.demo_guard import PasscodeGate
from api.routers import admin, approvals, gate, tickets
from config import get_settings
from rag.retriever import check_corpus_embedding_consistency
from scheduler import init_scheduler, shutdown_scheduler

settings = get_settings()
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_mcp_client()
    await init_graph()
    init_scheduler()
    for warning in check_corpus_embedding_consistency():
        logger.warning("RAG corpus check: %s", warning)
    yield
    shutdown_scheduler()
    await shutdown_graph()
    await shutdown_mcp_client()


app = FastAPI(title="Ops Triage API", lifespan=lifespan)

# Added before CORS so CORS is the outermost layer and a 401 still carries CORS headers.
app.add_middleware(PasscodeGate, passcode=settings.demo_passcode)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tickets.router)
app.include_router(approvals.router)
app.include_router(admin.router)
app.include_router(gate.router)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


# Last, so every /api route above wins: serve the built frontend from the same process. One public URL,
# the browser and the API share an origin, so nothing needs CORS.
if settings.static_dir and Path(settings.static_dir).is_dir():
    app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="web")
