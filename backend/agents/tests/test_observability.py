"""Smoke test for Observability (Phase 6): run a ticket through the full pipeline, then query
agent_runs and confirm one row per node visited, with populated latency and (for the
LLM-calling nodes) token usage. Requires a live DB + LLM provider, same as test_graph.py.
"""
import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy import select

from agents.graph import build_graph
from agents.integrations.mcp_client import shutdown_mcp_client
from config import get_settings
from db.models import AgentRun
from db.session import SessionLocal
from mcp_server import jira_store

settings = get_settings()


def _runs_for(ticket_id: str) -> list[AgentRun]:
    db = SessionLocal()
    try:
        return list(db.execute(select(AgentRun).where(AgentRun.ticket_id == ticket_id)).scalars())
    finally:
        db.close()


async def main() -> None:
    async with AsyncPostgresSaver.from_conn_string(settings.psycopg_dsn) as checkpointer:
        await checkpointer.setup()
        graph = build_graph().compile(checkpointer=checkpointer)

        # A generic S1 ticket: expected path is intake -> guardrail -> supervisor -> triage
        # -> routing -> hitl_gate (pauses) -> [resume] -> hitl_gate (again) -> action.
        summary = "Complete outage of order-service, all customers affected"
        description = "order-service is returning 500s for every request."
        ticket = jira_store.create_ticket(summary=summary, description=description, component="order-service", status="pending")
        ticket_id = ticket["id"]
        config = {"configurable": {"thread_id": ticket_id}}

        result = await graph.ainvoke(
            {
                "ticket_id": ticket_id,
                "summary": summary,
                "description": description,
                "component": "order-service",
                "reporter": "smoke-test",
            },
            config=config,
        )
        assert result.get("__interrupt__"), "expected this S1 ticket to pause for HITL"
        await graph.ainvoke(Command(resume={"decision": "approved", "notes": "smoke test"}), config=config)

        runs = _runs_for(ticket_id)
        by_node = {r.node_name: r for r in runs}
        node_names = [r.node_name for r in runs]

        expected_nodes = {"intake", "guardrail", "supervisor", "triage", "routing", "hitl_gate", "action"}
        missing = expected_nodes - set(node_names)
        assert not missing, f"missing agent_runs rows for: {missing} (got: {node_names})"

        # hitl_gate runs twice: once to the interrupt, once on resume (LangGraph re-executes
        # the node from the start on resume).
        assert node_names.count("hitl_gate") == 2, f"expected 2 hitl_gate rows, got {node_names.count('hitl_gate')}"

        for run in runs:
            assert run.duration_ms is not None and run.duration_ms >= 0, f"{run.node_name} has no latency recorded"
            assert run.error is None, f"{run.node_name} unexpectedly recorded an error: {run.error}"

        # triage and routing both make a real chat-completion call, so should have real token
        # counts (proves agents/llm.py's per-node accumulator is actually wired up).
        assert by_node["triage"].tokens_in, "triage node should have recorded token usage"
        assert by_node["routing"].tokens_in, "routing node should have recorded token usage"
        # guardrail/intake/hitl_gate don't call the LLM at all.
        assert by_node["guardrail"].tokens_in is None

        # Traceability: LLM-calling nodes record the model + prompt version that produced their
        # output; non-LLM nodes leave both NULL.
        assert by_node["triage"].model and by_node["triage"].prompt_version, "triage should record model + prompt_version"
        assert by_node["routing"].model and by_node["routing"].prompt_version, "routing should record model + prompt_version"
        assert by_node["guardrail"].model is None and by_node["guardrail"].prompt_version is None

        print(f"agent_runs for {ticket_id}: {len(runs)} rows across {len(set(node_names))} distinct nodes")
        for run in sorted(runs, key=lambda r: r.started_at):
            print(f"  {run.node_name:16s} {run.duration_ms:8.2f}ms  tokens_in={run.tokens_in} tokens_out={run.tokens_out} model={run.model} prompt={run.prompt_version}")

    await shutdown_mcp_client()
    print("test_observability: PASS")


if __name__ == "__main__":
    asyncio.run(main())
