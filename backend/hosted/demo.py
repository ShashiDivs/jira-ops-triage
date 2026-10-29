"""Put the whole app on the internet for a demo — then take it all down again.

    python -m hosted.demo plan                      # read-only: every step, its command, cost and undo
    python -m hosted.demo apply --step <key>        # ONE step, behind a y/N gate
    python -m hosted.demo status                    # what exists right now, and the URL
    python -m hosted.demo down                      # delete EVERYTHING the demo created (one y/N)

What it builds is Dockerfile.demo — the React app and the API in one container — plus a throwaway
Postgres container beside it, in Azure Container Apps. The Foundry project, the hosted specialist agents
and the container registry are NOT part of it and are never deleted: all the demo's own resources live in
one resource group (ops-triage-demo), so `down` is one delete and nothing keeps billing afterwards.

Same rules as hosted.deploy, because every step spends money or changes permissions: `plan` only reads
Azure; `apply` runs exactly one named step and does nothing unless you answer "y"; there is no run-all.

Secrets: the database password is generated here at run time and never printed — it lives only in the
Container Apps secret store. The demo passcode is NOT generated for you: put `DEMO_DEPLOY_PASSCODE=...` in
your own gitignored .env (or the environment) and it is read from there when the app is created. It is
never printed, and never appears in this file, the plan, or the image.

STATUS: the plan, ordering, gating, secret handling and teardown safety are unit-tested. The `az` commands
follow the CLI's own --help but had NOT been run against live Azure when this was written; the first
`apply` of each step is its first real test, which is why it is one confirmed step at a time.
"""
import argparse
import os
import secrets
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from hosted import deploy
from hosted.deploy import BACKEND_DIR, LOCATION, Facts, Step

REPO_DIR = BACKEND_DIR.parent  # Dockerfile.demo's build context: it needs frontend/ as well as backend/

DEMO_RG = "ops-triage-demo"
DEMO_TAG = ("purpose", "temporary-demo")  # `down` refuses to delete a group that does not carry this
ENV_NAME = "ops-demo-env"
DB_APP = "ops-demo-db"
WEB_APP = "ops-demo-app"
IDENTITY = "ops-demo-identity"
IMAGE_REPO = "ops-triage-demo"
PG_REPO_TAG = "pgvector:pg16"
PG_SOURCE = "docker.io/pgvector/pgvector:pg16"
DB_USER = DB_NAME = "ops_triage"
PASSCODE_VARIABLE = "DEMO_DEPLOY_PASSCODE"
MIN_PASSCODE_LENGTH = 8

# Shown in place of a secret wherever a command is displayed.
SECRET_PLACEHOLDER = "<generated at run time — never shown>"
PASSCODE_PLACEHOLDER = f"<from {PASSCODE_VARIABLE} in your .env — never shown>"


@dataclass(frozen=True)
class DemoOptions:
    ticket_cap: int = 30
    rate_per_minute: int = 5
    hosted_specialists: bool = False  # let the demo app call the four hosted specialists (needs a broad role)


@dataclass(frozen=True)
class Runtime:
    """Values that only exist once earlier steps have run (all looked up read-only, just before the gate)."""
    identity_principal_id: str = ""
    identity_client_id: str = ""
    identity_id: str = ""
    db_host: str = ""


def _identity_id(facts: Facts) -> str:
    return (f"/subscriptions/{facts.subscription_id}/resourceGroups/{DEMO_RG}"
            f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{IDENTITY}")


def _acr_server(facts: Facts) -> str:
    return f"{facts.acr_name}.azurecr.io"


def demo_image(facts: Facts) -> str:
    return f"{_acr_server(facts)}/{IMAGE_REPO}:demo-{facts.git_tag}"


def pg_image(facts: Facts) -> str:
    return f"{_acr_server(facts)}/{PG_REPO_TAG}"


def _base_env(facts: Facts, opts: DemoOptions) -> list[str]:
    """Plain (non-secret) settings for the app. KEY=VALUE pairs for --env-vars."""
    from config import get_settings

    env = {
        "TICKET_SUBMISSION_CAP": str(opts.ticket_cap),
        "TICKET_RATE_LIMIT_PER_MINUTE": str(opts.rate_per_minute),
        "SCHEDULER_ENABLED": "false",  # the generator would spend model calls on its own
        "RAG_MIN_SIMILARITY": "0.2",
        # Keyless: the container presents its managed identity to Foundry, so there is no API key to hold.
        **deploy._MODEL_ENV,
        "AZURE_AI_FOUNDRY_ENDPOINT": facts.project_endpoint,
        # Tells DefaultAzureCredential WHICH managed identity to use (the demo's user-assigned one).
        "AZURE_CLIENT_ID": "<the identity's client id — looked up at run time>",
        # The RBAC table (names and roles, not credentials). Empty would refuse every approval.
        "APPROVER_ROLES": get_settings().approver_roles,
    }
    pairs = [f"{k}={v}" for k, v in env.items()]
    return pairs + (specialist_env(facts) if opts.hosted_specialists else [])


def update_image_command(facts: Facts) -> list[str]:
    return ["az", "containerapp", "update", "--name", WEB_APP, "--resource-group", DEMO_RG,
            "--image", demo_image(facts), "--output", "none"]


def specialist_env(facts: Facts) -> list[str]:
    """What makes the app call the four hosted specialists instead of the models directly."""
    urls = ",".join(f"{r}={deploy.specialist_invocation_url(facts, r)}" for r in deploy.SPECIALIST_ROLES)
    return ["SPECIALIST_AUTH=entra", f"SPECIALIST_URLS={urls}"]


def enable_specialists_command(facts: Facts, opts: DemoOptions) -> list[str]:
    return ["az", "containerapp", "update", "--name", WEB_APP, "--resource-group", DEMO_RG,
            "--set-env-vars", *specialist_env(facts), "--output", "none"]


def database_app_command(facts: Facts, rt: Runtime, db_password: str | None = None) -> list[str]:
    ident = rt.identity_id or _identity_id(facts)
    return [
        "az", "containerapp", "create", "--name", DB_APP, "--resource-group", DEMO_RG, "--environment", ENV_NAME,
        "--image", pg_image(facts), "--user-assigned", ident,
        "--registry-server", _acr_server(facts), "--registry-identity", ident,
        # Reachable only from inside the environment — never from the internet.
        "--ingress", "internal", "--transport", "tcp", "--target-port", "5432", "--exposed-port", "5432",
        # One always-on copy: a scale-to-zero database would lose the paused approvals every time it slept.
        "--min-replicas", "1", "--max-replicas", "1", "--cpu", "0.5", "--memory", "1Gi",
        "--secrets", f"postgres-password={db_password or SECRET_PLACEHOLDER}",
        "--env-vars", f"POSTGRES_USER={DB_USER}", f"POSTGRES_DB={DB_NAME}", "POSTGRES_PASSWORD=secretref:postgres-password",
        "--output", "none",
    ]


def web_app_command(facts: Facts, opts: DemoOptions, rt: Runtime, db_password: str | None = None,
                    passcode: str | None = None) -> list[str]:
    ident = rt.identity_id or _identity_id(facts)
    database_url = (f"postgresql+psycopg://{DB_USER}:{db_password or SECRET_PLACEHOLDER}"
                    f"@{rt.db_host or '<the database app address — looked up at run time>'}:5432/{DB_NAME}")
    env = [e.replace("<the identity's client id — looked up at run time>", rt.identity_client_id or
                     "<the identity's client id — looked up at run time>") for e in _base_env(facts, opts)]
    return [
        "az", "containerapp", "create", "--name", WEB_APP, "--resource-group", DEMO_RG, "--environment", ENV_NAME,
        "--image", demo_image(facts), "--user-assigned", ident,
        "--registry-server", _acr_server(facts), "--registry-identity", ident,
        "--ingress", "external", "--target-port", "8000",
        # One always-on copy: the cost limiter and the pause/resume state assume a single server process.
        "--min-replicas", "1", "--max-replicas", "1", "--cpu", "1", "--memory", "2Gi",
        "--secrets", f"database-url={database_url}", f"demo-passcode={passcode or PASSCODE_PLACEHOLDER}",
        "--env-vars", "DATABASE_URL=secretref:database-url", "DEMO_PASSCODE=secretref:demo-passcode", *env,
        "--output", "none",
    ]


def build_plan(facts: Facts, opts: DemoOptions = DemoOptions(), rt: Runtime = Runtime()) -> list[Step]:
    principal = rt.identity_principal_id or "<the identity — looked up at run time>"
    acr, account, project = facts.acr_id, facts.account_id, facts.project_id
    steps = [
        Step(
            "register_app_provider",
            "Register the Container Apps resource provider (one-time, per subscription)",
            ["az", "provider", "register", "--namespace", "Microsoft.App", "--wait"],
            "Same opt-in the registry and Postgres needed: `az provider show --namespace Microsoft.App` said "
            "NotRegistered. SKIP if it already says Registered.",
            "a subscription-level registration (no resource is created)", "free",
            "az provider unregister --namespace Microsoft.App (only possible with no Container Apps)",
        ),
        Step(
            "create_resource_group",
            f"Create the demo's own resource group '{DEMO_RG}'",
            ["az", "group", "create", "--name", DEMO_RG, "--location", LOCATION,
             "--tags", f"{DEMO_TAG[0]}={DEMO_TAG[1]}"],
            "Everything the demo creates goes in here, so `down` is one delete. The Foundry project, the hosted "
            f"agents and the registry stay in {deploy.RESOURCE_GROUP} and are never touched. The tag is what `down` "
            "checks before it will delete anything.",
            f"1 empty resource group '{DEMO_RG}' in {LOCATION}", "free",
            f"az group delete --name {DEMO_RG} --yes   (or: python -m hosted.demo down)",
        ),
        Step(
            "create_identity",
            "Create an identity for the demo app",
            ["az", "identity", "create", "--name", IDENTITY, "--resource-group", DEMO_RG, "--location", LOCATION],
            "A user-assigned managed identity, created BEFORE the apps so it can be given its permissions first. "
            "It is how the container pulls its image and calls the models: no password or key is stored anywhere.",
            "1 managed identity", "free", "removed with the resource group",
        ),
        Step(
            "grant_registry_pull",
            "Let the demo identity pull images from the registry",
            ["az", "role", "assignment", "create", "--assignee-object-id", principal,
             "--assignee-principal-type", "ServicePrincipal", "--role", "Container Registry Repository Reader",
             "--scope", acr],
            "The registry is in ABAC mode (see docs/hosted-agents-deployment.md), where this is the role that "
            "grants pulls. Pull only — no push, no delete.",
            "1 role assignment for the demo identity, scoped to the registry", "free",
            "removed by `down` (the identity's role assignments are deleted before the group)",
        ),
        Step(
            "grant_model_access",
            "Let the demo identity call the models",
            ["az", "role", "assignment", "create", "--assignee-object-id", principal,
             "--assignee-principal-type", "ServicePrincipal", "--role", "Cognitive Services OpenAI User",
             "--scope", account],
            "Same least-privilege role the hosted agents use: inference only, on the one Foundry account. The app "
            "needs it for triage, routing, and for embedding the corpus at first start.",
            f"1 role assignment for the demo identity, scoped to the {deploy.FOUNDRY_ACCOUNT} account", "free",
            "removed by `down`",
        ),
    ]
    if opts.hosted_specialists:
        steps.append(Step(
            "grant_invoke_specialists",
            "Let the demo app call the four hosted specialist agents",
            ["az", "role", "assignment", "create", "--assignee-object-id", principal,
             "--assignee-principal-type", "ServicePrincipal", "--role", "Foundry User", "--scope", project],
            "OPTIONAL (--hosted-specialists). 'Foundry User' is broad (Microsoft.CognitiveServices/*) and is the "
            "role known to work for invoking agents; no narrower built-in role names an invoke action. Unverified "
            "for a managed identity. If a call is refused the app falls back to in-process model calls, so the "
            "demo still works — you just would not be showing the hosted agents answering.",
            "1 role assignment for the demo identity, scoped to the project", "free", "removed by `down`",
        ))
    steps += [
        Step(
            "build_image",
            "Build the demo image (React app + API) in the registry",
            ["az", "acr", "build", "--registry", facts.acr_name, "--platform", "linux/amd64",
             "--image", f"{IMAGE_REPO}:demo-{facts.git_tag}", "--file", "Dockerfile.demo",
             "--source-acr-auth-id", "[caller]", str(REPO_DIR)],
            "Built remotely, run from the repo root (Dockerfile.demo needs frontend/ and backend/; az looks for --file in the current directory). The root "
            ".dockerignore keeps .env out, and the Dockerfile deletes any .env as a second guard. Same ABAC "
            "requirements as the agent image build: you need push rights on the registry (granted earlier).",
            f"image {demo_image(facts)} (~640 MB)", "a few ACR build-minutes (cents) + registry storage; stays after `down`",
            f"az acr repository delete --name {facts.acr_name} --image {IMAGE_REPO}:demo-{facts.git_tag}",
        ),
        Step(
            "import_postgres_image",
            "Copy the pgvector Postgres image into the registry",
            ["az", "acr", "import", "--name", facts.acr_name, "--source", PG_SOURCE, "--image", PG_REPO_TAG],
            "Copies the public image server-side so the database container pulls from OUR registry, not from "
            "Docker Hub — Docker Hub throttles anonymous pulls from shared cloud addresses, which would make "
            "the demo fail at random. Unverified: whether an ABAC registry accepts the import with your role.",
            f"image {pg_image(facts)} (~400 MB)", "registry storage (cents); stays after `down`",
            f"az acr repository delete --name {facts.acr_name} --image {PG_REPO_TAG}",
        ),
        Step(
            "create_environment",
            "Create the Container Apps environment",
            ["az", "containerapp", "env", "create", "--name", ENV_NAME, "--resource-group", DEMO_RG,
             "--location", LOCATION, "--enable-workload-profiles", "false"],
            "The shared network + log sink the two containers run in. '--enable-workload-profiles false' picks the "
            "plain consumption environment, which has no per-environment fee. A Log Analytics workspace is created "
            "with it for the container logs.",
            "1 Container Apps environment + 1 Log Analytics workspace",
            "no environment fee; logs ~$2.99/GB ingested (this app writes a few MB)",
            "removed with the resource group",
        ),
        Step(
            "create_database_app",
            "Start the throwaway Postgres (pgvector) container",
            database_app_command(facts, rt),
            "Internal-only: no address on the internet. One always-on copy at 0.5 vCPU / 1 GiB with NO persistent "
            "disk, so its data dies with it — fine, the corpus is re-seeded at start and tickets are demo data. The "
            "password is generated here, never printed, and kept only as a Container Apps secret. The demo app's "
            "next step reads it back from Azure rather than from a file.",
            "1 Container App (the database)",
            "list price while running: active ~$0.054/h, idle ~$0.016/h (0.5 vCPU + 1 GiB)",
            "removed with the resource group",
        ),
        Step(
            "create_web_app",
            "Start the demo app and give it a public URL",
            web_app_command(facts, opts, rt),
            f"The React app and the API in one container, public over HTTPS, one always-on copy at 1 vCPU / 2 GiB. "
            f"Gated by your passcode (read from {PASSCODE_VARIABLE}, never shown) and capped at {opts.ticket_cap} "
            f"tickets in total, {opts.rate_per_minute}/minute per client. The scheduler is off. On start it runs the "
            "migrations and seeds the corpus (15 small embedding calls) before it answers, so the first start takes "
            "a minute or two. The identity from the earlier steps lets it pull its image and call the models with no key.",
            "1 Container App (the app) with a public *.azurecontainerapps.io address",
            "list price while running: active ~$0.108/h, idle ~$0.032/h (1 vCPU + 2 GiB); both containers always "
            "active is ~$0.16/h, so roughly $1-4 for a working day, plus the model calls the tickets make",
            "removed with the resource group",
        ),
        Step(
            "smoke_test",
            "Check the public URL",
            None,
            "Waits for the app to answer, then checks: the page loads, /api/gate says a passcode is required, and an "
            "API call WITHOUT the passcode is refused (401). It does not use the passcode. Because the app only "
            "starts after migrating and seeding, an answer also proves it reached its database and the models.",
            "nothing", "free", "n/a",
        ),
    ]
    steps.append(Step(
        "update_web_app_image",
        "Roll a newly built image onto the running app",
        update_image_command(facts),
        "For an app that is ALREADY running, after `build_image` made a new image (use the same --tag for both). "
        "Changes only the image: Azure starts a NEW revision and retires the old one once the new one is healthy, "
        "so the live site keeps answering and keeps its settings, secrets and database. Used for the first real fix "
        "(the checkpointer dropped its idle database connection and returned HTTP 500).",
        "one new revision of ops-demo-app (replaces the current one)", "no extra cost",
        "az containerapp update --image <the previous image> (see `az containerapp revision list`)",
    ))
    if opts.hosted_specialists:
        steps.append(Step(
            "enable_hosted_specialists",
            "Point the running demo app at the four hosted specialist agents",
            enable_specialists_command(facts, opts),
            "For an app that is ALREADY running (skip this if you created it with --hosted-specialists). Sets "
            "SPECIALIST_AUTH=entra and SPECIALIST_URLS on the app, which makes Azure start a NEW revision and retire "
            "the old one only once the new one is healthy — so the live site keeps answering through the change, and "
            "if the new revision fails to start, the old one stays up. The database is untouched, so tickets and "
            "pending approvals survive; the in-memory ticket counter restarts from zero. The first ticket after this "
            "(or after ~15 idle minutes) pays a 6-8 s cold start per specialist while its container starts.",
            "one new revision of ops-demo-app (replaces the current one)", "no extra cost; the specialists bill only while running",
            "az containerapp update with SPECIALIST_URLS and SPECIALIST_AUTH removed (--remove-env-vars), or `down`",
        ))
    return steps


# ---------------------------------------------------------------------------------------------
# Live lookups (read-only), secrets, rendering
# ---------------------------------------------------------------------------------------------

def read_passcode(environ: dict[str, str] | None = None, dotenv_path=BACKEND_DIR / ".env") -> str:
    """The passcode comes from the environment or your own .env — never generated, never printed."""
    environ = os.environ if environ is None else environ
    value = environ.get(PASSCODE_VARIABLE, "")
    if not value and dotenv_path is not None:
        try:
            from dotenv import dotenv_values

            value = dotenv_values(dotenv_path).get(PASSCODE_VARIABLE) or ""
        except Exception:
            value = ""
    if len(value) < MIN_PASSCODE_LENGTH:
        raise SystemExit(
            f"set {PASSCODE_VARIABLE} (at least {MIN_PASSCODE_LENGTH} characters) in your .env or environment first — "
            "you choose the passcode, so it is never generated or shown by this tool. Nothing was changed."
        )
    return value


def _lookup_runtime(facts: Facts, need_db: bool = False) -> Runtime:
    """Read-only: the identity created earlier (and, for the app step, the database's address)."""
    def az(*a: str) -> str:
        return deploy._az(*a)

    ident = az("identity", "show", "--name", IDENTITY, "--resource-group", DEMO_RG,
               "--query", "{p:principalId,c:clientId}", "-o", "tsv").split()
    if len(ident) != 2:
        raise SystemExit(f"the identity '{IDENTITY}' does not exist yet — run the create_identity step first")
    db_host = ""
    if need_db:
        exists = az("containerapp", "show", "--name", DB_APP, "--resource-group", DEMO_RG,
                    "--query", "properties.configuration.ingress.fqdn", "-o", "tsv")
        if not exists:
            raise SystemExit(f"the database app '{DB_APP}' has no address yet — run create_database_app first")
        # Apps in the same environment reach a TCP app by its SHORT name and exposed port. The long
        # `<app>.internal.<environment-domain>` name looked right but timed out on the first live run
        # ("connection timeout expired", app ActivationFailed), so it is deliberately NOT used.
        db_host = DB_APP
    return Runtime(ident[0], ident[1], _identity_id(facts), db_host)


def _stored_db_password() -> str:
    out = deploy._az("containerapp", "secret", "show", "--name", DB_APP, "--resource-group", DEMO_RG,
                     "--secret-name", "postgres-password", "--query", "value", "-o", "tsv")
    if not out:
        raise SystemExit("could not read the database password back from Azure — has create_database_app run?")
    return out


def render_plan(facts: Facts, opts: DemoOptions) -> str:
    out = [
        "DEMO PLAN — the whole app on a public URL, then gone again",
        f"  resource group : {DEMO_RG} (all demo resources; one delete removes them)",
        f"  app image      : {demo_image(facts)}",
        f"  limits         : {opts.ticket_cap} tickets total, {opts.rate_per_minute}/minute per client, scheduler off",
        "  Nothing below has been run. `plan` only reads Azure.",
        "",
    ]
    for i, s in enumerate(build_plan(facts, opts), 1):
        cmd = shlex.join(s.command) if s.command else "(SDK / REST call — see detail)"
        out += [f"[{i}] {s.key} — {s.title}", f"    command : {cmd}", f"    detail  : {s.detail}",
                f"    creates : {s.creates}", f"    cost    : {s.cost}", f"    undo    : {s.undo}", ""]
    out += [
        "Run one step at a time:  python -m hosted.demo apply --step <key>",
        "When you are finished:   python -m hosted.demo down",
        f"Before create_web_app: put {PASSCODE_VARIABLE}=<your passcode> in your .env (it is never shown).",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# apply — ONE step, behind a y/N gate
# ---------------------------------------------------------------------------------------------

def _smoke_test(fqdn: str, timeout_s: int = 300) -> None:
    import httpx

    base = f"https://{fqdn}"
    deadline, last = time.time() + timeout_s, "no answer yet"
    while time.time() < deadline:
        try:
            gate = httpx.get(f"{base}/api/gate", timeout=15)
            if gate.status_code == 200:
                break
            last = f"HTTP {gate.status_code}"
        except httpx.HTTPError as e:
            last = type(e).__name__
        print(f"  waiting for the app ({last}) ...")
        time.sleep(10)
    else:
        raise SystemExit(f"the app did not answer within {timeout_s}s (last: {last}). Look at its logs:\n"
                         f"  az containerapp logs show --name {WEB_APP} --resource-group {DEMO_RG} --tail 80")
    page = httpx.get(base, timeout=15)
    locked = httpx.get(f"{base}/api/tickets", timeout=15)
    required = gate.json().get("required")
    print(f"  page: HTTP {page.status_code}\n  gate says passcode required: {required}\n  API without passcode: HTTP {locked.status_code}")
    ok = page.status_code == 200 and required is True and locked.status_code == 401
    print(f"\nPUBLIC URL: {base}\n" if ok else "\nNOT HEALTHY — see above.")
    if not ok:
        raise SystemExit(1)


def apply_step(facts: Facts, opts: DemoOptions, key: str, ask: Callable[[str], str] = input,
               run: Callable[..., object] = subprocess.run, lookup: Callable[..., Runtime] = _lookup_runtime,
               read_db_password: Callable[[], str] = _stored_db_password,
               new_password: Callable[[], str] = lambda: secrets.token_urlsafe(24),
               passcode_source: Callable[[], str] = read_passcode) -> bool:
    """Runs ONE step if (and only if) the user types 'y'. The gate shows the command with every secret
    replaced by a placeholder; the real values are substituted only for the command that actually runs."""
    needs_identity = key in ("grant_registry_pull", "grant_model_access", "grant_invoke_specialists",
                             "create_database_app", "create_web_app")
    rt = lookup(facts, need_db=(key == "create_web_app")) if needs_identity else Runtime()
    steps = {s.key: s for s in build_plan(facts, opts, rt)}
    if key not in steps:
        raise SystemExit(f"unknown step '{key}' (expected one of: {', '.join(steps)})")
    step = steps[key]

    passcode = None
    if key == "create_web_app":
        passcode = passcode_source()  # fail BEFORE the gate if it is missing — and never display it

    print(f"\nSTEP {step.key}: {step.title}")
    print(f"  command : {shlex.join(step.command) if step.command else '(SDK / REST call)'}")
    print(f"  creates : {step.creates}\n  cost    : {step.cost}\n  undo    : {step.undo}")
    if not deploy._confirm("Run this step against your Azure subscription?", ask):
        print("not run.")
        return False

    command = step.command
    if key == "create_database_app":
        command = database_app_command(facts, rt, db_password=new_password())
    elif key == "create_web_app":
        command = web_app_command(facts, opts, rt, db_password=read_db_password(), passcode=passcode)

    if command:
        # `az acr build` looks for --file relative to the CURRENT directory, whatever its help says (the first
        # live run failed with "Unable to find 'Dockerfile.demo'" from backend/). So build from the repo root.
        extra = {"cwd": REPO_DIR} if key == "build_image" else {}
        deploy._run_or_exit(run, command, key, **extra)
    elif key == "smoke_test":
        fqdn = deploy._az("containerapp", "show", "--name", WEB_APP, "--resource-group", DEMO_RG,
                          "--query", "properties.configuration.ingress.fqdn", "-o", "tsv")
        _smoke_test(fqdn)
    return True


# ---------------------------------------------------------------------------------------------
# down — delete everything the demo made
# ---------------------------------------------------------------------------------------------

def down(facts: Facts, ask: Callable[[str], str] = input, run: Callable[..., object] = subprocess.run,
         az: Callable[..., str] = deploy._az, poll_seconds: int = 900) -> bool:
    """Deletes the demo's role assignments and then its resource group, after ONE y/N. Refuses to touch a
    group that is not the demo's own (wrong name, or missing the temporary-demo tag)."""
    exists = az("group", "exists", "--name", DEMO_RG)
    if exists.strip().lower() != "true":
        print(f"'{DEMO_RG}' does not exist — nothing to delete.")
        return False
    tag = az("group", "show", "--name", DEMO_RG, "--query", f"tags.{DEMO_TAG[0]}", "-o", "tsv").strip()
    if DEMO_RG == deploy.RESOURCE_GROUP or tag != DEMO_TAG[1]:
        raise SystemExit(f"refusing: '{DEMO_RG}' is not tagged {DEMO_TAG[0]}={DEMO_TAG[1]}, so it is not safe to treat as the demo's.")

    resources = az("resource", "list", "--resource-group", DEMO_RG, "--query", "[].name", "-o", "tsv").split()
    principal = az("identity", "show", "--name", IDENTITY, "--resource-group", DEMO_RG,
                   "--query", "principalId", "-o", "tsv").strip() if IDENTITY in resources else ""
    assignment_ids = az("role", "assignment", "list", "--assignee", principal, "--all", "--query", "[].id",
                        "-o", "tsv").split() if principal else []

    print(f"\nDOWN: delete everything the demo created\n  resource group : {DEMO_RG}\n  resources      : {', '.join(resources) or '(none)'}")
    print(f"  role assignments held by the demo identity (removed first, so none are left orphaned): {len(assignment_ids)}")
    print(f"  NOT touched    : {deploy.RESOURCE_GROUP} (Foundry project, hosted agents, registry) and the images in the registry")
    if not deploy._confirm(f"Permanently delete '{DEMO_RG}' and everything in it?", ask):
        print("not run.")
        return False

    if assignment_ids:
        deploy._run_or_exit(run, ["az", "role", "assignment", "delete", "--ids", *assignment_ids], "down: role assignments")
    deploy._run_or_exit(run, ["az", "group", "delete", "--name", DEMO_RG, "--yes", "--no-wait"], "down: resource group")
    deadline = time.time() + poll_seconds
    while time.time() < deadline:
        if az("group", "exists", "--name", DEMO_RG).strip().lower() != "true":
            print(f"deleted. '{DEMO_RG}' is gone and nothing from the demo is billing.")
            return True
        print("  still deleting ...")
        time.sleep(15)
    print(f"deletion is still in progress after {poll_seconds}s — check `az group exists --name {DEMO_RG}`; it continues in Azure.")
    return True


def status(az: Callable[..., str] = deploy._az) -> str:
    if az("group", "exists", "--name", DEMO_RG).strip().lower() != "true":
        return f"The demo is DOWN: '{DEMO_RG}' does not exist, so nothing is billing."
    resources = az("resource", "list", "--resource-group", DEMO_RG, "--query", "[].{n:name,t:type}", "-o", "tsv")
    fqdn = az("containerapp", "show", "--name", WEB_APP, "--resource-group", DEMO_RG,
              "--query", "properties.configuration.ingress.fqdn", "-o", "tsv") if WEB_APP in resources else ""
    lines = [f"The demo EXISTS in '{DEMO_RG}' (billing while the apps run):", *("  " + r for r in resources.splitlines())]
    lines.append(f"URL: https://{fqdn}" if fqdn else "URL: none yet (the app has not been created)")
    lines.append("Take it down with:  python -m hosted.demo down")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="hosted.demo", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply", "down", "status"):
        sp = sub.add_parser(name)
        if name in ("plan", "apply"):
            sp.add_argument("--tag", default=None, help="image tag to use instead of the current git commit")
            sp.add_argument("--cap", type=int, default=30, help="total tickets the demo will accept (default 30)")
            sp.add_argument("--rate", type=int, default=5, help="tickets per minute per client (default 5)")
            sp.add_argument("--hosted-specialists", action="store_true",
                            help="also let the demo call the four hosted specialist agents (needs a broad role)")
        if name == "apply":
            sp.add_argument("--step", required=True)
    args = p.parse_args(argv)
    if args.cmd == "status":
        print(status())
        return
    facts = deploy.gather_facts(getattr(args, "tag", None))
    if args.cmd == "down":
        down(facts)
        return
    opts = DemoOptions(args.cap, args.rate, args.hosted_specialists)
    if args.cmd == "plan":
        print(render_plan(facts, opts))
    else:
        apply_step(facts, opts, args.step)


if __name__ == "__main__":
    main(sys.argv[1:])
