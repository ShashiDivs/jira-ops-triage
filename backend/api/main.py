from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agents.mcp_client import get_mcp_client, shutdown_mcp_client
from agents.runtime import init_graph, shutdown_graph
from api.routers import admin, approvals, tickets
from config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_mcp_client()
    await init_graph()
    yield
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
