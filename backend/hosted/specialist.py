"""The four specialist agents, as plain functions: payload in, validated result + metadata out.

Each specialist is a pure LLM step. The orchestrator does the retrieval (it owns the database
and MCP tools) and sends the evidence along in the payload, so a specialist needs only model
access — no database, no MCP subprocess, no secrets. That keeps each hosted specialist small and
means only the orchestrator ever needs network access to Postgres.

This module has no web-framework dependency; hosted/server.py wires it to the Invocations
protocol. Keeping them apart makes the logic testable without a server.
"""
from typing import Any

from agents.integrations import specialist_client
from agents.llm import client as llm
from agents.llm.client import _token_usage_var

ROLE_TRIAGE_GENERIC = "triage-generic"
ROLE_TRIAGE_SECURITY = "triage-security"
ROLE_TRIAGE_TRIVIAL = "triage-trivial"
ROLE_ROUTING = "routing"
SPECIALIST_ROLES = (ROLE_TRIAGE_GENERIC, ROLE_TRIAGE_SECURITY, ROLE_TRIAGE_TRIVIAL, ROLE_ROUTING)


class BadRequest(ValueError):
    """The payload is missing something this specialist needs (maps to HTTP 400)."""


def _require(payload: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if k not in payload]
    if missing:
        raise BadRequest(f"payload is missing required field(s): {', '.join(missing)}")


def _generic(p: dict[str, Any]) -> dict:
    _require(p, "ticket", "severity_policy")
    return llm.classify_severity(p["ticket"], p.get("similar_tickets") or [], p["severity_policy"])


def _security(p: dict[str, Any]) -> dict:
    _require(p, "ticket", "security_runbook")
    return llm.classify_severity_security(p["ticket"], p.get("similar_tickets") or [], p["security_runbook"])


def _trivial(p: dict[str, Any]) -> dict:
    _require(p, "ticket")
    return llm.classify_severity_fast(p["ticket"])


def _routing(p: dict[str, Any]) -> dict:
    _require(p, "ticket", "ownership_doc")
    return llm.classify_team(p["ticket"], p["ownership_doc"])


_HANDLERS = {
    ROLE_TRIAGE_GENERIC: _generic,
    ROLE_TRIAGE_SECURITY: _security,
    ROLE_TRIAGE_TRIVIAL: _trivial,
    ROLE_ROUTING: _routing,
}


def run_specialist(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Runs one classification and returns {"result": <contract-validated dict>, "meta": {...}}.
    `meta` carries the model, prompt version, and token counts so the orchestrator can record
    them in agent_runs exactly as if it had made the call itself."""
    handler = _HANDLERS.get(role)
    if handler is None:
        raise BadRequest(f"unknown specialist role '{role}'")

    # Guard against loops: a specialist must never call out to another specialist, even if
    # SPECIALIST_URLS leaked into its environment.
    guard = specialist_client.in_specialist.set(True)
    usage_token = _token_usage_var.set({"tokens_in": 0, "tokens_out": 0, "model": None, "prompt_version": None})
    try:
        result = handler(payload)
        usage = dict(_token_usage_var.get())
    finally:
        _token_usage_var.reset(usage_token)
        specialist_client.in_specialist.reset(guard)

    return {
        "result": result,
        "meta": {
            "role": role,
            "model": usage["model"],
            "prompt_version": usage["prompt_version"],
            "tokens_in": usage["tokens_in"] or None,
            "tokens_out": usage["tokens_out"] or None,
        },
    }
