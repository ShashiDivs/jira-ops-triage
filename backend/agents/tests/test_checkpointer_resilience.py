"""The pipeline's saved-state layer must survive its database connection being dropped.

The first public demo returned HTTP 500 ("server closed the connection unexpectedly") on the first ticket after a
few idle minutes: Azure's internal TCP proxy had closed the single long-lived connection the LangGraph checkpointer
held, and nothing could replace it. This test reproduces that without Azure by killing the checkpointer's connections
on the Postgres side, then using it again. Needs the local Postgres (docker compose up -d postgres); no model calls.
"""
import asyncio

from sqlalchemy import text

from agents import runtime
from db.session import engine

_CONFIG = {"configurable": {"thread_id": "checkpointer-resilience-test", "checkpoint_ns": ""}}


def _kill_checkpointer_connections() -> int:
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = :n AND pid <> pg_backend_pid()"), {"n": runtime.CHECKPOINTER_APP_NAME}).all()
    return len(rows)


async def _survives_dropped_connections() -> None:
    graph = await runtime.init_graph()
    try:
        saver = graph.checkpointer
        await saver.aget_tuple(_CONFIG)  # works while the connection is healthy
        killed = _kill_checkpointer_connections()
        assert killed >= 1, "the test must actually drop a connection, or it proves nothing"
        await saver.aget_tuple(_CONFIG)  # the dropped connection must be replaced, not raise
        await saver.aget_tuple(_CONFIG)
        print(f"  dropped {killed} checkpointer connection(s); the next calls still worked")
    finally:
        await runtime.shutdown_graph()


def test_checkpointer_survives_dropped_connections() -> None:
    asyncio.run(_survives_dropped_connections())
    print("test_checkpointer_survives_dropped_connections: PASS")


def test_checkpointer_connections_send_keepalives() -> None:
    kwargs = runtime._checkpointer_connection_kwargs()
    assert kwargs["keepalives"] == 1 and kwargs["keepalives_idle"] <= 60, "idle connections must not look idle to a proxy"
    assert kwargs["application_name"] == runtime.CHECKPOINTER_APP_NAME
    print("test_checkpointer_connections_send_keepalives: PASS")


if __name__ == "__main__":
    test_checkpointer_connections_send_keepalives()
    test_checkpointer_survives_dropped_connections()
    print("Checkpointer resilience tests passed.")
