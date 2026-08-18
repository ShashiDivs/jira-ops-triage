"""Tests for Failure Containment / Blast Radius (Phase 5): the shared retry/circuit-breaker
primitives, the LLM client degrading to the keyword-heuristic fallback once its breaker trips,
and the MCP client degrading read-only RAG lookups to [] once its breaker trips (while writes
still raise — there's no safe way to fake a Jira write). No live DB/LLM/subprocess needed.
"""
import asyncio

from agents.safety.resilience import CircuitBreaker, acall_with_retry, call_with_retry


class _RetryableError(Exception):
    pass


class _FatalError(Exception):
    pass


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

def test_breaker_trips_after_threshold_and_resets_on_success() -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)
    assert not breaker.is_open

    breaker.record_failure()
    breaker.record_failure()
    assert not breaker.is_open, "should not trip before reaching the threshold"

    breaker.record_failure()
    assert breaker.is_open, "should trip on the 3rd consecutive failure"

    breaker.record_success()
    assert not breaker.is_open, "a success should reset the breaker immediately"
    print("test_breaker_trips_after_threshold_and_resets_on_success: PASS")


# ---------------------------------------------------------------------------
# call_with_retry / acall_with_retry
# ---------------------------------------------------------------------------

def test_call_with_retry_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _RetryableError("not yet")
        return "ok"

    result = call_with_retry(flaky, max_attempts=3, base_delay=0.01, should_retry=lambda e: isinstance(e, _RetryableError))
    assert result == "ok"
    assert calls["n"] == 3
    print("test_call_with_retry_retries_then_succeeds: PASS")


def test_call_with_retry_does_not_retry_non_matching_errors() -> None:
    calls = {"n": 0}

    def always_fatal():
        calls["n"] += 1
        raise _FatalError("nope")

    try:
        call_with_retry(always_fatal, max_attempts=3, base_delay=0.01, should_retry=lambda e: isinstance(e, _RetryableError))
        raise AssertionError("expected _FatalError to propagate")
    except _FatalError:
        pass
    assert calls["n"] == 1, "a non-retryable error should not be retried at all"
    print("test_call_with_retry_does_not_retry_non_matching_errors: PASS")


def test_acall_with_retry_gives_up_after_max_attempts() -> None:
    calls = {"n": 0}

    async def always_flaky():
        calls["n"] += 1
        raise _RetryableError("still failing")

    async def run():
        try:
            await acall_with_retry(always_flaky, max_attempts=3, base_delay=0.01, should_retry=lambda e: True)
            raise AssertionError("expected _RetryableError to propagate")
        except _RetryableError:
            pass

    asyncio.run(run())
    assert calls["n"] == 3
    print("test_acall_with_retry_gives_up_after_max_attempts: PASS")


# ---------------------------------------------------------------------------
# LLM client: breaker trip degrades to the keyword-heuristic fallback
# ---------------------------------------------------------------------------

def test_llm_falls_back_to_heuristic_once_breaker_is_open() -> None:
    import agents.llm.client as llm

    class _NeverCalledClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("LLM should not be called while the breaker is open")

    llm._client = _NeverCalledClient()
    llm._llm_breaker.record_failure()
    llm._llm_breaker.record_failure()
    llm._llm_breaker.record_failure()
    assert llm._llm_breaker.is_open

    result = llm.classify_severity(
        {"summary": "Complete outage", "description": "everything is down", "component": "order-service"},
        [],
        "policy",
    )
    assert "circuit breaker open" in result["reasoning"]
    assert result["severity"] == "S1"  # outage keyword, via the heuristic path

    llm._llm_breaker.record_success()
    print("test_llm_falls_back_to_heuristic_once_breaker_is_open: PASS")


# ---------------------------------------------------------------------------
# MCP client: retries transient failures, degrades reads to [] once tripped, but still
# raises for writes (no safe fallback for a Jira write).
# ---------------------------------------------------------------------------

def test_mcp_retries_degrades_reads_but_not_writes() -> None:
    import agents.integrations.mcp_client as mcp_client

    spawn_attempts = {"n": 0}

    class _AlwaysFailingStdioClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            spawn_attempts["n"] += 1
            raise ConnectionError("simulated: cannot spawn MCP subprocess")

        async def __aexit__(self, *args) -> bool:
            return False

    original_stdio_client = mcp_client.stdio_client
    mcp_client.stdio_client = lambda *a, **kw: _AlwaysFailingStdioClient()
    mcp_client._mcp_breaker = mcp_client.CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)

    async def run():
        client = mcp_client.MCPClient()

        # Call 1: a degradable read tool. Retries 3x internally, then degrades to [] instead
        # of raising, recording one breaker failure.
        result = await client.call_tool("search_similar_tickets", {"query": "x", "k": 3})
        assert result == [], "should degrade to [] rather than raise"
        assert spawn_attempts["n"] == 3, f"expected 3 spawn attempts (max_attempts), got {spawn_attempts['n']}"

        # Two more failing calls to reach the breaker's threshold of 3 consecutive failures.
        await client.call_tool("search_similar_tickets", {"query": "y", "k": 3})
        await client.call_tool("search_similar_tickets", {"query": "z", "k": 3})
        assert mcp_client._mcp_breaker.is_open

        # Breaker is now open: a further read call should short-circuit without spawning at all.
        before = spawn_attempts["n"]
        result = await client.call_tool("search_runbooks", {"query": "x", "k": 1})
        assert result == []
        assert spawn_attempts["n"] == before, "breaker being open should skip the subprocess entirely"

        # A write tool must still raise even with the breaker open — no safe fallback exists.
        try:
            await client.call_tool("update_ticket", {"ticket_id": "abc", "status": "approved"})
            raise AssertionError("expected update_ticket to raise, not degrade")
        except ConnectionError:
            pass

    try:
        asyncio.run(run())
    finally:
        mcp_client.stdio_client = original_stdio_client

    print("test_mcp_retries_degrades_reads_but_not_writes: PASS")


if __name__ == "__main__":
    test_breaker_trips_after_threshold_and_resets_on_success()
    test_call_with_retry_retries_then_succeeds()
    test_call_with_retry_does_not_retry_non_matching_errors()
    test_acall_with_retry_gives_up_after_max_attempts()
    test_llm_falls_back_to_heuristic_once_breaker_is_open()
    test_mcp_retries_degrades_reads_but_not_writes()
