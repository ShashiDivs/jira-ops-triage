"""Unit test for Model Routing (Phase 4): the trivial fast-path uses the configured cheap
model, the security path uses the configured security model, generic triage uses the main
model, and the fast/security tiers default back to the main model when not configured. No network, no DB — same fake-client pattern
as test_contracts.py.
"""
import agents.llm.client as llm
from agents.llm.contracts import TriageResult


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = None


class _FakeCompletions:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(self._responses.pop(0))


class _FakeChat:
    def __init__(self, responses: list[str]) -> None:
        self.completions = _FakeCompletions(responses)


class _FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.chat = _FakeChat(responses)


def _use(main: str, fast: str, security: str) -> _FakeClient:
    fake = _FakeClient([VALID_JSON, VALID_JSON])
    llm._client = fake
    llm._chat_model, llm._chat_model_fast, llm._chat_model_security = main, fast, security
    return fake


VALID_JSON = '{"severity": "S4", "confidence": 0.9, "is_security_related": false, "reasoning": "ok"}'


def test_fast_path_uses_fast_model_when_configured() -> None:
    fake = _FakeClient([VALID_JSON])
    llm._client = fake
    llm._chat_model = "capable-model"
    llm._chat_model_fast = "cheap-model"

    llm.classify_severity_fast({"summary": "typo", "description": "footer typo", "component": ""})

    assert fake.chat.completions.calls[0]["model"] == "cheap-model"
    print("test_fast_path_uses_fast_model_when_configured: PASS")


def test_fast_path_defaults_to_main_model_when_unconfigured() -> None:
    fake = _FakeClient([VALID_JSON])
    llm._client = fake
    llm._chat_model = "capable-model"
    llm._chat_model_fast = "capable-model"  # simulates *_CHAT_MODEL_FAST left unset

    llm.classify_severity_fast({"summary": "typo", "description": "footer typo", "component": ""})

    assert fake.chat.completions.calls[0]["model"] == "capable-model"
    print("test_fast_path_defaults_to_main_model_when_unconfigured: PASS")


def test_generic_path_uses_main_and_security_path_uses_security_model() -> None:
    fake = _use(main="capable-model", fast="cheap-model", security="strong-model")

    llm.classify_severity({"summary": "outage", "description": "down", "component": ""}, [], "policy")
    llm.classify_severity_security({"summary": "breach", "description": "unauthorized", "component": ""}, [], "runbook")

    assert fake.chat.completions.calls[0]["model"] == "capable-model", "generic path should use the main model"
    assert fake.chat.completions.calls[1]["model"] == "strong-model", "security path should use the security model"
    print("test_generic_path_uses_main_and_security_path_uses_security_model: PASS")


def test_security_path_defaults_to_main_model_when_unconfigured() -> None:
    fake = _use(main="capable-model", fast="cheap-model", security="capable-model")  # *_SECURITY left unset

    llm.classify_severity_security({"summary": "breach", "description": "unauthorized", "component": ""}, [], "runbook")

    assert fake.chat.completions.calls[0]["model"] == "capable-model"
    print("test_security_path_defaults_to_main_model_when_unconfigured: PASS")


def test_routing_uses_main_model() -> None:
    fake = _FakeClient(['{"team": "Platform", "reasoning": "ok"}'])
    llm._client = fake
    llm._chat_model, llm._chat_model_fast, llm._chat_model_security = "capable-model", "cheap-model", "strong-model"

    llm.classify_team({"component": "checkout-service"}, "map")

    assert fake.chat.completions.calls[0]["model"] == "capable-model", "routing should use the main model"
    print("test_routing_uses_main_model: PASS")


if __name__ == "__main__":
    test_fast_path_uses_fast_model_when_configured()
    test_fast_path_defaults_to_main_model_when_unconfigured()
    test_generic_path_uses_main_and_security_path_uses_security_model()
    test_security_path_defaults_to_main_model_when_unconfigured()
    test_routing_uses_main_model()
