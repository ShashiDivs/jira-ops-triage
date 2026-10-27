"""Entry point for running ONE agent as a Foundry-style hosted agent: `python -m hosted.server`.

Which agent this process is comes from OPS_ROLE:
  - a specialist ("triage-generic", "triage-security", "triage-trivial", "routing"): a pure LLM
    step behind POST /invocations — see hosted/specialist.py.
  - the "orchestrator": the LangGraph pipeline plus the approval workflow, behind the same
    POST /invocations with an {"action": ...} envelope. It reuses the existing API router
    functions, so approval rules and RBAC are enforced by exactly the same code as the REST API.

Serves the Invocations protocol (POST /invocations, GET /readiness) on $PORT or 8088, which is
what the Foundry hosted-agent platform expects of a container. The same process runs locally,
unchanged, which is how we test the whole topology before deploying anything.
"""
import asyncio
import logging
from typing import Any

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import get_settings
from hosted.specialist import SPECIALIST_ROLES, BadRequest, run_specialist

settings = get_settings()
logger = logging.getLogger("hosted")


async def _json_body(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "request body must be valid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    return body


def _build_specialist_app(role: str) -> InvocationAgentServerHost:
    from agents.llm.client import LLMContractError

    app = InvocationAgentServerHost()

    @app.invoke_handler
    async def handle(request: Request) -> JSONResponse:
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            # The classify_* functions are synchronous (blocking SDK calls) — run them off the
            # event loop so one slow model call doesn't stall readiness checks or other requests.
            return JSONResponse(await asyncio.to_thread(run_specialist, role, body))
        except BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except LLMContractError as e:
            return JSONResponse({"error": f"model output violated the contract: {e}"}, status_code=502)
        except Exception:
            logger.exception("specialist %s failed", role)
            return JSONResponse({"error": "specialist failed"}, status_code=500)

    return app


# --- orchestrator ---

_init_lock = asyncio.Lock()
_ready = False


async def _ensure_ready() -> None:
    """The host owns the app lifespan, so initialize the graph + MCP client lazily on the first
    request instead (guarded: concurrent first requests initialize once)."""
    global _ready
    async with _init_lock:
        if not _ready:
            from agents.integrations.mcp_client import get_mcp_client
            from agents.runtime import init_graph

            await get_mcp_client()
            await init_graph()
            _ready = True


async def _dispatch(body: dict[str, Any]) -> Any:
    from api.routers import approvals, tickets
    from api.schemas import ApprovalRequest, TicketSubmitRequest

    action = body.get("action")
    if action == "submit":
        return (await tickets.submit_ticket(TicketSubmitRequest(**body.get("ticket", {})))).model_dump()
    if action == "pending":
        return [t.model_dump() for t in await tickets.list_pending()]
    if action == "list":
        return [t.model_dump() for t in await tickets.list_tickets(body.get("status"))]
    if action == "get":
        return (await tickets.get_ticket(body["ticket_id"])).model_dump()
    if action in ("approve", "reject"):
        payload = ApprovalRequest(approver=body.get("approver", ""), notes=body.get("notes", ""))
        fn = approvals.approve_ticket if action == "approve" else approvals.reject_ticket
        return (await fn(body["ticket_id"], payload)).model_dump()
    raise BadRequest(f"unknown action '{action}' (expected submit | pending | list | get | approve | reject)")


def _build_orchestrator_app() -> InvocationAgentServerHost:
    app = InvocationAgentServerHost()

    @app.invoke_handler
    async def handle(request: Request) -> JSONResponse:
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            await _ensure_ready()
            return JSONResponse({"result": await _dispatch(body)})
        except BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except (ValidationError, KeyError) as e:
            return JSONResponse({"error": f"invalid request: {e}"}, status_code=400)
        except HTTPException as e:  # 403 from RBAC, 404, 409 — same statuses as the REST API
            return JSONResponse({"error": e.detail}, status_code=e.status_code)
        except Exception:
            logger.exception("orchestrator action failed")
            return JSONResponse({"error": "orchestrator failed"}, status_code=500)

    return app


def build_app(role: str) -> InvocationAgentServerHost:
    if role == "orchestrator":
        return _build_orchestrator_app()
    if role in SPECIALIST_ROLES:
        return _build_specialist_app(role)
    raise SystemExit(f"unknown OPS_ROLE '{role}' (expected orchestrator or one of {', '.join(SPECIALIST_ROLES)})")


def main() -> None:
    role = settings.ops_role
    logger.info("starting hosted agent: %s", role)
    build_app(role).run()


if __name__ == "__main__":
    main()
