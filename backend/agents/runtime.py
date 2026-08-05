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


async def get_pending_interrupt_payload(ticket_id: str) -> dict:
    """The payload passed to hitl_gate_node's interrupt() for a ticket paused mid-execution —
    has severity/hitl_reasons/etc. that snapshot.values doesn't have yet (see
    api/routers/tickets.py's list_pending for the original explanation of why). Returns {} if
    the ticket isn't currently paused at an interrupt (already decided, or doesn't require
    HITL at all)."""
    graph = get_graph()
    config = {"configurable": {"thread_id": ticket_id}}
    snapshot = await graph.aget_state(config)
    if snapshot:
        for task in snapshot.tasks:
            if task.interrupts:
                return task.interrupts[0].value
    return {}
