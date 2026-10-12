"""Client the orchestrator uses to call a specialist agent running as its own (hosted) service.

When SPECIALIST_URLS maps a role to a URL, agents/llm/client.py's classify_* functions send the
classification there instead of calling the model in-process. The specialist is a separate
agent — in Foundry it shows up as its own hosted agent with its own identity and traces.

Failure containment (same pattern as the LLM and MCP clients): if a specialist is down, slow,
or returns garbage, this returns None and the caller falls back to the in-process path, so a
dead specialist degrades a ticket instead of failing it. A circuit breaker stops us from paying
the timeout on every ticket while a specialist is down.
"""
import contextvars
import logging
from typing import Any

import httpx

from agents.safety.resilience import CircuitBreaker, call_with_retry
from config import get_settings

settings = get_settings()
logger = logging.getLogger("specialist_client")

# Set by hosted/specialist.py while a specialist is serving a request, so a specialist can
# never call another specialist (or itself) even if SPECIALIST_URLS leaks into its environment.
in_specialist: contextvars.ContextVar[bool] = contextvars.ContextVar("in_specialist", default=False)

_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=30.0)


class _TransientSpecialistError(RuntimeError):
    """Worth retrying: transport failure, timeout, 429, or 5xx."""


def _auth_headers() -> dict[str, str]:
    if settings.specialist_auth != "entra":
        return {}
    # Reuse the same keyless token path the LLM client uses (lazy import: avoids an import
    # cycle with agents.llm.client, which imports this module).
    from agents.llm.client import _entra_token_provider

    return {"Authorization": f"Bearer {_entra_token_provider()()}"}


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = httpx.post(
            url,
            json=payload,
            headers=_auth_headers(),
            timeout=settings.specialist_timeout_seconds,
        )
    except httpx.TransportError as e:  # connect errors, timeouts, dropped connections
        raise _TransientSpecialistError(f"{type(e).__name__}: {e}") from e
    if resp.status_code == 429 or resp.status_code >= 500:
        raise _TransientSpecialistError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        # A 4xx means the request itself was wrong (a bug on our side) — retrying can't fix it.
        raise ValueError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def call_specialist(role: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Returns (result, meta) from the specialist for `role`, or None if the caller should use
    the in-process path (no specialist configured, we ARE a specialist, breaker open, or the
    call failed)."""
    url = settings.specialist_url_map.get(role)
    if url is None or in_specialist.get():
        return None
    if _breaker.is_open:
        return None
    try:
        body = call_with_retry(
            lambda: _post(url, payload),
            max_attempts=2,
            base_delay=0.5,
            should_retry=lambda e: isinstance(e, _TransientSpecialistError),
        )
    except _TransientSpecialistError as e:
        _breaker.record_failure()
        logger.warning("specialist %s unavailable, falling back to in-process: %s", role, e)
        return None
    except Exception as e:  # non-transient: bad request, undecodable body, auth failure
        logger.warning("specialist %s call failed, falling back to in-process: %s", role, e)
        return None

    result, meta = body.get("result"), body.get("meta") or {}
    if not isinstance(result, dict):
        logger.warning("specialist %s returned no result object, falling back to in-process", role)
        return None
    _breaker.record_success()
    return result, meta
