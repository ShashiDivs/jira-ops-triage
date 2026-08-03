import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agents.mcp_client import get_mcp_client, shutdown_mcp_client
from agents.runtime import init_graph, shutdown_graph
from api.routers import admin, approvals, tickets
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tickets.router)
app.include_router(approvals.router)
app.include_router(admin.router)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}
