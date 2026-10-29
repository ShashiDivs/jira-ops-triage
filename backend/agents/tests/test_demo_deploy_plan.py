"""Unit tests for the public-demo deploy tool (hosted/demo.py). No Azure, no network: the plan is built from
fixed facts and every command is recorded, not run. What matters most is the safety design — nothing runs
without a "y", secrets never appear where a command is displayed, and `down` can only ever delete the demo's
own tagged resource group.
"""
import re
import shlex

from hosted import demo, deploy

_FACTS = deploy.Facts(
    subscription_id="00000000-aaaa-bbbb-cccc-111111111111",
    deployer_object_id="11111111-1111-1111-1111-111111111111",
    project_principal_id="22222222-2222-2222-2222-222222222222",
    project_endpoint="https://proj-app.services.ai.azure.com/api/projects/proj-app",
    git_tag="abc1234",
)
_OPTS = demo.DemoOptions()
_RT = demo.Runtime("99999999-9999-9999-9999-999999999999", "cccccccc-cccc-cccc-cccc-cccccccccccc",
                   demo._identity_id(_FACTS), "ops-demo-db.internal.example.azurecontainerapps.io")
_PASSCODE, _DBPASS = "correct horse battery", "dbpass-XYZ-123"
_ORDER = ["register_app_provider", "create_resource_group", "create_identity", "grant_registry_pull",
          "grant_model_access", "build_image", "import_postgres_image", "create_environment",
          "create_database_app", "create_web_app", "smoke_test", "update_web_app_image"]


def _steps(opts=_OPTS, rt=_RT):
    return {s.key: s for s in demo.build_plan(_FACTS, opts, rt)}


def test_plan_is_ordered_and_complete() -> None:
    assert [s.key for s in demo.build_plan(_FACTS, _OPTS, _RT)] == _ORDER
    print("test_plan_is_ordered_and_complete: PASS")


def test_everything_it_creates_lives_in_its_own_resource_group() -> None:
    assert demo.DEMO_RG != deploy.RESOURCE_GROUP
    for key, step in _steps().items():
        cmd = step.command or []
        if "--resource-group" in cmd:
            assert cmd[cmd.index("--resource-group") + 1] == demo.DEMO_RG, f"{key} would create something outside the demo group"
    print("test_everything_it_creates_lives_in_its_own_resource_group: PASS")


def test_the_group_is_tagged_so_down_can_recognise_it() -> None:
    cmd = _steps()["create_resource_group"].command
    assert cmd[cmd.index("--tags") + 1] == "purpose=temporary-demo"
    print("test_the_group_is_tagged_so_down_can_recognise_it: PASS")


def test_roles_are_least_privilege_and_scoped() -> None:
    s = _steps()
    pull = s["grant_registry_pull"].command
    assert pull[pull.index("--role") + 1] == "Container Registry Repository Reader" and pull[-1] == _FACTS.acr_id
    model = s["grant_model_access"].command
    assert model[model.index("--role") + 1] == "Cognitive Services OpenAI User" and model[-1] == _FACTS.account_id
    for cmd in (pull, model):
        assert cmd[cmd.index("--assignee-object-id") + 1] == _RT.identity_principal_id
        assert "Owner" not in cmd and "Contributor" not in " ".join(cmd)
    print("test_roles_are_least_privilege_and_scoped: PASS")


def test_the_database_is_internal_only_and_always_on() -> None:
    c = _steps()["create_database_app"].command
    assert c[c.index("--ingress") + 1] == "internal", "the database must never be reachable from the internet"
    assert c[c.index("--transport") + 1] == "tcp"
    assert c[c.index("--min-replicas") + 1] == "1" and c[c.index("--max-replicas") + 1] == "1"
    assert c[c.index("--image") + 1].startswith(f"{_FACTS.acr_name}.azurecr.io/"), "pull from our registry, not Docker Hub"
    print("test_the_database_is_internal_only_and_always_on: PASS")


def test_the_app_is_public_single_replica_and_capped() -> None:
    c = _steps()["create_web_app"].command
    assert c[c.index("--ingress") + 1] == "external" and c[c.index("--target-port") + 1] == "8000"
    assert c[c.index("--min-replicas") + 1] == "1" and c[c.index("--max-replicas") + 1] == "1", "the limiter is in-memory: ONE process"
    env = dict(e.split("=", 1) for e in c[c.index("--env-vars") + 1:c.index("--output")])
    assert env["TICKET_SUBMISSION_CAP"] == "30" and env["TICKET_RATE_LIMIT_PER_MINUTE"] == "5"
    assert env["SCHEDULER_ENABLED"] == "false" and env["AZURE_AUTH_MODE"] == "entra"
    assert env["AZURE_CLIENT_ID"] == _RT.identity_client_id, "DefaultAzureCredential must be told which managed identity"
    assert "SPECIALIST_URLS" not in env, "hosted specialists are opt-in"
    print("test_the_app_is_public_single_replica_and_capped: PASS")


def test_secrets_are_never_displayed_and_only_travel_as_secretrefs() -> None:
    for key in ("create_database_app", "create_web_app"):
        shown = shlex.join(_steps()[key].command)
        for secret in (_PASSCODE, _DBPASS):
            assert secret not in shown
        assert demo.SECRET_PLACEHOLDER in shown or demo.PASSCODE_PLACEHOLDER in shown
    web = demo.web_app_command(_FACTS, _OPTS, _RT, db_password=_DBPASS, passcode=_PASSCODE)
    env_args = web[web.index("--env-vars") + 1:web.index("--output")]
    assert not [e for e in env_args if _PASSCODE in e or _DBPASS in e], "a secret leaked into a plain env var"
    assert {"DATABASE_URL=secretref:database-url", "DEMO_PASSCODE=secretref:demo-passcode"} <= set(env_args)
    secrets_args = web[web.index("--secrets") + 1:web.index("--env-vars")]
    assert any(_PASSCODE in a for a in secrets_args) and any(_DBPASS in a for a in secrets_args)
    assert web[-2:] == ["--output", "none"], "az must not echo the created app (and its secrets) back"
    print("test_secrets_are_never_displayed_and_only_travel_as_secretrefs: PASS")


def test_the_executed_command_equals_the_displayed_one_except_for_secrets() -> None:
    shown = demo.web_app_command(_FACTS, _OPTS, _RT)
    real = demo.web_app_command(_FACTS, _OPTS, _RT, db_password=_DBPASS, passcode=_PASSCODE)
    swapped = [a.replace(_DBPASS, demo.SECRET_PLACEHOLDER).replace(_PASSCODE, demo.PASSCODE_PLACEHOLDER) for a in real]
    assert swapped == shown
    print("test_the_executed_command_equals_the_displayed_one_except_for_secrets: PASS")


def test_the_passcode_is_the_users_own_and_never_invented() -> None:
    try:
        demo.read_passcode({}, dotenv_path=None)
        raise AssertionError("must refuse when no passcode is set")
    except SystemExit as e:
        assert "DEMO_DEPLOY_PASSCODE" in str(e) and "never generated" in str(e)
    try:
        demo.read_passcode({"DEMO_DEPLOY_PASSCODE": "short"}, dotenv_path=None)
        raise AssertionError("a weak passcode must be refused")
    except SystemExit:
        pass
    assert demo.read_passcode({"DEMO_DEPLOY_PASSCODE": _PASSCODE}, dotenv_path=None) == _PASSCODE
    print("test_the_passcode_is_the_users_own_and_never_invented: PASS")


def test_nothing_runs_without_an_explicit_yes_and_secrets_are_read_late() -> None:
    ran, asked = [], []
    kw = dict(lookup=lambda f, need_db=False: _RT, read_db_password=lambda: _DBPASS, new_password=lambda: _DBPASS,
              passcode_source=lambda: _PASSCODE, run=lambda cmd, **k: ran.append(cmd))
    for answer in ("", "n", "yes please", "Y!"):
        assert demo.apply_step(_FACTS, _OPTS, "create_web_app", ask=lambda p, a=answer: asked.append(p) or a, **kw) is False
    assert ran == [], "only an exact 'y' may run a step"
    assert demo.apply_step(_FACTS, _OPTS, "create_web_app", ask=lambda p: "y", **kw) is True
    assert len(ran) == 1 and _PASSCODE in " ".join(ran[0]) and _DBPASS in " ".join(ran[0])
    print("test_nothing_runs_without_an_explicit_yes_and_secrets_are_read_late: PASS")


def test_a_missing_passcode_stops_before_the_gate() -> None:
    def no_passcode() -> str:
        raise SystemExit("set DEMO_DEPLOY_PASSCODE")

    prompts = []
    try:
        demo.apply_step(_FACTS, _OPTS, "create_web_app", ask=lambda p: prompts.append(p) or "y",
                        lookup=lambda f, need_db=False: _RT, passcode_source=no_passcode, run=lambda *a, **k: None)
        raise AssertionError("expected a refusal")
    except SystemExit:
        assert prompts == [], "the user must not be asked to approve a step that cannot run"
    print("test_a_missing_passcode_stops_before_the_gate: PASS")


def test_the_database_password_is_generated_only_when_the_database_is_created() -> None:
    ran = []
    generated = []
    demo.apply_step(_FACTS, _OPTS, "create_database_app", ask=lambda p: "y", lookup=lambda f, need_db=False: _RT,
                    new_password=lambda: generated.append(1) or _DBPASS, run=lambda cmd, **k: ran.append(cmd))
    assert generated == [1] and _DBPASS in " ".join(ran[0])
    ran.clear()
    demo.apply_step(_FACTS, _OPTS, "create_environment", ask=lambda p: "y", new_password=lambda: generated.append(1) or "x",
                    run=lambda cmd, **k: ran.append(cmd))
    assert generated == [1], "no other step may generate a password"
    print("test_the_database_password_is_generated_only_when_the_database_is_created: PASS")


def test_hosted_specialists_are_opt_in_and_say_what_they_need() -> None:
    opt = _steps(demo.DemoOptions(hosted_specialists=True))
    assert "grant_invoke_specialists" in opt and "grant_invoke_specialists" not in _steps()
    role = opt["grant_invoke_specialists"].command
    assert role[role.index("--role") + 1] == "Foundry User" and role[-1] == _FACTS.project_id
    assert "broad" in opt["grant_invoke_specialists"].detail
    web = demo.web_app_command(_FACTS, demo.DemoOptions(hosted_specialists=True), _RT)
    env = dict(e.split("=", 1) for e in web[web.index("--env-vars") + 1:web.index("--output")])
    assert env["SPECIALIST_AUTH"] == "entra" and set(
        e.split("=", 1)[0] for e in env["SPECIALIST_URLS"].split(",")) == set(deploy.SPECIALIST_ROLES)
    print("test_hosted_specialists_are_opt_in_and_say_what_they_need: PASS")


def test_costs_are_stated_where_money_is_spent() -> None:
    s = _steps()
    for key in ("create_database_app", "create_web_app"):
        assert re.search(r"\$\d", s[key].cost), f"{key} must state a cost"
    assert "$1-4" in s["create_web_app"].cost
    print("test_costs_are_stated_where_money_is_spent: PASS")


def test_the_rendered_plan_names_no_secret_and_commands_are_copy_pasteable() -> None:
    text = demo.render_plan(_FACTS, _OPTS)
    assert _PASSCODE not in text and _DBPASS not in text
    for line in text.splitlines():
        if line.strip().startswith("command : az "):
            shlex.split(line.split("command :", 1)[1])
    assert "Nothing below has been run" in text
    print("test_the_rendered_plan_names_no_secret_and_commands_are_copy_pasteable: PASS")


class _FakeAz:
    """A scripted `az`: answers by the leading words of the call, records nothing it should not."""
    def __init__(self, exists=True, tag="temporary-demo", resources=("ops-demo-identity", "ops-demo-app"), assignments=("A1", "A2")):
        self.exists, self.tag, self.resources, self.assignments = exists, tag, list(resources), list(assignments)
        self.deleted = False

    def __call__(self, *args: str) -> str:
        if args[:2] == ("group", "exists"):
            return "false" if (not self.exists or self.deleted) else "true"
        if args[:2] == ("group", "show"):
            return self.tag
        if args[:2] == ("resource", "list"):
            return " ".join(self.resources)
        if args[:2] == ("identity", "show"):
            return "99999999-9999-9999-9999-999999999999"
        if args[:3] == ("role", "assignment", "list"):
            return " ".join(self.assignments)
        raise AssertionError(f"unexpected lookup: {args}")


def test_down_deletes_assignments_then_only_the_demo_group_after_one_yes() -> None:
    fake, ran = _FakeAz(), []

    def run(cmd, **kw):
        ran.append(cmd)
        if cmd[:3] == ["az", "group", "delete"]:
            fake.deleted = True

    prompts = []
    assert demo.down(_FACTS, ask=lambda p: prompts.append(p) or "y", run=run, az=fake, poll_seconds=5) is True
    assert len(prompts) == 1, "one confirmation"
    assert ran[0] == ["az", "role", "assignment", "delete", "--ids", "A1", "A2"], "assignments first, so none are orphaned"
    assert ran[1] == ["az", "group", "delete", "--name", "ops-triage-demo", "--yes", "--no-wait"]
    assert all(deploy.RESOURCE_GROUP not in " ".join(c) for c in ran), "must never name the Foundry resource group"
    print("test_down_deletes_assignments_then_only_the_demo_group_after_one_yes: PASS")


def test_down_does_nothing_without_a_yes() -> None:
    ran = []
    assert demo.down(_FACTS, ask=lambda p: "no", run=lambda c, **k: ran.append(c), az=_FakeAz()) is False
    assert ran == []
    print("test_down_does_nothing_without_a_yes: PASS")


def test_down_refuses_a_group_that_is_not_tagged_as_the_demos() -> None:
    ran = []
    for tag in ("", "production", "temporary-demo-ish"):
        try:
            demo.down(_FACTS, ask=lambda p: "y", run=lambda c, **k: ran.append(c), az=_FakeAz(tag=tag))
            raise AssertionError(f"tag {tag!r} must be refused")
        except SystemExit as e:
            assert "refusing" in str(e)
    assert ran == []
    print("test_down_refuses_a_group_that_is_not_tagged_as_the_demos: PASS")


def test_down_is_a_no_op_when_the_demo_is_already_gone() -> None:
    ran = []
    assert demo.down(_FACTS, ask=lambda p: "y", run=lambda c, **k: ran.append(c), az=_FakeAz(exists=False)) is False
    assert ran == []
    assert "DOWN" in demo.status(_FakeAz(exists=False))
    print("test_down_is_a_no_op_when_the_demo_is_already_gone: PASS")


def test_the_image_build_uses_the_repo_root_and_the_demo_dockerfile() -> None:
    cmd = _steps()["build_image"].command
    assert cmd[cmd.index("--file") + 1] == "Dockerfile.demo" and cmd[-1] == str(demo.REPO_DIR)
    assert (demo.REPO_DIR / "Dockerfile.demo").exists() and (demo.REPO_DIR / ".dockerignore").exists()
    ignore = (demo.REPO_DIR / ".dockerignore").read_text()
    lines = [l.strip() for l in ignore.splitlines()]
    # `az acr build` packs the context with its own matcher, where "**/.env" misses the TOP-LEVEL .env (the first
    # real build uploaded the root .env to the registry's build storage). The plain spellings are required.
    assert ".env" in lines and ".env.*" in lines and "**/.env" in lines, "exclude .env at the top level AND below"
    assert "!.env.example" in lines
    assert "rm -f /app/.env" in (demo.REPO_DIR / "Dockerfile.demo").read_text(), "second guard against a stray .env"
    print("test_the_image_build_uses_the_repo_root_and_the_demo_dockerfile: PASS")


def test_the_image_build_runs_from_the_repo_root() -> None:
    """The first live run failed with "Unable to find 'Dockerfile.demo'": az looks for --file in the CURRENT
    directory, and this tool is run from backend/ while the Dockerfile is at the repo root."""
    seen = []
    demo.apply_step(_FACTS, _OPTS, "build_image", ask=lambda p: "y", run=lambda cmd, **kw: seen.append((cmd, kw)))
    assert len(seen) == 1 and seen[0][1].get("cwd") == demo.REPO_DIR
    assert (seen[0][1]["cwd"] / seen[0][0][seen[0][0].index("--file") + 1]).is_file(), "az must find the Dockerfile from that directory"
    others = []
    demo.apply_step(_FACTS, _OPTS, "create_environment", ask=lambda p: "y", run=lambda cmd, **kw: others.append(kw))
    assert len(others) == 1 and "cwd" not in others[0], "only the image build needs a working directory"
    print("test_the_image_build_runs_from_the_repo_root: PASS")


def test_rolling_out_a_new_image_changes_only_the_image() -> None:
    cmd = _steps()["update_web_app_image"].command
    assert cmd[:4] == ["az", "containerapp", "update", "--name"] and cmd[4] == "ops-demo-app"
    assert cmd[cmd.index("--resource-group") + 1] == demo.DEMO_RG
    assert cmd[cmd.index("--image") + 1] == demo.demo_image(_FACTS)
    for forbidden in ("--set-env-vars", "--replace-env-vars", "--secrets", "--min-replicas", "--cpu", "--ingress", "--remove-env-vars"):
        assert forbidden not in cmd, f"a code fix must not alter the live app's settings ({forbidden})"
    assert cmd[-2:] == ["--output", "none"]
    print("test_rolling_out_a_new_image_changes_only_the_image: PASS")


def test_the_app_reaches_the_database_by_its_short_name() -> None:
    """The first live run used the long ".internal.<domain>" name and the app timed out connecting (ActivationFailed).
    Apps in one environment reach a TCP app by its short name and exposed port."""
    answers = {("identity", "show"): "99999999-9999-9999-9999-999999999999\tcccccccc-cccc-cccc-cccc-cccccccccccc",
               ("containerapp", "show"): "ops-demo-db.internal.something.azurecontainerapps.io"}
    original = deploy._az
    deploy._az = lambda *a: answers[a[:2]]
    try:
        rt = demo._lookup_runtime(_FACTS, need_db=True)
    finally:
        deploy._az = original
    assert rt.db_host == "ops-demo-db" and "." not in rt.db_host
    web = demo.web_app_command(_FACTS, _OPTS, rt, db_password=_DBPASS, passcode=_PASSCODE)
    url = next(a for a in web if a.startswith("database-url="))
    assert url == f"database-url=postgresql+psycopg://ops_triage:{_DBPASS}@ops-demo-db:5432/ops_triage", url
    exposed = demo.database_app_command(_FACTS, rt)
    assert exposed[exposed.index("--exposed-port") + 1] == "5432", "the short-name address needs the exposed port"
    print("test_the_app_reaches_the_database_by_its_short_name: PASS")


def test_enabling_hosted_specialists_on_a_running_app_changes_only_its_environment() -> None:
    assert "enable_hosted_specialists" not in _steps(), "off unless --hosted-specialists is given"
    plan = demo.build_plan(_FACTS, demo.DemoOptions(hosted_specialists=True), _RT)
    keys = [s.key for s in plan]
    assert keys[-1] == "enable_hosted_specialists" and keys.index("grant_invoke_specialists") < keys.index("create_web_app")
    cmd = plan[-1].command
    assert cmd[:4] == ["az", "containerapp", "update", "--name"] and cmd[4] == "ops-demo-app"
    assert cmd[cmd.index("--resource-group") + 1] == demo.DEMO_RG
    for forbidden in ("--image", "--secrets", "--replace-env-vars", "--min-replicas", "--cpu", "--ingress"):
        assert forbidden not in cmd, f"the live app must not be altered by {forbidden}"
    env = dict(e.split("=", 1) for e in cmd[cmd.index("--set-env-vars") + 1:cmd.index("--output")])
    assert set(env) == {"SPECIALIST_AUTH", "SPECIALIST_URLS"} and env["SPECIALIST_AUTH"] == "entra"
    assert cmd[-2:] == ["--output", "none"]
    from config import Settings

    parsed = Settings(_env_file=None, specialist_urls=env["SPECIALIST_URLS"]).specialist_url_map
    assert set(parsed) == set(deploy.SPECIALIST_ROLES), "the app must parse every role from the value we set"
    for role, url in parsed.items():
        assert url == deploy.specialist_invocation_url(_FACTS, role)
    print("test_enabling_hosted_specialists_on_a_running_app_changes_only_its_environment: PASS")


if __name__ == "__main__":
    test_rolling_out_a_new_image_changes_only_the_image()
    test_enabling_hosted_specialists_on_a_running_app_changes_only_its_environment()
    test_the_app_reaches_the_database_by_its_short_name()
    test_the_image_build_runs_from_the_repo_root()
    test_plan_is_ordered_and_complete()
    test_everything_it_creates_lives_in_its_own_resource_group()
    test_the_group_is_tagged_so_down_can_recognise_it()
    test_roles_are_least_privilege_and_scoped()
    test_the_database_is_internal_only_and_always_on()
    test_the_app_is_public_single_replica_and_capped()
    test_secrets_are_never_displayed_and_only_travel_as_secretrefs()
    test_the_executed_command_equals_the_displayed_one_except_for_secrets()
    test_the_passcode_is_the_users_own_and_never_invented()
    test_nothing_runs_without_an_explicit_yes_and_secrets_are_read_late()
    test_a_missing_passcode_stops_before_the_gate()
    test_the_database_password_is_generated_only_when_the_database_is_created()
    test_hosted_specialists_are_opt_in_and_say_what_they_need()
    test_costs_are_stated_where_money_is_spent()
    test_the_rendered_plan_names_no_secret_and_commands_are_copy_pasteable()
    test_down_deletes_assignments_then_only_the_demo_group_after_one_yes()
    test_down_does_nothing_without_a_yes()
    test_down_refuses_a_group_that_is_not_tagged_as_the_demos()
    test_down_is_a_no_op_when_the_demo_is_already_gone()
    test_the_image_build_uses_the_repo_root_and_the_demo_dockerfile()
    print("All demo deploy plan tests passed.")
