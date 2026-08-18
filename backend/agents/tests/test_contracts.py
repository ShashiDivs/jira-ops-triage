"""Unit test for the Agent Contracts / Structured Output hardening (Phase 1): verifies
_chat_json retries once on invalid JSON and raises LLMContractError if it's still invalid
after the retry. No network, no DB — a fake OpenAI-shaped client stands in for the real one.
"""
import agents.llm.client as llm
from agents.llm.contracts import TriageResult
from agents.llm.client import LLMContractError


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


VALID_TRIAGE_JSON = (
    '{"severity": "S1", "confidence": 0.9, "is_security_related": false, "reasoning": "ok"}'
)


def test_retries_once_then_succeeds() -> None:
    fake = _FakeClient(["not valid json at all", VALID_TRIAGE_JSON])
    llm._client, llm._chat_model = fake, "fake-model"

    result = llm._chat_json("system", "user", TriageResult)

    assert result["severity"] == "S1"
    assert result["confidence"] == 0.9
    assert len(fake.chat.completions.calls) == 2, "expected exactly one retry"
    print("test_retries_once_then_succeeds: PASS")


def test_raises_contract_error_after_retry_exhausted() -> None:
    fake = _FakeClient(["not valid json", "still not valid json"])
    llm._client, llm._chat_model = fake, "fake-model"

    try:
        llm._chat_json("system", "user", TriageResult)
        raise AssertionError("expected LLMContractError to be raised")
    except LLMContractError:
        pass

    assert len(fake.chat.completions.calls) == 2, "expected exactly one retry before giving up"
    print("test_raises_contract_error_after_retry_exhausted: PASS")


def test_schema_violation_also_retries_and_fails() -> None:
    # Valid JSON, but "severity" fails the ^S[1-4]$ pattern both times.
    bad_severity = '{"severity": "CRITICAL", "confidence": 0.9, "is_security_related": false, "reasoning": "x"}'
    fake = _FakeClient([bad_severity, bad_severity])
    llm._client, llm._chat_model = fake, "fake-model"

    try:
        llm._chat_json("system", "user", TriageResult)
        raise AssertionError("expected LLMContractError to be raised")
    except LLMContractError:
        pass

    assert len(fake.chat.completions.calls) == 2
    print("test_schema_violation_also_retries_and_fails: PASS")


if __name__ == "__main__":
    test_retries_once_then_succeeds()
    test_raises_contract_error_after_retry_exhausted()
    test_schema_violation_also_retries_and_fails()
