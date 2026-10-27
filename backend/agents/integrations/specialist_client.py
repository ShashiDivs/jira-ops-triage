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


_token_provider = None


def _auth_headers() -> dict[str, str]:
    if settings.specialist_auth != "entra":
        return {}
    # Built ONCE and reused: the provider caches its token, but a new provider per call re-acquires one
    # every time (measured ~1.3 s each against a live Foundry project).
    global _token_provider
    if _token_provider is None:
        # Same keyless token path the LLM client uses (lazy import: avoids an import cycle with
        # agents.llm.client, which imports this module).
        from agents.llm.client import _entra_token_provider

        _token_provider = _entra_token_provider()
    return {"Authorization": f"Bearer {_token_provider()}"}


# Hosted agents run each session in its own container. A request WITHOUT a session id starts a brand-new
# session — a cold start of ~6-8 s measured against live Foundry — while a request that carries the id
# from an earlier response reuses the warm container (~1-4 s). The platform returns the id in the
# `x-agent-session-id` response header and accepts it back as the `agent_session_id` query parameter.
_SESSION_HEADER = "x-agent-session-id"
_SESSION_PARAM = "agent_session_id"
_sessions: dict[str, str] = {}  # specialist url -> session id


def _send(url: str, payload: dict[str, Any], session_id: str | None) -> httpx.Response:
    target = str(httpx.URL(url).copy_merge_params({_SESSION_PARAM: session_id})) if session_id else url
    try:
        return httpx.post(
            target,
            json=payload,
            headers=_auth_headers(),
            timeout=settings.specialist_timeout_seconds,
        )
    except httpx.TransportError as e:  # connect errors, timeouts, dropped connections
        raise _TransientSpecialistError(f"{type(e).__name__}: {e}") from e


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    session_id = _sessions.get(url)
    resp = _send(url, payload, session_id)
    if session_id and 400 <= resp.status_code < 500 and resp.status_code != 429:
        # The platform no longer accepts our session id (expired, deleted): forget it and start a fresh
        # session once. A genuinely bad request fails again below, and is reported as such.
        _sessions.pop(url, None)
        resp = _send(url, payload, None)
    new_session = resp.headers.get(_SESSION_HEADER)
    if new_session and resp.status_code < 400:
        _sessions[url] = new_session
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
