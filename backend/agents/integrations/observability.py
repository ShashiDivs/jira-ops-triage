"""Per-node execution tracing (Observability): wraps each LangGraph node with structured
logging and a persisted row in `agent_runs` — node name, ticket_id as the trace id, latency,
and (where applicable) token usage from agents.llm.client's per-node accumulator.
"""
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable

from langgraph.errors import GraphBubbleUp

from agents.llm.client import _token_usage_var
from agents.state import TicketState

logger = logging.getLogger("agent_trace")


def _persist_run(record: dict) -> None:
    # Imported lazily so a module import cycle (db -> config -> ... ) can't bite at import
    # time, and so tests that never touch the DB don't need it importable either.
    from db.models import AgentRun
    from db.session import SessionLocal

    db = SessionLocal()
    try:
        db.add(
            AgentRun(
                ticket_id=record["ticket_id"],
                node_name=record["node_name"],
                duration_ms=record["duration_ms"],
                tokens_in=record["tokens_in"],
                tokens_out=record["tokens_out"],
                error=record["error"],
                model=record["model"],
                prompt_version=record["prompt_version"],
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        # Observability must never break the actual pipeline — log and move on.
        logger.exception("failed to persist agent_run record for %s/%s", record["ticket_id"], record["node_name"])
    finally:
        db.close()


def traced_node(
    node_name: str,
) -> Callable[[Callable[[TicketState], Awaitable[TicketState]]], Callable[[TicketState], Awaitable[TicketState]]]:
    def decorator(fn: Callable[[TicketState], Awaitable[TicketState]]) -> Callable[[TicketState], Awaitable[TicketState]]:
        @functools.wraps(fn)
        async def wrapper(state: TicketState) -> TicketState:
            ticket_id = state.get("ticket_id", "?")
            token = _token_usage_var.set({"tokens_in": 0, "tokens_out": 0, "model": None, "prompt_version": None})
            start = time.monotonic()
            error: str | None = None
            try:
                return await fn(state)
            except GraphBubbleUp:
                # LangGraph's own control flow (interrupt()/etc), not a real failure — let it
                # propagate untouched, just don't mislabel it as an error below.
                raise
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                raise
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                usage = _token_usage_var.get()
                _token_usage_var.reset(token)
                record = {
                    "ticket_id": ticket_id,
                    "node_name": node_name,
                    "duration_ms": round(duration_ms, 2),
                    "tokens_in": usage["tokens_in"] or None,
                    "tokens_out": usage["tokens_out"] or None,
                    "error": error,
                    "model": usage["model"],
                    "prompt_version": usage["prompt_version"],
                }
                logger.info(json.dumps(record))
                _persist_run(record)

        return wrapper

    return decorator
