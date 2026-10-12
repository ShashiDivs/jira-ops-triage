"""Unit test for prompt/model traceability (Governance — Auditability): verifies the LLM client
records the model and PROMPT_VERSION that produced a call into the per-node accumulator that
agents/integrations/observability.py persists to agent_runs, and that each classify_* function
passes its own prompt module's version. No network, no DB — a fake OpenAI-shaped client.
"""
import agents.llm.client as llm
from agents.llm.client import _token_usage_var
from agents.prompts import routing as routing_prompt
from agents.prompts import triage_generic, triage_security, triage_trivial

_TRIAGE_JSON = '{"severity": "S2", "confidence": 0.8, "is_security_related": false, "reasoning": "ok"}'
_ROUTING_JSON = '{"team": "Platform team", "reasoning": "ok"}'


class _Msg:
    def __init__(self, c): self.content = c


class _Choice:
    def __init__(self, c): self.message = _Msg(c)


class _Resp:
    def __init__(self, c):
        self.choices = [_Choice(c)]
        self.usage = None


class _Completions:
    def __init__(self, content): self._content = content
    def create(self, **kwargs): return _Resp(self._content)


class _Client:
    def __init__(self, content):
        self.chat = type("Chat", (), {"completions": _Completions(content)})()


def _run_with_accumulator(fn) -> dict:
    token = _token_usage_var.set({"tokens_in": 0, "tokens_out": 0, "model": None, "prompt_version": None})
    try:
        fn()
        return dict(_token_usage_var.get())
    finally:
        _token_usage_var.reset(token)


def main() -> None:
    original = (llm._client, llm._chat_model, llm._chat_model_fast, llm._chat_model_security)
    llm._chat_model, llm._chat_model_fast, llm._chat_model_security = "main-model", "fast-model", "security-model"
    ticket = {"summary": "s", "description": "d", "component": "c"}
    try:
        llm._client = _Client(_TRIAGE_JSON)

        acc = _run_with_accumulator(lambda: llm.classify_severity(ticket, [], "policy"))
        assert acc["model"] == "main-model" and acc["prompt_version"] == triage_generic.PROMPT_VERSION, acc
        print("test_generic_triage_records_model_and_prompt_version: PASS")

        acc = _run_with_accumulator(lambda: llm.classify_severity_security(ticket, [], "runbook"))
        assert acc["model"] == "security-model" and acc["prompt_version"] == triage_security.PROMPT_VERSION, acc
        print("test_security_triage_records_the_security_model: PASS")

        acc = _run_with_accumulator(lambda: llm.classify_severity_fast(ticket))
        assert acc["model"] == "fast-model" and acc["prompt_version"] == triage_trivial.PROMPT_VERSION, acc
        print("test_trivial_triage_records_the_fast_model: PASS")

        llm._client = _Client(_ROUTING_JSON)
        acc = _run_with_accumulator(lambda: llm.classify_team({"component": "c"}, "map"))
        assert acc["model"] == "main-model" and acc["prompt_version"] == routing_prompt.PROMPT_VERSION, acc
        print("test_routing_records_model_and_prompt_version: PASS")

        # Outside a traced node there is no accumulator — recording must be a silent no-op.
        llm._client = _Client(_TRIAGE_JSON)
        llm.classify_severity(ticket, [], "policy")
        print("test_no_accumulator_outside_traced_node_is_a_noop: PASS")
    finally:
        llm._client, llm._chat_model, llm._chat_model_fast, llm._chat_model_security = original


if __name__ == "__main__":
    main()
