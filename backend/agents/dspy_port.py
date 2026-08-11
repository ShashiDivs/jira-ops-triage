"""DSPy port of agents/llm.py's four prompt functions (Prompt Engineering / DSPy Phase 1 —
Port). Goal of this phase: reproduce today's hand-tuned prompt behavior as DSPy Signatures,
faithfully, with NO optimization yet — that's a later phase, once there's a labeled eval set
to optimize against. This module is purely additive: it is not imported by agents/graph.py or
agents/llm.py, and changing it cannot affect the live pipeline. See
reference/agentic_engineering_layers.xlsx (Prompt Engineering tab) for the broader context.

Scope note: only the OpenAI path is wired up and verified here, since OpenAI is the currently
active provider (settings.openai_configured). dspy.LM's `azure/...` provider traditionally
expects the classic api_base + api_version shape (via litellm), not the /openai/v1/ endpoint
agents/llm.py's OpenAI-SDK-compatible client uses — that combination is unverified. If Azure
ever becomes primary again, this needs its own check before trusting it.

Also intentionally NOT replicated here: the circuit breaker / retry / keyword-heuristic
fallback behavior in agents/llm.py. This module assumes a working LLM provider — wiring it
into the resilience layer is a later phase's job, once/if this path is promoted from
"parallel proof" to "production."
"""
import dspy

from agents.contracts import RoutingResult, TriageResult
from agents.llm import _format_similar_ticket
from config import get_settings

settings = get_settings()


def configure_dspy() -> None:
    """Idempotent — safe to call multiple times (e.g. once per test run)."""
    if not settings.openai_configured:
        raise RuntimeError(
            "DSPy port only supports the OpenAI path right now (see module docstring) — "
            "OPENAI_API_KEY is not set."
        )
    lm = dspy.LM(f"openai/{settings.openai_chat_model}", api_key=settings.openai_api_key)
    dspy.settings.configure(lm=lm)


# ---------------------------------------------------------------------------
# Signatures — instructions ported verbatim (as docstrings) from agents/llm.py's system
# prompts; inputs ported from the user prompts' structure.
# ---------------------------------------------------------------------------

class TriageGenericSignature(dspy.Signature):
    """You are an incident triage assistant. Classify the ticket's severity as one of S1, S2,
    S3, S4, grounded in the severity policy and similar historical tickets. Each historical
    ticket is tagged with a similarity score (0-1, higher = closer match) — weigh a 0.55+
    match as real evidence, and treat anything below ~0.35 as a weak analogy at best, not
    proof this is the same situation. Let a low top similarity score push your confidence
    down, not just the ticket text itself."""

    severity_policy: str = dspy.InputField()
    similar_tickets: str = dspy.InputField(desc="Formatted list of similar past tickets with similarity scores")
    summary: str = dspy.InputField()
    description: str = dspy.InputField()
    component: str = dspy.InputField()

    severity: str = dspy.OutputField(desc="One of S1, S2, S3, S4")
    confidence: float = dspy.OutputField(desc="0.0 to 1.0")
    is_security_related: bool = dspy.OutputField()
    reasoning: str = dspy.OutputField()


class TriageSecuritySignature(dspy.Signature):
    """You are a security incident triage specialist. This ticket was already flagged as
    security-related by pattern matching. Classify its severity strictly per the security
    runbook and similar past security incidents — most security incidents are S1. Each
    historical ticket is tagged with a similarity score (0-1); weigh it when judging
    confidence, but note this ticket is security-related regardless of that score — human
    sign-off is still required either way."""

    security_runbook: str = dspy.InputField()
    similar_tickets: str = dspy.InputField(desc="Formatted list of similar past security incidents")
    summary: str = dspy.InputField()
    description: str = dspy.InputField()
    component: str = dspy.InputField()

    severity: str = dspy.OutputField(desc="One of S1, S2, S3, S4")
    confidence: float = dspy.OutputField(desc="0.0 to 1.0")
    reasoning: str = dspy.OutputField()


class TriageTrivialSignature(dspy.Signature):
    """Pattern-matching suggests this ticket is cosmetic/trivial (e.g. a typo or copy issue
    with no functional impact). Confirm or correct its severity."""

    summary: str = dspy.InputField()
    description: str = dspy.InputField()

    severity: str = dspy.OutputField(desc="One of S1, S2, S3, S4")
    confidence: float = dspy.OutputField(desc="0.0 to 1.0")
    is_security_related: bool = dspy.OutputField()
    reasoning: str = dspy.OutputField()


class RoutingSignature(dspy.Signature):
    """You are an incident routing assistant. Assign the owning team based on the
    component-ownership runbook."""

    ownership_map: str = dspy.InputField()
    component: str = dspy.InputField()

    team: str = dspy.OutputField()
    reasoning: str = dspy.OutputField()


# ---------------------------------------------------------------------------
# Adapters — same function names/shapes as agents/llm.py's originals (dict in, dict out,
# validated against the same Pydantic contracts), so they're directly comparable in tests
# and swappable later without touching call sites elsewhere.
# ---------------------------------------------------------------------------

_triage_generic = dspy.Predict(TriageGenericSignature)
_triage_security = dspy.Predict(TriageSecuritySignature)
_triage_trivial = dspy.Predict(TriageTrivialSignature)
_routing = dspy.Predict(RoutingSignature)


def classify_severity_dspy(ticket: dict, similar_tickets: list[dict], severity_policy: str) -> dict:
    similar_str = "\n".join(_format_similar_ticket(t) for t in similar_tickets)
    result = _triage_generic(
        severity_policy=severity_policy,
        similar_tickets=similar_str,
        summary=ticket["summary"],
        description=ticket["description"],
        component=ticket.get("component", ""),
    )
    return TriageResult(
        severity=result.severity,
        confidence=result.confidence,
        is_security_related=result.is_security_related,
        reasoning=result.reasoning,
    ).model_dump()


def classify_severity_security_dspy(ticket: dict, similar_tickets: list[dict], security_runbook: str) -> dict:
    similar_str = "\n".join(_format_similar_ticket(t) for t in similar_tickets)
    result = _triage_security(
        security_runbook=security_runbook,
        similar_tickets=similar_str,
        summary=ticket["summary"],
        description=ticket["description"],
        component=ticket.get("component", ""),
    )
    # Same defense-in-depth override as the original: this path must not be able to talk
    # itself out of being security-related, regardless of what the model concludes.
    return TriageResult(
        severity=result.severity,
        confidence=result.confidence,
        is_security_related=True,
        reasoning=result.reasoning,
    ).model_dump()


def classify_severity_fast_dspy(ticket: dict) -> dict:
    result = _triage_trivial(summary=ticket["summary"], description=ticket["description"])
    return TriageResult(
        severity=result.severity,
        confidence=result.confidence,
        is_security_related=result.is_security_related,
        reasoning=result.reasoning,
    ).model_dump()


def classify_team_dspy(ticket: dict, ownership_doc: str) -> dict:
    result = _routing(ownership_map=ownership_doc, component=ticket.get("component", ""))
    return RoutingResult(team=result.team, reasoning=result.reasoning).model_dump()
