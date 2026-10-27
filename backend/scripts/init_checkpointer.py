"""Create the LangGraph checkpoint tables once, with whatever identity DATABASE_URL points at.

The hosted orchestrator runs with CHECKPOINTER_AUTO_SETUP=false and a database role that can read and
write data but not create tables, so this runs ahead of time as the database admin (hosted.deploy's
migrate_and_seed step does it). Safe to re-run: the checkpointer's setup() is idempotent.
"""
import asyncio

from agents.runtime import init_graph, shutdown_graph


async def main() -> None:
    await init_graph()  # runs checkpointer.setup() unless CHECKPOINTER_AUTO_SETUP=false
    await shutdown_graph()
    print("checkpoint tables are in place")


if __name__ == "__main__":
    asyncio.run(main())
