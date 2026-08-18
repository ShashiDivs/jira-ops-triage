"""Input-safety guardrails run before the supervisor/triage agents ever see a ticket — the
"Guardrails before Supervisor" hardening pattern. Scoped to injection/degenerate-input
defense; PII/payment sensitivity is a separate, existing concern handled by the HITL policy
(graph.py's _SENSITIVE_KEYWORDS), not duplicated here.
"""

_INJECTION_MARKERS = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "disregard all prior instructions",
    "you are now",
    "system prompt",
    "new instructions:",
    "jailbreak",
]


def check_guardrails(summary: str, description: str) -> str | None:
    """Returns a block reason if the ticket should be blocked before triage, else None."""
    text = f"{summary} {description}".strip()

    if not text:
        return "Ticket summary and description are both empty."

    lowered = text.lower()
    for marker in _INJECTION_MARKERS:
        if marker in lowered:
            return f"Input contains a potential prompt-injection marker: '{marker}'."

    return None
