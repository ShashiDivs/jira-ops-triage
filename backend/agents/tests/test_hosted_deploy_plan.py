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
    "grant_deployer_role", "register_acr_provider", "create_acr", "verify_acr_mode", "grant_registry_writer", "grant_project_pull",
    "build_image", "create_agent_version", "wait_active", "smoke_test",
]


def test_plan_is_ordered_and_complete() -> None:
    plan = deploy.build_plan(_FACTS, _SPEC)
    assert [s.key for s in plan] == _EXPECTED_ORDER
    for s in plan:
        assert s.title and s.creates and s.cost and s.undo, f"{s.key} must explain itself"
        assert s.command is None or (s.command[0] == "az" and all(isinstance(a, str) for a in s.command)), s.key
    print("test_plan_is_ordered_and_complete: PASS")


def test_registry_is_abac_and_roles_match_that_mode() -> None:
    """The registry mode and the roles must agree: legacy AcrPull/AcrPush are not honored in ABAC
    registries, and the Repository Reader/Writer roles are the ones that apply there."""
    steps = {s.key: s for s in deploy.build_plan(_FACTS, _SPEC)}
    create = steps["create_acr"].command
    assert create[create.index("--role-assignment-mode") + 1] == "rbac-abac"
    assert "--sku" in create and create[create.index("--sku") + 1] == "Basic"
    for key, role in [("grant_registry_writer", "Container Registry Repository Writer"),
                      ("grant_project_pull", "Container Registry Repository Reader")]:
        cmd = steps[key].command
        assert cmd[cmd.index("--role") + 1] == role, key
    all_cmds = " ".join(" ".join(s.command) for s in steps.values() if s.command)
    assert "AcrPull" not in all_cmds and "AcrPush" not in all_cmds, "legacy roles are not honored in ABAC mode"
    build = steps["build_image"].command
    assert build[build.index("--source-acr-auth-id") + 1] == "[caller]", "ABAC build tasks need an explicit identity"
    verify = steps["verify_acr_mode"].command
    assert "roleAssignmentMode" in verify and steps["verify_acr_mode"].cost == "free"
    print("test_registry_is_abac_and_roles_match_that_mode: PASS")


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
        assert env["OPS_ROLE"] == role and env["AZURE_AUTH_MODE"] == "entra"
        offending = [k for k in env if re.search(r"KEY|SECRET|TOKEN|PASSWORD|CONNECTION", k, re.I)]
        assert not offending, f"{role}: secrets must not be in the agent's env vars: {offending}"
    print("test_agent_environment_carries_no_secrets: PASS")


def test_no_agent_env_var_uses_a_platform_reserved_prefix() -> None:
    """The platform rejects any env var starting with FOUNDRY_ or AGENT_ ("reserved for platform
    use") — the first live deploy failed on AGENT_ROLE. Never again."""
    from config import Settings

    for role in SPECIALIST_ROLES:
        bad = [k for k in deploy.agent_spec(role).env if k.upper().startswith(("FOUNDRY_", "AGENT_"))]
        assert not bad, f"{role}: reserved env var name(s): {bad}"
    # and the app actually reads the role from the name we deploy with
    assert Settings(_env_file=None, ops_role="routing").ops_role == "routing"
    assert "OPS_ROLE" in deploy.agent_spec("routing").env
    print("test_no_agent_env_var_uses_a_platform_reserved_prefix: PASS")


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


def test_build_context_does_not_depend_on_the_working_directory() -> None:
    """The first live build failed with "'backend' doesn't exist" because the context was the relative
    path 'backend' and the tool was run from inside backend/. The context must be absolute and real."""
    import os
    from pathlib import Path

    def context() -> str:
        build = {s.key: s for s in deploy.build_plan(_FACTS, _SPEC)}["build_image"].command
        return build[-1]

    here = context()
    original = os.getcwd()
    try:
        os.chdir("/")
        assert context() == here, "build context changed with the working directory"
    finally:
        os.chdir(original)
    ctx = Path(here)
    assert ctx.is_absolute() and ctx.is_dir(), here
    assert (ctx / "hosted" / "server.py").is_file(), "context is not the backend directory"
    if (ctx.parent / ".git").exists():  # a real checkout; the dev image's .dockerignore strips Dockerfile*
        assert (ctx / "Dockerfile.hosted").is_file(), "Dockerfile.hosted missing from the build context"
    print("test_build_context_does_not_depend_on_the_working_directory: PASS")


def test_a_stalled_az_lookup_fails_loudly_instead_of_hanging() -> None:
    import subprocess

    seen: dict = {}
    original = subprocess.run

    def _stall(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    subprocess.run = _stall
    try:
        deploy._az("ad", "signed-in-user", "show")
        raise AssertionError("expected a SystemExit on a stalled lookup")
    except SystemExit as e:
        assert "did not answer" in str(e) and "Nothing was changed in Azure" in str(e), str(e)
    finally:
        subprocess.run = original
    assert seen.get("timeout") == deploy.LOOKUP_TIMEOUT_SECONDS, "every az lookup must be time-bounded"
    print("test_a_stalled_az_lookup_fails_loudly_instead_of_hanging: PASS")


def test_an_explicit_tag_pins_the_image() -> None:
    pinned = deploy.Facts(**{**_FACTS.__dict__, "git_tag": "1877562"})
    assert pinned.image.endswith("/ops-triage-agent:1877562")
    steps = {s.key: s for s in deploy.build_plan(pinned, _SPEC)}
    assert "1877562" in steps["create_agent_version"].detail, "the agent must reference the pinned image"
    print("test_an_explicit_tag_pins_the_image: PASS")


def test_a_failing_az_command_exits_cleanly_without_a_traceback() -> None:
    import subprocess

    def _boom(cmd, check=True):
        raise subprocess.CalledProcessError(1, cmd)

    try:
        deploy.apply_step(_FACTS, _SPEC, "create_acr", ask=lambda _p: "y", run=_boom)
        raise AssertionError("expected a clean SystemExit")
    except SystemExit as e:
        msg = str(e)
        assert "STEP FAILED" in msg and "create_acr" in msg and "Nothing further was run" in msg, msg
    print("test_a_failing_az_command_exits_cleanly_without_a_traceback: PASS")


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
    test_registry_is_abac_and_roles_match_that_mode()
    test_names_and_image_are_valid()
    test_agent_environment_carries_no_secrets()
    test_no_agent_env_var_uses_a_platform_reserved_prefix()
    test_orchestrator_and_unknown_roles_are_refused()
    test_nothing_runs_without_an_explicit_yes()
    test_apply_runs_exactly_one_step_and_rejects_unknown_steps()
    test_build_context_does_not_depend_on_the_working_directory()
    test_a_stalled_az_lookup_fails_loudly_instead_of_hanging()
    test_an_explicit_tag_pins_the_image()
    test_a_failing_az_command_exits_cleanly_without_a_traceback()
    test_rendered_commands_are_copy_pasteable()
    test_rendered_plan_is_honest_about_its_state()
    test_every_smoke_payload_is_accepted_by_its_specialist()
