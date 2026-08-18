"""Versioned instruction text for the trivial/fast triage path (agents/llm/client.py's
classify_severity_fast). See triage_generic.py for the versioning convention."""

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = (
    "Pattern-matching suggests this ticket is cosmetic/trivial (e.g. a typo or copy issue "
    "with no functional impact). Confirm or correct its severity. "
    'Respond with strict JSON: {"severity": "S1|S2|S3|S4", "confidence": 0.0-1.0, '
    '"is_security_related": true|false, "reasoning": "..."}'
)
