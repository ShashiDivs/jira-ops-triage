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
    subscription_id="00000000-aaaa-bbbb-cccc-111111111111",
    deployer_object_id="11111111-1111-1111-1111-111111111111",
    project_principal_id="22222222-2222-2222-2222-222222222222",
    project_endpoint="https://proj-app.services.ai.azure.com/api/projects/proj-app",
    git_tag="abc1234",
    deployer_upn="dev.user@example.com",
    client_ip="203.0.113.7",
)
_SPEC = deploy.agent_spec("routing")
_EXPECTED_ORDER = [
    "grant_deployer_role", "register_acr_provider", "create_acr", "verify_acr_mode", "grant_registry_writer", "grant_project_pull",
    "build_image", "create_agent_version", "wait_active", "grant_agent_model_access", "smoke_test",
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


# ---------------------------------------------------------------------------------------------
# The orchestrator: Postgres + hosted agent
# ---------------------------------------------------------------------------------------------

_ORCH = deploy.agent_spec("orchestrator", _FACTS)
_ORCH_ORDER = [
    "register_postgres_provider", "create_postgres", "set_postgres_entra_admin", "allow_pgvector",
    "firewall_azure_services", "firewall_your_ip", "create_database", "migrate_and_seed",
    "build_image", "create_agent_version", "wait_active", "grant_agent_model_access",
    "grant_agent_database_access", "grant_orchestrator_invoke_specialists", "smoke_test",
]


def test_orchestrator_plan_is_ordered_and_reuses_the_registry() -> None:
    keys = [s.key for s in deploy.build_plan(_FACTS, _ORCH, "99999999-9999-9999-9999-999999999999")]
    assert keys == _ORCH_ORDER, keys
    assert not {"create_acr", "grant_registry_writer", "grant_project_pull"} & set(keys), "registry already exists"
    assert _ORCH.name == "ops-orchestrator" and (_ORCH.cpu, _ORCH.memory) == ("1", "2Gi")
    print("test_orchestrator_plan_is_ordered_and_reuses_the_registry: PASS")


def test_orchestrator_env_carries_no_secrets_and_no_database_password() -> None:
    env = _ORCH.env
    assert env["OPS_ROLE"] == "orchestrator" and env["DATABASE_AUTH_MODE"] == "entra" and env["SPECIALIST_AUTH"] == "entra"
    assert env["CHECKPOINTER_AUTO_SETUP"] == "false"
    assert not [k for k in env if re.search(r"KEY|SECRET|TOKEN|PASSWORD|CONNECTION", k, re.I)], env.keys()
    assert not [k for k in env if k.upper().startswith(("FOUNDRY_", "AGENT_"))], "platform-reserved prefix"
    url = env["DATABASE_URL"]
    assert url == f"postgresql+psycopg://ops-orchestrator@{_FACTS.pg_host}:5432/ops_triage?sslmode=require", url
    assert not re.search(r"://[^/@]*:[^/@]*@", url), "a password in the URL would be a secret in the agent's env"
    urls = dict(e.split("=", 1) for e in env["SPECIALIST_URLS"].split(","))
    assert set(urls) == set(SPECIALIST_ROLES)
    for role, u in urls.items():
        assert u == f"{_FACTS.project_endpoint}/agents/ops-{role}/endpoint/protocols/invocations?api-version=v1"
    print("test_orchestrator_env_carries_no_secrets_and_no_database_password: PASS")


def test_the_url_the_orchestrator_is_given_is_parsed_correctly_by_its_own_settings() -> None:
    from config import Settings

    cfg = Settings(_env_file=None, specialist_urls=_ORCH.env["SPECIALIST_URLS"])
    assert set(cfg.specialist_url_map) == set(SPECIALIST_ROLES), "a '?api-version=v1' in a URL must not break role=url parsing"
    print("test_the_url_the_orchestrator_is_given_is_parsed_correctly_by_its_own_settings: PASS")


def test_a_login_name_with_an_at_sign_is_quoted_in_the_database_url() -> None:
    assert deploy.pg_database_url(_FACTS, "a.b@example.com").startswith("postgresql+psycopg://a.b%40example.com@")
    print("test_a_login_name_with_an_at_sign_is_quoted_in_the_database_url: PASS")


def test_postgres_has_no_password_and_no_open_firewall() -> None:
    steps = {s.key: s for s in deploy.build_plan(_FACTS, _ORCH, "99999999-9999-9999-9999-999999999999")}
    create = steps["create_postgres"].command
    assert create[create.index("--password-auth") + 1] == "Disabled"
    assert create[create.index("--microsoft-entra-auth") + 1] == "Enabled"
    assert create[create.index("--public-access") + 1] == "None", "no firewall rule may be created implicitly"
    assert create[create.index("--sku-name") + 1] == "Standard_B1ms" and create[create.index("--storage-size") + 1] == "32"
    assert "~$19/month" in steps["create_postgres"].cost, "the cost is the reason this step is gated"
    for key in ("firewall_azure_services", "firewall_your_ip"):
        cmd = steps[key].command
        assert "255.255.255.255" not in cmd, "never open the database to the whole internet"
    mine = steps["firewall_your_ip"].command
    assert mine[mine.index("--start-ip-address") + 1] == mine[mine.index("--end-ip-address") + 1] == "203.0.113.7"
    admin = steps["set_postgres_entra_admin"].command
    assert admin[admin.index("--object-id") + 1] == _FACTS.deployer_object_id
    assert admin[admin.index("--display-name") + 1] == "dev.user@example.com"
    allow = steps["allow_pgvector"].command
    assert allow[allow.index("--name") + 1] == "azure.extensions" and allow[allow.index("--value") + 1] == "VECTOR"
    print("test_postgres_has_no_password_and_no_open_firewall: PASS")


def test_a_zone_can_be_pinned_after_a_capacity_failure() -> None:
    def create_cmd(facts):
        return {s.key: s for s in deploy.build_plan(facts, _ORCH)}["create_postgres"].command

    assert "--zone" not in create_cmd(_FACTS), "by default Azure chooses the zone"
    pinned = create_cmd(deploy.Facts(**{**_FACTS.__dict__, "pg_zone": "2"}))
    assert pinned[pinned.index("--zone") + 1] == "2" and "--yes" in pinned
    print("test_a_zone_can_be_pinned_after_a_capacity_failure: PASS")


def test_the_firewall_step_for_your_ip_refuses_to_guess() -> None:
    no_ip = deploy.Facts(**{**_FACTS.__dict__, "client_ip": ""})
    try:
        deploy.apply_step(no_ip, _ORCH, "firewall_your_ip", ask=lambda _: "y", run=lambda *a, **k: None)
        raise AssertionError("expected a refusal")
    except SystemExit as e:
        assert "--client-ip" in str(e)
    print("test_the_firewall_step_for_your_ip_refuses_to_guess: PASS")


def test_database_grants_are_data_access_only() -> None:
    grants = deploy.database_grant_sql("ops-orchestrator")
    text = "\n".join(grants).upper()
    for forbidden in ("CREATE ON", "SUPERUSER", "ALL PRIVILEGES", "OWNER", "CREATEROLE", "WITH GRANT OPTION"):
        assert forbidden not in text, f"{forbidden} is more than the orchestrator needs"
    assert "GRANT TRUNCATE ON CHECKPOINTS, CHECKPOINT_BLOBS, CHECKPOINT_WRITES" in text, "reset empties only these"
    assert not [g for g in grants if "TRUNCATE" in g.upper() and "CHECKPOINT" not in g.upper()]
    print("test_database_grants_are_data_access_only: PASS")


def test_the_grant_step_runs_exactly_the_sql_it_displayed() -> None:
    import psycopg

    executed: list[tuple] = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            executed.append((sql, params))
            return self
        def fetchone(self): return None  # role does not exist yet

    original, tokens = psycopg.connect, deploy
    psycopg.connect = lambda **kw: (executed.append(("CONNECT", kw["user"], kw["sslmode"])), _Conn())[1]
    import db.entra as entra
    real_token, entra.database_token = entra.database_token, lambda: "tok"
    try:
        deploy._grant_database_access(_FACTS, _ORCH, "99999999-9999-9999-9999-999999999999")
    finally:
        psycopg.connect, entra.database_token = original, real_token
    assert executed[0] == ("CONNECT", "dev.user@example.com", "require")
    sql_run = [e[0] for e in executed[1:]]
    shown = deploy.database_grant_sql("ops-orchestrator")
    assert sql_run[-(len(shown) - 1):] == shown[1:], "what the gate displayed must be exactly what ran"
    mapping = [e for e in executed if "pgaadauth_create_principal_with_oid" in e[0]]
    assert mapping and mapping[0][1] == ("ops-orchestrator", "99999999-9999-9999-9999-999999999999")
    print("test_the_grant_step_runs_exactly_the_sql_it_displayed: PASS")


def test_migrate_and_seed_connects_keyless_as_the_admin() -> None:
    calls = []
    deploy._migrate_and_seed(_FACTS, lambda cmd, **kw: calls.append((cmd, kw)))
    modules = [c[0][2:] for c in calls]
    assert modules == [["alembic", "upgrade", "head"], ["scripts.init_checkpointer"], ["scripts.seed"]], modules
    for _, kw in calls:
        env = kw["env"]
        assert env["DATABASE_AUTH_MODE"] == "entra" and env["CHECKPOINTER_AUTO_SETUP"] == "true"
        assert env["DATABASE_URL"] == deploy.pg_database_url(_FACTS, "dev.user@example.com")
        assert kw["cwd"] == deploy.BACKEND_DIR
    print("test_migrate_and_seed_connects_keyless_as_the_admin: PASS")


def test_the_orchestrator_smoke_payload_is_read_only() -> None:
    assert deploy._smoke_payload(_ORCH) == {"action": "list"}
    print("test_the_orchestrator_smoke_payload_is_read_only: PASS")


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


def test_agent_model_access_is_least_privilege_and_shows_the_exact_command() -> None:
    pid = "33333333-3333-3333-3333-333333333333"
    steps = {s.key: s for s in deploy.build_plan(_FACTS, _SPEC, pid)}
    cmd = steps["grant_agent_model_access"].command
    assert cmd[cmd.index("--assignee-object-id") + 1] == pid
    assert cmd[cmd.index("--role") + 1] == "Cognitive Services OpenAI User", "least privilege: inference only"
    assert cmd[cmd.index("--scope") + 1] == _FACTS.account_id and cmd[-1].endswith("/accounts/proj-app")
    joined = " ".join(cmd)
    assert "Foundry User" not in joined and "Owner" not in joined and "Contributor" not in joined

    # the gate shows the resolved identity, and still runs nothing without "y"
    ran: list = []
    lookup = lambda f, s: pid  # noqa: E731
    for answer in ("", "n"):
        assert deploy.apply_step(_FACTS, _SPEC, "grant_agent_model_access", ask=lambda _p, a=answer: a,
                                 run=lambda *a, **k: ran.append(a), lookup_principal=lookup) is False
    assert ran == []
    ok = deploy.apply_step(_FACTS, _SPEC, "grant_agent_model_access", ask=lambda _p: "y",
                           run=lambda *a, **k: ran.append(a), lookup_principal=lookup)
    assert ok and ran[0][0][:4] == ["az", "role", "assignment", "create"] and pid in ran[0][0]
    print("test_agent_model_access_is_least_privilege_and_shows_the_exact_command: PASS")


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
    test_orchestrator_plan_is_ordered_and_reuses_the_registry()
    test_orchestrator_env_carries_no_secrets_and_no_database_password()
    test_the_url_the_orchestrator_is_given_is_parsed_correctly_by_its_own_settings()
    test_a_login_name_with_an_at_sign_is_quoted_in_the_database_url()
    test_postgres_has_no_password_and_no_open_firewall()
    test_a_zone_can_be_pinned_after_a_capacity_failure()
    test_the_firewall_step_for_your_ip_refuses_to_guess()
    test_database_grants_are_data_access_only()
    test_the_grant_step_runs_exactly_the_sql_it_displayed()
    test_migrate_and_seed_connects_keyless_as_the_admin()
    test_the_orchestrator_smoke_payload_is_read_only()
    test_nothing_runs_without_an_explicit_yes()
    test_apply_runs_exactly_one_step_and_rejects_unknown_steps()
    test_build_context_does_not_depend_on_the_working_directory()
    test_agent_model_access_is_least_privilege_and_shows_the_exact_command()
    test_a_stalled_az_lookup_fails_loudly_instead_of_hanging()
    test_an_explicit_tag_pins_the_image()
    test_a_failing_az_command_exits_cleanly_without_a_traceback()
    test_rendered_commands_are_copy_pasteable()
    test_rendered_plan_is_honest_about_its_state()
    test_every_smoke_payload_is_accepted_by_its_specialist()
