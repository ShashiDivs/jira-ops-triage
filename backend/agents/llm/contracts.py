"""Pydantic schemas that LLM triage/routing output must satisfy — the "Agent Contracts" +
"Structured Output" hardening pattern. Enforced in agents/llm.py's _chat_json."""
from pydantic import BaseModel, Field


class TriageResult(BaseModel):
    severity: str = Field(pattern=r"^S[1-4]$")
    confidence: float = Field(ge=0.0, le=1.0)
    is_security_related: bool
    reasoning: str


class RoutingResult(BaseModel):
    team: str
    reasoning: str
