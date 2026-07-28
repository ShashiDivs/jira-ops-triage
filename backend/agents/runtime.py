"""Owns the compiled LangGraph pipeline and its Postgres checkpointer lifecycle, so pending
HITL interrupts survive a backend restart (unlike the reference project's MemorySaver)."""
import contextlib

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agents.graph import build_graph
from config import get_settings

settings = get_settings()

_exit_stack = contextlib.AsyncExitStack()
_compiled_graph = None


async def init_graph():
    global _compiled_graph
    checkpointer = await _exit_stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(settings.psycopg_dsn)
    )
    await checkpointer.setup()
    _compiled_graph = build_graph().compile(checkpointer=checkpointer)
    return _compiled_graph


async def shutdown_graph() -> None:
    global _compiled_graph
    await _exit_stack.aclose()
    _compiled_graph = None


def get_graph():
    if _compiled_graph is None:
        raise RuntimeError("Graph not initialized — call init_graph() during app startup")
    return _compiled_graph
