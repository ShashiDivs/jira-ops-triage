"""Unit test for the public-demo protections: passcode gate, submission limiter, same-origin static
serving and the seed-if-empty helper. No database and no model calls: the gate and the static mount sit in
front of the routes, and the helper is exercised with fakes.
"""
import os
import tempfile
from pathlib import Path

_STATIC = tempfile.mkdtemp()
Path(_STATIC, "index.html").write_text("<html><body>OPS-TRIAGE-WEB</body></html>")

# Settings are read once at import, so configure the "public demo" BEFORE importing the app.
os.environ["DEMO_PASSCODE"] = "open-sesame"
os.environ["STATIC_DIR"] = _STATIC

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.demo_guard import SubmissionLimiter, client_address  # noqa: E402
from api.main import app  # noqa: E402

client = TestClient(app)  # not used as a context manager: no lifespan, so no database or MCP is started


def test_api_is_locked_without_the_passcode() -> None:
    for method, path in (("get", "/api/tickets"), ("get", "/api/tickets/pending"), ("post", "/api/tickets"),
                         ("post", "/api/reset"), ("get", "/api/session"), ("post", "/api/scheduler/resume")):
        r = getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} must need the passcode, got {r.status_code}"
    wrong = client.get("/api/session", headers={"X-Demo-Passcode": "open-sesam"})
    assert wrong.status_code == 401
    print("test_api_is_locked_without_the_passcode: PASS")


def test_the_right_passcode_opens_it() -> None:
    r = client.get("/api/session", headers={"X-Demo-Passcode": "open-sesame"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    print("test_the_right_passcode_opens_it: PASS")


def test_health_and_gate_status_stay_open() -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/gate").json() == {"required": True}
    print("test_health_and_gate_status_stay_open: PASS")


def test_a_cors_preflight_is_not_blocked() -> None:
    r = client.options("/api/tickets", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "x-demo-passcode,content-type"})
    assert r.status_code == 200, r.status_code
    print("test_a_cors_preflight_is_not_blocked: PASS")


def test_the_built_frontend_is_served_from_the_same_process() -> None:
    page = client.get("/")
    assert page.status_code == 200 and "OPS-TRIAGE-WEB" in page.text, "the page must load so it can ask for the passcode"
    assert client.get("/api/health").headers["content-type"].startswith("application/json"), "the mount must not shadow /api"
    print("test_the_built_frontend_is_served_from_the_same_process: PASS")


def test_no_passcode_means_no_gate() -> None:
    from api.demo_guard import PasscodeGate
    from fastapi import FastAPI

    bare = FastAPI()
    bare.add_middleware(PasscodeGate, passcode="")

    @bare.get("/api/x")
    def _x():
        return {"ok": True}

    assert TestClient(bare).get("/api/x").status_code == 200
    print("test_no_passcode_means_no_gate: PASS")


def test_limiter_per_minute_and_window_reset() -> None:
    now = [0.0]
    lim = SubmissionLimiter(per_minute=2, clock=lambda: now[0])
    lim.check("a"), lim.check("a")
    try:
        lim.check("a")
        raise AssertionError("third request in a minute must be refused")
    except HTTPException as e:
        assert e.status_code == 429
    lim.check("b")  # another client is unaffected
    now[0] = 61.0
    lim.check("a")  # the window has passed
    print("test_limiter_per_minute_and_window_reset: PASS")


def test_limiter_total_cap_cannot_be_dodged_by_changing_address() -> None:
    lim = SubmissionLimiter(total_cap=3)
    for i in range(3):
        lim.check(f"client-{i}")
    try:
        lim.check("a-brand-new-client")
        raise AssertionError("the total cap must apply to every client")
    except HTTPException as e:
        assert e.status_code == 429 and "limit" in e.detail
    print("test_limiter_total_cap_cannot_be_dodged_by_changing_address: PASS")


def test_unconfigured_limiter_allows_everything() -> None:
    lim = SubmissionLimiter()
    for _ in range(500):
        lim.check("a")
    print("test_unconfigured_limiter_allows_everything: PASS")


def test_the_trusted_client_address_is_the_one_the_proxy_saw() -> None:
    from starlette.requests import Request

    def req(xff: str | None):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": ("10.0.0.9", 1234), "method": "GET", "path": "/"})

    assert client_address(req("6.6.6.6, 203.0.113.5")) == "203.0.113.5", "a client-supplied leading entry must not be trusted"
    assert client_address(req(None)) == "10.0.0.9"
    print("test_the_trusted_client_address_is_the_one_the_proxy_saw: PASS")


def test_the_limiter_is_wired_to_ticket_submission() -> None:
    from api.routers import tickets

    tickets._limiter = SubmissionLimiter(total_cap=1)
    headers = {"X-Demo-Passcode": "open-sesame"}
    body = {"summary": "s", "description": "d", "component": "c", "reporter": "r"}
    # The first request is allowed past the limiter; with no database here the handler itself then fails (500) —
    # which is fine, the point is only that it was not refused.
    first = TestClient(app, raise_server_exceptions=False).post("/api/tickets", json=body, headers=headers)
    second = TestClient(app, raise_server_exceptions=False).post("/api/tickets", json=body, headers=headers)
    assert first.status_code != 429, first.status_code
    assert second.status_code == 429 and "limit" in second.json()["detail"], (second.status_code, second.text)
    print("test_the_limiter_is_wired_to_ticket_submission: PASS")


def test_seed_if_empty_only_seeds_an_empty_corpus() -> None:
    import scripts.seed_if_empty as mod

    calls, counts = [], {"v": (0, 0)}

    class _Q:
        def __init__(self, n): self.n = n
        def count(self): return self.n

    class _Db:
        def __init__(self): self.i = 0
        def query(self, _model):
            self.i += 1
            return _Q(counts["v"][self.i - 1])
        def close(self): pass

    mod.SessionLocal, mod.seed = _Db, lambda: calls.append("seed")
    mod.main()
    assert calls == ["seed"], "empty corpus must be seeded"
    counts["v"] = (10, 5)
    mod.main()
    assert calls == ["seed"], "a populated corpus must NOT be reseeded (that wipes it and spends embedding calls)"
    counts["v"] = (10, 0)
    mod.main()
    assert calls == ["seed", "seed"], "a half-empty corpus is treated as empty"
    print("test_seed_if_empty_only_seeds_an_empty_corpus: PASS")


if __name__ == "__main__":
    test_api_is_locked_without_the_passcode()
    test_the_right_passcode_opens_it()
    test_health_and_gate_status_stay_open()
    test_a_cors_preflight_is_not_blocked()
    test_the_built_frontend_is_served_from_the_same_process()
    test_no_passcode_means_no_gate()
    test_limiter_per_minute_and_window_reset()
    test_limiter_total_cap_cannot_be_dodged_by_changing_address()
    test_unconfigured_limiter_allows_everything()
    test_the_trusted_client_address_is_the_one_the_proxy_saw()
    test_the_limiter_is_wired_to_ticket_submission()
    test_seed_if_empty_only_seeds_an_empty_corpus()
    print("All demo guard tests passed.")
