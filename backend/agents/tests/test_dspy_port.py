"""Verifies the DSPy port (agents/dspy_port.py) reproduces the existing hand-tuned prompts'
behavior on clear-cut cases. Not byte-identical comparison — LLM output isn't deterministic —
but both implementations should land in the same severity/team bucket on cases that aren't
ambiguous. Standalone: does not touch agents/graph.py, does not need a DB or MCP. Requires a
live OpenAI provider (real API calls, same as agents/test_graph.py and friends).
"""
from agents.prompts.dspy_port import (
    classify_severity_dspy,
    classify_severity_fast_dspy,
    classify_severity_security_dspy,
    classify_team_dspy,
    configure_dspy,
)
from agents.llm.client import classify_severity, classify_severity_fast, classify_severity_security, classify_team

_SEVERITY_POLICY = (
    "S1: complete outage or all-customers-affected incident. S2: significant degraded "
    "service. S3: minor issue with a workaround. S4: cosmetic, no functional impact."
)
_SECURITY_RUNBOOK = (
    "Security incidents (unauthorized access, data breach, PII exposure) are almost always "
    "S1 and always require human sign-off before any action."
)
_OWNERSHIP_MAP = (
    "checkout-service -> Platform team\n"
    "admin-panel -> Security team\n"
    "marketing-site -> Growth team"
)


def _assert_contract_shape(result: dict, is_routing: bool = False) -> None:
    if is_routing:
        assert set(result.keys()) >= {"team", "reasoning"}
        assert isinstance(result["team"], str) and result["team"]
    else:
        assert set(result.keys()) >= {"severity", "confidence", "is_security_related", "reasoning"}
        assert result["severity"] in ("S1", "S2", "S3", "S4")
        assert 0.0 <= result["confidence"] <= 1.0
        assert isinstance(result["is_security_related"], bool)


def main() -> None:
    configure_dspy()

    print("--- Case 1: obvious S1 outage (generic triage path) ---")
    ticket = {
        "summary": "Complete outage of checkout-service, all customers affected",
        "description": "checkout-service is returning 500s for every request. No orders are completing.",
        "component": "checkout-service",
    }
    original = classify_severity(ticket, [], _SEVERITY_POLICY)
    ported = classify_severity_dspy(ticket, [], _SEVERITY_POLICY)
    _assert_contract_shape(ported)
    print(f"  original: severity={original['severity']} confidence={original['confidence']:.2f}")
    print(f"  dspy:     severity={ported['severity']} confidence={ported['confidence']:.2f}")
    assert ported["severity"] == "S1", f"expected DSPy to also classify an obvious outage as S1, got {ported['severity']}"
    assert original["severity"] == "S1", "original implementation should also classify this as S1 (sanity check)"
    print("test_generic_triage_port_matches_on_obvious_outage: PASS")

    print("--- Case 2: security-flagged ticket (security triage path) ---")
    ticket = {
        "summary": "Unauthorized access to internal admin panel detected",
        "description": "Repeated logins from an unrecognized IP using a decommissioned account.",
        "component": "admin-panel",
    }
    original = classify_severity_security(ticket, [], _SECURITY_RUNBOOK)
    ported = classify_severity_security_dspy(ticket, [], _SECURITY_RUNBOOK)
    _assert_contract_shape(ported)
    assert ported["is_security_related"] is True, "security path must always report is_security_related=True"
    assert original["is_security_related"] is True
    print(f"  original: severity={original['severity']}  dspy: severity={ported['severity']}")
    print("test_security_triage_port_forces_is_security_related: PASS")

    print("--- Case 3: obvious trivial/cosmetic ticket (fast triage path) ---")
    ticket = {
        "summary": "Footer copyright year is wrong",
        "description": "The marketing site footer still shows last year's copyright year.",
        "component": "marketing-site",
    }
    original = classify_severity_fast(ticket)
    ported = classify_severity_fast_dspy(ticket)
    _assert_contract_shape(ported)
    print(f"  original: severity={original['severity']}  dspy: severity={ported['severity']}")
    assert ported["severity"] == "S4", f"expected DSPy to also classify an obvious cosmetic issue as S4, got {ported['severity']}"
    print("test_trivial_triage_port_matches_on_obvious_case: PASS")

    print("--- Case 4: routing (unambiguous ownership lookup) ---")
    ticket = {"component": "admin-panel"}
    original = classify_team(ticket, _OWNERSHIP_MAP)
    ported = classify_team_dspy(ticket, _OWNERSHIP_MAP)
    _assert_contract_shape(ported, is_routing=True)
    print(f"  original: team={original['team']}  dspy: team={ported['team']}")
    assert "Security" in ported["team"], f"expected DSPy routing to also pick the Security team, got {ported['team']}"
    print("test_routing_port_matches_on_unambiguous_ownership: PASS")


if __name__ == "__main__":
    main()
