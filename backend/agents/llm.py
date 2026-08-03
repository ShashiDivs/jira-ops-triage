"""Chat + embeddings via OpenAI or Azure AI Foundry (OpenAI takes priority when configured),
with an offline keyword-heuristic fallback for chat when neither is configured.

Mirrors the reference README's design: when no LLM provider is configured, the pipeline
still runs end-to-end deterministically using keyword heuristics instead of LLM calls.
"""
import contextvars
import json

from openai import APIConnectionError, APITimeoutError, InternalServerError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from agents.contracts import RoutingResult, TriageResult
from agents.resilience import CircuitBreaker, call_with_retry
from config import get_settings

settings = get_settings()

# Accumulates token usage for whichever graph node is currently executing (Observability).
# The traced_node decorator in agents/observability.py sets this at node entry and reads it
# at node exit; None outside of a traced node, so untraced callers (tests, scripts) pay no cost.
_token_usage_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("_token_usage_var", default=None)


def _record_token_usage(usage) -> None:
    acc = _token_usage_var.get()
    if acc is None or usage is None:
        return
    acc["tokens_in"] += getattr(usage, "prompt_tokens", 0) or 0
    acc["tokens_out"] += getattr(usage, "completion_tokens", 0) or 0

# Transient/connectivity errors worth retrying — not schema/contract failures (those have
# their own retry-then-escalate handling in _chat_json) and not e.g. AuthenticationError or
# BadRequestError, which retrying can't fix.
_TRANSIENT_LLM_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

# Trips after 3 consecutive transient failures, degrading chat-completion callers to the
# keyword-heuristic fallback for 30s instead of retrying a dependency that's clearly down.
_llm_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=30.0)


def _v1_base_url(endpoint: str) -> str:
    """Normalize AZURE_AI_FOUNDRY_ENDPOINT to `<resource-root>/openai/v1/`, whether the
    configured value is the bare resource root or already has /openai/v1 appended."""
    url = endpoint.strip().rstrip("/")
    if not url.endswith("/openai/v1"):
        url = f"{url}/openai/v1"
    return f"{url}/"


_client: OpenAI | None = None
_chat_model = settings.azure_chat_deployment
_chat_model_fast = settings.azure_chat_deployment_fast or settings.azure_chat_deployment
_embedding_model = settings.azure_embedding_deployment

if settings.openai_configured:
    # Stopgap while the Azure subscription is disabled — plain OpenAI, no base_url override.
    # Takes priority over Azure whenever OPENAI_API_KEY is set; remove it to switch back.
    _client = OpenAI(api_key=settings.openai_api_key)
    _chat_model = settings.openai_chat_model
    _chat_model_fast = settings.openai_chat_model_fast or settings.openai_chat_model
    _embedding_model = settings.openai_embedding_model
elif settings.azure_configured:
    # Foundry's v1 API (recommended over the dated api-version REST surface): a plain OpenAI
    # client pointed at <resource>/openai/v1/, no api_version needed. AZURE_AI_FOUNDRY_ENDPOINT
    # must be the resource root (e.g. https://<resource>.services.ai.azure.com), not a
    # project endpoint (.../api/projects/<name>) — those are for the azure-ai-projects SDK.
    _client = OpenAI(
        base_url=_v1_base_url(settings.azure_ai_foundry_endpoint),
        api_key=settings.azure_ai_foundry_api_key,
    )


def llm_configured() -> bool:
    return _client is not None


def current_embedding_model() -> str:
    """The embedding model actually in use right now (whichever provider is active) — used to
    tag newly-embedded corpus rows and to detect a stale/mismatched corpus at startup."""
    return _embedding_model


def _llm_available() -> bool:
    """False if no provider is configured, OR the circuit breaker is currently open (the
    provider's been failing) — either way, callers should use the keyword-heuristic fallback."""
    return _client is not None and not _llm_breaker.is_open


def _fallback_reason_prefix() -> str:
    if _client is not None:
        return "Keyword-heuristic fallback (LLM provider is failing, circuit breaker open)"
    return "Keyword-heuristic fallback (no LLM provider configured)"


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str]) -> list[list[float]]:
    if _client is None:
        raise RuntimeError(
            "No LLM provider is configured (set either OPENAI_API_KEY, or "
            "AZURE_AI_FOUNDRY_ENDPOINT / AZURE_AI_FOUNDRY_API_KEY). Embeddings require a "
            "real deployment; there is no offline fallback for RAG indexing."
        )
    try:
        resp = call_with_retry(
            lambda: _client.embeddings.create(model=_embedding_model, input=texts),
            should_retry=lambda e: isinstance(e, _TRANSIENT_LLM_ERRORS),
        )
    except _TRANSIENT_LLM_ERRORS:
        _llm_breaker.record_failure()
        raise
    _llm_breaker.record_success()
    _record_token_usage(resp.usage)
    return [item.embedding for item in resp.data]


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


class LLMContractError(RuntimeError):
    """Raised when the LLM's JSON output still fails schema validation after one retry."""


# ---------------------------------------------------------------------------
# Structured JSON chat completion helper (Agent Contracts / Structured Output)
# ---------------------------------------------------------------------------

def _chat_json(system_prompt: str, user_prompt: str, schema: type[BaseModel], model: str | None = None) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            resp = call_with_retry(
                lambda: _client.chat.completions.create(
                    model=model or _chat_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                ),
                should_retry=lambda e: isinstance(e, _TRANSIENT_LLM_ERRORS),
            )
        except _TRANSIENT_LLM_ERRORS:
            _llm_breaker.record_failure()
            raise
        _llm_breaker.record_success()
        _record_token_usage(resp.usage)
        raw = resp.choices[0].message.content
        try:
            return schema.model_validate_json(raw).model_dump()
        except (ValidationError, ValueError) as e:
            last_error = e
            if attempt == 0:
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            f"That response was invalid: {e}\n"
                            "Return ONLY valid JSON matching the required schema, no other text."
                        ),
                    },
                ]

    raise LLMContractError(f"LLM returned invalid JSON after retry: {last_error}") from last_error


# ---------------------------------------------------------------------------
# Severity triage
# ---------------------------------------------------------------------------

# Not underscore-prefixed: also used by graph.py's supervisor node as the single source of
# truth for the security/trivial routing decision, so that decision and this fallback's
# decision can't drift apart.
SECURITY_KEYWORDS = ["breach", "unauthorized", "exposure", "exposed", "compromise", "exploit", "leak", "hacked"]
COSMETIC_KEYWORDS = ["typo", "copy", "cosmetic", "footer", "spelling"]

_OUTAGE_KEYWORDS = ["outage", "down", "all customers", "complete outage", "500", "502"]
_DEGRADED_KEYWORDS = ["degraded", "partial", "slow", "intermittent", "delay", "delayed", "latency"]
_SENSITIVE_KEYWORDS = ["payment", "billing", "charge", "charged", "pii", "personal data", "phone number", "email address"]


def _format_similar_ticket(t: dict) -> str:
    """Includes the retrieval similarity score so the model (and, via similar_tickets in
    TicketState, the human reviewer) can tell a strong match from a weak analogy instead of
    trusting every retrieved ticket equally."""
    similarity = t.get("similarity")
    score_str = f"similarity={similarity:.2f}" if similarity is not None else "similarity=n/a"
    return f"- [{t['severity']}, {score_str}] {t['summary']} -> resolved by: {t['resolution']}"


def classify_severity(ticket: dict, similar_tickets: list[dict], severity_policy: str) -> dict:
    text = f"{ticket['summary']} {ticket['description']}".lower()

    if _llm_available():
        similar_str = "\n".join(_format_similar_ticket(t) for t in similar_tickets)
        system_prompt = (
            "You are an incident triage assistant. Classify the ticket's severity as one of "
            "S1, S2, S3, S4, grounded in the severity policy and similar historical tickets. "
            "Each historical ticket is tagged with a similarity score (0-1, higher = closer "
            "match) — weigh a 0.55+ match as real evidence, and treat anything below ~0.35 as "
            "a weak analogy at best, not proof this is the same situation. Let a low top "
            "similarity score push your confidence down, not just the ticket text itself. "
            'Respond with strict JSON: {"severity": "S1|S2|S3|S4", "confidence": 0.0-1.0, '
            '"is_security_related": true|false, "reasoning": "..."}'
        )
        user_prompt = (
            f"SEVERITY POLICY:\n{severity_policy}\n\n"
            f"SIMILAR PAST TICKETS:\n{similar_str}\n\n"
            f"NEW TICKET:\nSummary: {ticket['summary']}\nDescription: {ticket['description']}\n"
            f"Component: {ticket.get('component', '')}"
        )
        try:
            return _chat_json(system_prompt, user_prompt, TriageResult)
        except _TRANSIENT_LLM_ERRORS:
            pass  # provider just tripped the breaker — degrade to the heuristic below

    # --- Keyword-heuristic fallback (no LLM provider configured, or it's failing) ---
    is_security = any(kw in text for kw in SECURITY_KEYWORDS)
    if is_security:
        severity, confidence = "S1", 0.9
    elif any(kw in text for kw in _OUTAGE_KEYWORDS):
        severity, confidence = "S1", 0.85
    elif any(kw in text for kw in _DEGRADED_KEYWORDS):
        severity, confidence = "S2", 0.75
    elif any(kw in text for kw in COSMETIC_KEYWORDS):
        severity, confidence = "S4", 0.8
    else:
        severity, confidence = "S3", 0.6

    return {
        "severity": severity,
        "confidence": confidence,
        "is_security_related": is_security,
        "reasoning": f"{_fallback_reason_prefix()}: matched pattern for severity {severity}.",
    }


def classify_severity_security(ticket: dict, similar_tickets: list[dict], security_runbook: str) -> dict:
    """Specialized triage for tickets the supervisor has already pattern-matched as
    security-related — grounded specifically in the security runbook, and always returns
    is_security_related=True regardless of what the model concludes (defense in depth: the
    HITL policy requires human sign-off on anything security-related, so this path must not
    be able to talk itself out of that)."""
    if _llm_available():
        similar_str = "\n".join(_format_similar_ticket(t) for t in similar_tickets)
        system_prompt = (
            "You are a security incident triage specialist. This ticket was already flagged as "
            "security-related by pattern matching. Classify its severity strictly per the security "
            "runbook and similar past security incidents — most security incidents are S1. Each "
            "historical ticket is tagged with a similarity score (0-1); weigh it when judging "
            "confidence, but note this ticket is security-related regardless of that score — "
            "human sign-off is still required either way. "
            'Respond with strict JSON: {"severity": "S1|S2|S3|S4", "confidence": 0.0-1.0, '
            '"is_security_related": true, "reasoning": "..."}'
        )
        user_prompt = (
            f"SECURITY RUNBOOK:\n{security_runbook}\n\n"
            f"SIMILAR PAST SECURITY INCIDENTS:\n{similar_str}\n\n"
            f"NEW TICKET:\nSummary: {ticket['summary']}\nDescription: {ticket['description']}\n"
            f"Component: {ticket.get('component', '')}"
        )
        try:
            result = _chat_json(system_prompt, user_prompt, TriageResult)
            result["is_security_related"] = True
            return result
        except _TRANSIENT_LLM_ERRORS:
            pass

    return {
        "severity": "S1",
        "confidence": 0.9,
        "is_security_related": True,
        "reasoning": f"{_fallback_reason_prefix()}: supervisor pattern-matched this as security-related.",
    }


def classify_severity_fast(ticket: dict) -> dict:
    """Lightweight triage for tickets the supervisor has pattern-matched as trivial/cosmetic —
    no RAG grounding (skips the similar-tickets/runbook lookups), cheapest path. Still model-
    backed so an obviously-wrong fast-path guess can be corrected rather than rubber-stamped."""
    if _llm_available():
        system_prompt = (
            "Pattern-matching suggests this ticket is cosmetic/trivial (e.g. a typo or copy issue "
            "with no functional impact). Confirm or correct its severity. "
            'Respond with strict JSON: {"severity": "S1|S2|S3|S4", "confidence": 0.0-1.0, '
            '"is_security_related": true|false, "reasoning": "..."}'
        )
        user_prompt = f"Summary: {ticket['summary']}\nDescription: {ticket['description']}"
        try:
            return _chat_json(system_prompt, user_prompt, TriageResult, model=_chat_model_fast)
        except _TRANSIENT_LLM_ERRORS:
            pass

    return {
        "severity": "S4",
        "confidence": 0.85,
        "is_security_related": False,
        "reasoning": f"{_fallback_reason_prefix()}: supervisor pattern-matched this as trivial/cosmetic.",
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

_OWNERSHIP_FALLBACK = {
    "checkout-service": "Platform",
    "order-service": "Platform",
    "payments-service": "Platform",
    "billing-service": "Platform",
    "search-service": "Search",
    "recommendations-service": "Search",
    "marketing-site": "Growth",
    "profile-service": "Growth",
    "notifications-service": "Growth",
    "admin-panel": "Security",
    "auth-service": "Security",
    "support-dashboard": "Security",
}


def classify_team(ticket: dict, ownership_doc: str) -> dict:
    component = ticket.get("component", "")

    if _llm_available():
        system_prompt = (
            "You are an incident routing assistant. Assign the owning team based on the "
            'component-ownership runbook. Respond with strict JSON: {"team": "...", "reasoning": "..."}'
        )
        user_prompt = f"OWNERSHIP MAP:\n{ownership_doc}\n\nTICKET COMPONENT: {component}"
        try:
            return _chat_json(system_prompt, user_prompt, RoutingResult)
        except _TRANSIENT_LLM_ERRORS:
            pass

    team = _OWNERSHIP_FALLBACK.get(component, "Platform")
    return {
        "team": team,
        "reasoning": f"{_fallback_reason_prefix()}: component '{component}' mapped to {team} via static ownership table.",
    }
