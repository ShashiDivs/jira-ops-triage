"""Unit tests for the hosted-agent topology: the four specialist agents (hosted/specialist.py),
the orchestrator's remote path with its fallbacks (agents/integrations/specialist_client.py +
agents/llm/client.py), and the Invocations HTTP layer (hosted/server.py). No network, no DB, no
real model — a fake OpenAI-shaped client and an in-process stand-in for the network.
"""
import httpx
from starlette.testclient import TestClient

import agents.llm.client as llm
from agents.integrations import specialist_client as sc
from agents.llm.client import _token_usage_var
from hosted.server import build_app
from hosted.specialist import BadRequest, run_specialist

_TRIAGE = '{"severity": "S2", "confidence": 0.8, "is_security_related": false, "reasoning": "ok"}'
_ROUTING = '{"team": "Platform team", "reasoning": "ok"}'
_TICKET = {"summary": "s", "description": "d", "component": "checkout-service"}


class _Usage:
    prompt_tokens, completion_tokens = 100, 20


class _Completions:
    def __init__(self, content): self.content, self.calls = content, 0
    def create(self, **kw):
        self.calls += 1
        msg = type("M", (), {"content": self.content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()], "usage": _Usage()})()


class _FakeLLM:
    def __init__(self, content):
        self.completions = _Completions(content)
        self.chat = type("Chat", (), {"completions": self.completions})()

    @property
    def calls(self): return self.completions.calls


def _acc():
    return _token_usage_var.set({"tokens_in": 0, "tokens_out": 0, "model": None, "prompt_version": None})


class _Env:
    """Swaps module state for one test and restores it after."""
    def __enter__(self):
        self.saved = (llm._client, llm._chat_model, llm._chat_model_fast, llm._chat_model_security,
                      sc.settings.specialist_urls, sc._post)
        llm._chat_model, llm._chat_model_fast, llm._chat_model_security = "main", "fast", "strong"
        sc._breaker.record_success()
        return self

    def __exit__(self, *exc):
        (llm._client, llm._chat_model, llm._chat_model_fast, llm._chat_model_security,
         sc.settings.specialist_urls, sc._post) = self.saved
        sc._breaker.record_success()


# --- the specialists themselves -------------------------------------------------------------

def test_each_specialist_returns_a_validated_result_and_metadata() -> None:
    with _Env():
        cases = [
            ("triage-generic", {"ticket": _TICKET, "similar_tickets": [], "severity_policy": "p"}, _TRIAGE, "main"),
            ("triage-security", {"ticket": _TICKET, "similar_tickets": [], "security_runbook": "r"}, _TRIAGE, "strong"),
            ("triage-trivial", {"ticket": _TICKET}, _TRIAGE, "fast"),
            ("routing", {"ticket": {"component": "c"}, "ownership_doc": "m"}, _ROUTING, "main"),
        ]
        for role, payload, reply, expected_model in cases:
            llm._client = _FakeLLM(reply)
            out = run_specialist(role, payload)
            assert out["meta"]["model"] == expected_model, (role, out["meta"])
            assert out["meta"]["prompt_version"] == "v1", (role, out["meta"])
            assert (out["meta"]["tokens_in"], out["meta"]["tokens_out"]) == (100, 20), (role, out["meta"])
            assert out["meta"]["role"] == role
        assert out["result"]["team"] == "Platform team"
    print("test_each_specialist_returns_a_validated_result_and_metadata: PASS")


def test_specialist_security_invariant_and_bad_requests() -> None:
    with _Env():
        llm._client = _FakeLLM(_TRIAGE)  # model says is_security_related=false
        out = run_specialist("triage-security", {"ticket": _TICKET, "security_runbook": "r"})
        assert out["result"]["is_security_related"] is True
        for role, payload in [("triage-generic", {"ticket": _TICKET}), ("nope", {}), ("routing", {})]:
            try:
                run_specialist(role, payload)
                raise AssertionError(f"expected BadRequest for {role}")
            except BadRequest:
                pass
    print("test_specialist_security_invariant_and_bad_requests: PASS")


# --- the orchestrator's remote path ---------------------------------------------------------

def _fake_network(sink: list | None = None):
    """Stands in for the network: routes a POST to the right specialist in-process, so the
    request/response wire format is exercised end to end (JSON round-trip included)."""
    import json

    def _post(url, payload):
        role = url.rsplit("/", 1)[-1]
        if sink is not None:
            sink.append(role)
        return json.loads(json.dumps(run_specialist(role, json.loads(json.dumps(payload)))))
    return _post


def test_remote_specialist_is_used_and_recorded() -> None:
    with _Env():
        sc.settings.specialist_urls = "triage-generic=http://x/triage-generic,routing=http://x/routing"
        calls: list[str] = []
        sc._post = _fake_network(calls)
        llm._client = _FakeLLM(_TRIAGE)

        tok = _acc()
        try:
            result = llm.classify_severity(_TICKET, [], "policy")
            acc = dict(_token_usage_var.get())
        finally:
            _token_usage_var.reset(tok)

        assert calls == ["triage-generic"] and result["severity"] == "S2"
        assert (acc["model"], acc["prompt_version"], acc["tokens_in"], acc["tokens_out"]) == ("main", "v1", 100, 20)
        # the specialist's own model call happened exactly once (inside run_specialist), and the
        # orchestrator made none of its own
        assert llm._client.calls == 1

        llm._client = _FakeLLM(_ROUTING)
        assert llm.classify_team({"component": "c"}, "map")["team"] == "Platform team"
        assert calls == ["triage-generic", "routing"]
    print("test_remote_specialist_is_used_and_recorded: PASS")


def test_unconfigured_roles_stay_in_process() -> None:
    with _Env():
        sc.settings.specialist_urls = "routing=http://x/routing"  # generic NOT configured
        calls: list[str] = []
        sc._post = _fake_network(calls)
        llm._client = _FakeLLM(_TRIAGE)
        llm.classify_severity(_TICKET, [], "policy")
        assert calls == [] and llm._client.calls == 1
    print("test_unconfigured_roles_stay_in_process: PASS")


def test_security_invariant_is_enforced_on_the_remote_result_too() -> None:
    with _Env():
        sc.settings.specialist_urls = "triage-security=http://x/triage-security"
        sc._post = lambda url, payload: {
            "result": {"severity": "S1", "confidence": 0.9, "is_security_related": False, "reasoning": "x"},
            "meta": {"model": "evil", "prompt_version": "v9"},
        }
        assert llm.classify_severity_security(_TICKET, [], "runbook")["is_security_related"] is True
    print("test_security_invariant_is_enforced_on_the_remote_result_too: PASS")


def test_contract_violating_remote_result_falls_back_in_process() -> None:
    with _Env():
        sc.settings.specialist_urls = "triage-generic=http://x/triage-generic"
        sc._post = lambda url, payload: {"result": {"severity": "BANANA", "confidence": 9}, "meta": {}}
        llm._client = _FakeLLM(_TRIAGE)
        result = llm.classify_severity(_TICKET, [], "policy")
        assert result["severity"] == "S2" and llm._client.calls == 1, "should have used the local model"
    print("test_contract_violating_remote_result_falls_back_in_process: PASS")


def test_unreachable_specialist_falls_back_then_breaker_opens() -> None:
    with _Env():
        sc.settings.specialist_urls = "triage-generic=http://x/triage-generic"
        attempts = {"n": 0}

        def _down(url, payload):
            attempts["n"] += 1
            raise sc._TransientSpecialistError("connection refused")

        sc._post = _down
        llm._client = _FakeLLM(_TRIAGE)
        original_sleep = __import__("time").sleep
        __import__("time").sleep = lambda s: None  # don't actually back off in a unit test
        try:
            for _ in range(3):
                assert llm.classify_severity(_TICKET, [], "policy")["severity"] == "S2"
            assert llm._client.calls == 3, "every ticket still got classified (via the local fallback)"
            used = attempts["n"]
            llm.classify_severity(_TICKET, [], "policy")
            assert attempts["n"] == used, "breaker is open: the dead specialist must not be called again"
            assert sc._breaker.is_open
        finally:
            __import__("time").sleep = original_sleep
    print("test_unreachable_specialist_falls_back_then_breaker_opens: PASS")


def test_a_specialist_never_calls_another_specialist() -> None:
    with _Env():
        sc.settings.specialist_urls = "triage-generic=http://x/triage-generic"
        calls: list[str] = []
        sc._post = _fake_network(calls)
        llm._client = _FakeLLM(_TRIAGE)
        # run_specialist sets the guard; even with URLs configured it must stay in-process
        run_specialist("triage-generic", {"ticket": _TICKET, "severity_policy": "p"})
        assert calls == [], "specialist must not recurse into the remote path"
    print("test_a_specialist_never_calls_another_specialist: PASS")


def test_http_status_handling() -> None:
    with _Env():
        original = httpx.post
        try:
            for status, expected in [(500, sc._TransientSpecialistError), (429, sc._TransientSpecialistError), (400, ValueError)]:
                httpx.post = lambda *a, _s=status, **k: httpx.Response(_s, text="boom", request=httpx.Request("POST", "http://x"))
                try:
                    sc._post("http://x", {})
                    raise AssertionError(f"expected an error for HTTP {status}")
                except expected:
                    pass
            httpx.post = lambda *a, **k: httpx.Response(200, json={"result": {}}, request=httpx.Request("POST", "http://x"))
            assert sc._post("http://x", {}) == {"result": {}}
        finally:
            httpx.post = original
    print("test_http_status_handling: PASS")


# --- the Invocations HTTP layer -------------------------------------------------------------

def _reset_client_state() -> None:
    sc._sessions.clear()
    sc._token_provider = None


def test_session_id_is_captured_and_reused() -> None:
    """Without this every call to a hosted agent is a cold start (~6-8 s measured live)."""
    with _Env():
        _reset_client_state()
        seen: list[str] = []
        original = httpx.post

        def fake_post(url, **kw):
            seen.append(url)
            return httpx.Response(200, json={"result": {"x": 1}}, headers={"x-agent-session-id": "SESS-1"},
                                  request=httpx.Request("POST", url))

        httpx.post = fake_post
        try:
            base = "https://host/agents/ops-routing/endpoint/protocols/invocations?api-version=v1"
            sc._post(base, {})
            sc._post(base, {})
            other = base.replace("ops-routing", "ops-other")
            sc._post(other, {})
        finally:
            httpx.post = original
            _reset_client_state()
        assert seen[0] == base, "the first call carries no session id"
        assert "agent_session_id=SESS-1" in seen[1] and "api-version=v1" in seen[1], seen[1]
        assert "agent_session_id" not in seen[2], "a session belongs to one specialist, not shared across them"
    print("test_session_id_is_captured_and_reused: PASS")


def test_a_rejected_session_is_dropped_and_retried_once_without_it() -> None:
    with _Env():
        _reset_client_state()
        url = "https://host/agents/a/endpoint/protocols/invocations?api-version=v1"
        sc._sessions[url] = "STALE"
        calls: list[str] = []
        original = httpx.post

        def fake_post(target, **kw):
            calls.append(target)
            if "agent_session_id=STALE" in target:
                return httpx.Response(404, text="session not found", request=httpx.Request("POST", target))
            return httpx.Response(200, json={"result": {"ok": True}}, headers={"x-agent-session-id": "FRESH"},
                                  request=httpx.Request("POST", target))

        httpx.post = fake_post
        try:
            assert sc._post(url, {}) == {"result": {"ok": True}}
            assert len(calls) == 2 and "STALE" in calls[0] and "agent_session_id" not in calls[1]
            assert sc._sessions[url] == "FRESH", "the replacement session is remembered"
            # a genuinely bad request fails (once retried) as a non-transient error — never loops
            sc._sessions[url] = "S2"
            httpx.post = lambda target, **kw: httpx.Response(400, text="bad", request=httpx.Request("POST", target))
            try:
                sc._post(url, {})
                raise AssertionError("expected a 400 to surface")
            except ValueError:
                pass
        finally:
            httpx.post = original
            _reset_client_state()
    print("test_a_rejected_session_is_dropped_and_retried_once_without_it: PASS")


def test_the_entra_token_provider_is_built_once() -> None:
    with _Env():
        _reset_client_state()
        built = {"n": 0}
        original_factory, original_auth = llm._entra_token_provider, sc.settings.specialist_auth

        def factory():
            built["n"] += 1
            return lambda: "tok"

        llm._entra_token_provider = factory
        sc.settings.specialist_auth = "entra"
        try:
            heads = [sc._auth_headers() for _ in range(4)]
        finally:
            llm._entra_token_provider, sc.settings.specialist_auth = original_factory, original_auth
            _reset_client_state()
        assert built["n"] == 1, f"provider rebuilt {built['n']} times — each rebuild re-acquires a token (~1.3 s live)"
        assert all(h == {"Authorization": "Bearer tok"} for h in heads)
    print("test_the_entra_token_provider_is_built_once: PASS")


def test_specialist_http_endpoint() -> None:
    with _Env():
        llm._client = _FakeLLM(_TRIAGE)
        with TestClient(build_app("triage-trivial")) as client:
            assert client.get("/readiness").status_code == 200
            ok = client.post("/invocations", json={"ticket": _TICKET})
            assert ok.status_code == 200, ok.text
            assert ok.json()["result"]["severity"] == "S2" and ok.json()["meta"]["model"] == "fast"
            assert client.post("/invocations", json={}).status_code == 400, "missing field -> 400"
            assert client.post("/invocations", content=b"not json").status_code == 400
            assert client.post("/invocations", json=[1, 2]).status_code == 400, "must be a JSON object"
    print("test_specialist_http_endpoint: PASS")


def test_build_app_rejects_an_unknown_role() -> None:
    try:
        build_app("not-a-role")
        raise AssertionError("expected SystemExit for an unknown role")
    except SystemExit:
        pass
    print("test_build_app_rejects_an_unknown_role: PASS")


if __name__ == "__main__":
    test_each_specialist_returns_a_validated_result_and_metadata()
    test_specialist_security_invariant_and_bad_requests()
    test_remote_specialist_is_used_and_recorded()
    test_unconfigured_roles_stay_in_process()
    test_security_invariant_is_enforced_on_the_remote_result_too()
    test_contract_violating_remote_result_falls_back_in_process()
    test_unreachable_specialist_falls_back_then_breaker_opens()
    test_a_specialist_never_calls_another_specialist()
    test_http_status_handling()
    test_session_id_is_captured_and_reused()
    test_a_rejected_session_is_dropped_and_retried_once_without_it()
    test_the_entra_token_provider_is_built_once()
    test_specialist_http_endpoint()
    test_build_app_rejects_an_unknown_role()
