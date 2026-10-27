"""Unit tests for the hosted-agent deploy tooling (hosted/deploy.py). No Azure access, no network:
the plan is built from fixed facts. What matters most here is the safety design — nothing runs
without an explicit "y", there is no run-everything mode, and no secret can reach the agent.
"""
import re

import agents.llm.client as llm
from agents.tests.test_specialist_agents import _ROUTING, _TRIAGE, _Env, _FakeLLM
from hosted import deploy
from hosted.specialist import SPECIALIST_ROLES, run_specialist

_FACTS = deploy.Facts(
    subscription_id="b24a32d9-90f9-4f9b-bde2-f190e8403f48",
    deployer_object_id="11111111-1111-1111-1111-111111111111",
    project_principal_id="22222222-2222-2222-2222-222222222222",
    project_endpoint="https://proj-app.services.ai.azure.com/api/projects/proj-app",
    git_tag="abc1234",
)
_SPEC = deploy.agent_spec("routing")
_EXPECTED_ORDER = [
    "grant_deployer_role", "create_acr", "grant_project_pull", "build_image",
    "create_agent_version", "wait_active", "smoke_test",
]


def test_plan_is_ordered_and_complete() -> None:
    plan = deploy.build_plan(_FACTS, _SPEC)
    assert [s.key for s in plan] == _EXPECTED_ORDER
    for s in plan:
        assert s.title and s.creates and s.cost and s.undo, f"{s.key} must explain itself"
        assert s.command is None or (s.command[0] == "az" and all(isinstance(a, str) for a in s.command)), s.key
    print("test_plan_is_ordered_and_complete: PASS")


def test_names_and_image_are_valid() -> None:
    assert re.fullmatch(r"[a-z0-9]{5,50}", _FACTS.acr_name), _FACTS.acr_name  # ACR naming rules
    assert _FACTS.image == f"{_FACTS.acr_name}.azurecr.io/ops-triage-agent:abc1234"
    assert ":latest" not in _FACTS.image, "the docs say to use unique tags, not :latest"
    assert re.fullmatch(r"[A-Za-z0-9-]{1,63}", _SPEC.name), "Foundry agent names: alphanumeric + hyphens, <= 63"
    assert "/accounts/proj-app/projects/proj-app" in _FACTS.project_id
    print("test_names_and_image_are_valid: PASS")


def test_agent_environment_carries_no_secrets() -> None:
    for role in SPECIALIST_ROLES:
        env = deploy.agent_spec(role).env
        assert env["AGENT_ROLE"] == role and env["AZURE_AUTH_MODE"] == "entra"
        offending = [k for k in env if re.search(r"KEY|SECRET|TOKEN|PASSWORD|CONNECTION", k, re.I)]
        assert not offending, f"{role}: secrets must not be in the agent's env vars: {offending}"
    print("test_agent_environment_carries_no_secrets: PASS")


def test_orchestrator_and_unknown_roles_are_refused() -> None:
    for role in ("orchestrator", "nope"):
        try:
            deploy.agent_spec(role)
            raise AssertionError(f"expected {role} to be refused")
        except SystemExit:
            pass
    print("test_orchestrator_and_unknown_roles_are_refused: PASS")


def test_nothing_runs_without_an_explicit_yes() -> None:
    ran: list = []
    for answer in ("", "n", "no", "yes please", "Y?", "  "):
        ok = deploy.apply_step(_FACTS, _SPEC, "create_acr", ask=lambda _p, a=answer: a, run=lambda *a, **k: ran.append(a))
        assert ok is False
    assert ran == [], "an az command ran without an explicit 'y'"

    ok = deploy.apply_step(_FACTS, _SPEC, "create_acr", ask=lambda _p: "y", run=lambda *a, **k: ran.append(a))
    assert ok is True and len(ran) == 1
    assert ran[0][0][:3] == ["az", "acr", "create"] and _FACTS.acr_name in ran[0][0]
    print("test_nothing_runs_without_an_explicit_yes: PASS")


def test_apply_runs_exactly_one_step_and_rejects_unknown_steps() -> None:
    ran: list = []
    deploy.apply_step(_FACTS, _SPEC, "build_image", ask=lambda _p: "y", run=lambda *a, **k: ran.append(a))
    assert len(ran) == 1, "apply must run exactly the one named step"
    try:
        deploy.apply_step(_FACTS, _SPEC, "run_everything", ask=lambda _p: "y", run=lambda *a, **k: ran.append(a))
        raise AssertionError("expected an unknown step to be rejected")
    except SystemExit:
        pass
    assert len(ran) == 1
    assert "all" not in {s.key for s in deploy.build_plan(_FACTS, _SPEC)}, "no run-everything step"
    print("test_apply_runs_exactly_one_step_and_rejects_unknown_steps: PASS")


def test_rendered_commands_are_copy_pasteable() -> None:
    text = deploy.render_plan(_FACTS, _SPEC)
    assert "--role 'Foundry Project Manager'" in text, "arguments containing spaces must be shell-quoted"
    assert "--role 'Container Registry Repository Reader'" in text
    print("test_rendered_commands_are_copy_pasteable: PASS")


def test_rendered_plan_is_honest_about_its_state() -> None:
    text = deploy.render_plan(_FACTS, _SPEC)
    assert "Nothing below has been run" in text
    for key in _EXPECTED_ORDER:
        assert key in text
    assert "Application Insights is NOT connected" in text, "known gap must be stated, not hidden"
    print("test_rendered_plan_is_honest_about_its_state: PASS")


def test_every_smoke_payload_is_accepted_by_its_specialist() -> None:
    """The payload the deploy tooling would send to a live agent must be one the real specialist
    code accepts — otherwise the first live smoke test would fail for a reason we could have caught."""
    with _Env():
        for role in SPECIALIST_ROLES:
            llm._client = _FakeLLM(_ROUTING if role == "routing" else _TRIAGE)
            out = run_specialist(role, deploy._smoke_payload(deploy.agent_spec(role)))
            assert out["result"] and out["meta"]["role"] == role, role
    print("test_every_smoke_payload_is_accepted_by_its_specialist: PASS")


if __name__ == "__main__":
    test_plan_is_ordered_and_complete()
    test_names_and_image_are_valid()
    test_agent_environment_carries_no_secrets()
    test_orchestrator_and_unknown_roles_are_refused()
    test_nothing_runs_without_an_explicit_yes()
    test_apply_runs_exactly_one_step_and_rejects_unknown_steps()
    test_rendered_commands_are_copy_pasteable()
    test_rendered_plan_is_honest_about_its_state()
    test_every_smoke_payload_is_accepted_by_its_specialist()
