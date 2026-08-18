"""Versioned instruction text for the routing/ownership path (agents/llm/client.py's
classify_team). See triage_generic.py for the versioning convention."""

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = (
    "You are an incident routing assistant. Assign the owning team based on the "
    'component-ownership runbook. Respond with strict JSON: {"team": "...", "reasoning": "..."}'
)
