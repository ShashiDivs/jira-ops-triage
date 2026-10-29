"""Owns the compiled LangGraph pipeline and its Postgres checkpointer lifecycle, so pending
HITL interrupts survive a backend restart (unlike the reference project's MemorySaver)."""
import contextlib

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agents.graph import build_graph
from config import get_settings

settings = get_settings()

_exit_stack = contextlib.AsyncExitStack()
_compiled_graph = None


CHECKPOINTER_APP_NAME = "ops-checkpointer"  # lets a test (or an operator) find exactly these connections in Postgres


def _checkpointer_connection_kwargs() -> dict:
    from psycopg.rows import dict_row

    return {
        # The same three AsyncPostgresSaver.from_conn_string uses for its own single connection.
        "autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
        "application_name": CHECKPOINTER_APP_NAME,
        # TCP keepalives: a little traffic on an idle connection stops a proxy between us and Postgres from
        # closing it as idle. (Azure Container Apps' internal TCP ingress did, about six minutes after start.)
        "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
    }


async def _open_checkpointer() -> AsyncPostgresSaver:
    """A POOL, not the single connection AsyncPostgresSaver.from_conn_string would open. One long-lived connection
    has no way to recover once something between us and Postgres drops it: the first live demo returned HTTP 500
    ("server closed the connection unexpectedly") on the first ticket after a few idle minutes. The pool checks each
    connection as it is handed out and quietly replaces a dead one, so a dropped connection costs nothing."""
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    connection_class = AsyncConnection
    if settings.database_auth_mode == "entra":
        # Keyless: every connection the pool opens presents a fresh Entra token (db/entra.py).
        from db.entra import EntraAsyncConnection

        connection_class = EntraAsyncConnection

    pool = AsyncConnectionPool(
        settings.psycopg_dsn,
        connection_class=connection_class,
        kwargs=_checkpointer_connection_kwargs(),
        min_size=1,
        max_size=4,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open(wait=True)
    _exit_stack.push_async_callback(pool.close)
    return AsyncPostgresSaver(pool)


async def init_graph():
    global _compiled_graph
    checkpointer = await _open_checkpointer()
    if settings.checkpointer_auto_setup:
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
