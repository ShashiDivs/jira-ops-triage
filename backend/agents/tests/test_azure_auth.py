"""Unit test for Foundry auth modes (keyless Entra login for hosted agents). No network, no
Azure: the OpenAI client is given an httpx MockTransport, so we assert on the exact URL and
Authorization header that WOULD be sent — in key mode, in keyless (entra) mode, and when the
endpoint is the project-endpoint form the hosted-agent platform injects.
"""
import httpx

import agents.llm.client as llm
from config import Settings

_COMPLETION = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
}


def _settings(**kw) -> Settings:
    # _env_file=None ignores a local .env, but Settings still reads real environment variables
    # (the container injects the .env as env vars) — so blank every field these tests reason
    # about, or "no endpoint configured" would silently pick up the real one.
    defaults = {
        "openai_api_key": "",
        "azure_ai_foundry_api_key": "",
        "azure_ai_foundry_endpoint": "",
        "foundry_project_endpoint": "",
        "azure_auth_mode": "key",
    }
    return Settings(_env_file=None, **{**defaults, **kw})


def _call(client_cfg: Settings) -> httpx.Request:
    """Build the Azure client from `client_cfg`, make one chat call, return the captured request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_COMPLETION)

    client = llm._build_azure_client(client_cfg, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert len(seen) == 1
    return seen[0]


def test_base_url_normalization() -> None:
    root = "https://r.services.ai.azure.com/openai/v1/"
    assert llm._v1_base_url("https://r.services.ai.azure.com") == root
    assert llm._v1_base_url("https://r.services.ai.azure.com/") == root
    assert llm._v1_base_url("https://r.services.ai.azure.com/openai/v1") == root
    # the form the hosted-agent platform injects:
    assert llm._v1_base_url("https://r.services.ai.azure.com/api/projects/my-proj") == root
    assert llm._v1_base_url("https://r.services.ai.azure.com/api/projects/my-proj/") == root
    print("test_base_url_normalization: PASS")


def test_configured_logic() -> None:
    ep = "https://r.services.ai.azure.com"
    assert _settings(azure_ai_foundry_endpoint=ep, azure_ai_foundry_api_key="k").azure_configured
    assert not _settings(azure_ai_foundry_endpoint=ep).azure_configured, "key mode without a key is not configured"
    assert _settings(azure_ai_foundry_endpoint=ep, azure_auth_mode="entra").azure_configured, "entra needs no key"
    assert not _settings(azure_auth_mode="entra").azure_configured, "entra still needs an endpoint"
    # platform-injected project endpoint is accepted as the endpoint
    s = _settings(foundry_project_endpoint=f"{ep}/api/projects/p", azure_auth_mode="entra")
    assert s.azure_configured and s.azure_endpoint.endswith("/api/projects/p")
    print("test_configured_logic: PASS")


def test_key_mode_sends_the_api_key() -> None:
    cfg = _settings(azure_ai_foundry_endpoint="https://r.services.ai.azure.com", azure_ai_foundry_api_key="secret-key")
    req = _call(cfg)
    assert req.headers["authorization"] == "Bearer secret-key"
    assert str(req.url) == "https://r.services.ai.azure.com/openai/v1/chat/completions"
    print("test_key_mode_sends_the_api_key: PASS")


def test_entra_mode_sends_a_token_and_no_key() -> None:
    original = llm._entra_token_provider
    llm._entra_token_provider = lambda: (lambda: "entra-token-123")
    try:
        cfg = _settings(
            foundry_project_endpoint="https://r.services.ai.azure.com/api/projects/p", azure_auth_mode="entra"
        )
        req = _call(cfg)
    finally:
        llm._entra_token_provider = original
    assert req.headers["authorization"] == "Bearer entra-token-123"
    assert str(req.url) == "https://r.services.ai.azure.com/openai/v1/chat/completions"
    print("test_entra_mode_sends_a_token_and_no_key: PASS")


if __name__ == "__main__":
    test_base_url_normalization()
    test_configured_logic()
    test_key_mode_sends_the_api_key()
    test_entra_mode_sends_a_token_and_no_key()
