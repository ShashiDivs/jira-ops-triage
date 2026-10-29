"""Deploy one agent to Foundry as a hosted agent — a plan you can read first, then run ONE step
at a time.

    python -m hosted.deploy plan  --agent routing
    python -m hosted.deploy apply --agent routing --step create_acr      # asks y/N first

Design rules, because every `apply` step spends money or changes the Azure subscription:
  * `plan` only READS Azure (identity, ids) and prints every step with the exact command, what
    it creates, what it costs, and how to undo it. It changes nothing.
  * `apply` runs exactly one named step, prints the exact command, and does nothing unless you
    answer "y". There is deliberately no "run all steps" mode.
  * Steps are ordered; each one says what it needs from the previous ones.

Uses the documented Python SDK path (build image in ACR -> create_version) rather than `azd`,
because the azure.yaml format for container-mode agents isn't published. Needs, for `apply` only:
`pip install "azure-ai-projects>=2.3.0"`.

STATUS: the plan, the step ordering and the y/N gate are unit-tested. The `az` commands and the
SDK/REST calls follow Microsoft's published examples but have NOT yet been run against live Azure —
the first `apply` is their first real test, which is why it is one confirmed step at a time.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from hosted.specialist import SPECIALIST_ROLES

# The build context is located from this file, not from the caller's working directory — a relative
# "backend" only worked when run from the repo root (the first live build failed on exactly that).
BACKEND_DIR = Path(__file__).resolve().parents[1]
RESOURCE_GROUP = "proj-foundryapp"
LOCATION = "swedencentral"  # same region as the proj-app Foundry project; supported for hosted agents
FOUNDRY_ACCOUNT = "proj-app"
FOUNDRY_PROJECT = "proj-app"
IMAGE_REPO = "ops-triage-agent"
API_VERSION_PROJECT = "2025-06-01"
# Hosted-agent sandbox sizes are fixed combos: 0.5/1Gi, 1/2Gi, 2/4Gi. The specialists are a thin
# model call, so the smallest is plenty; raise only if App Insights shows sustained >70% usage.
CPU, MEMORY = "0.5", "1Gi"
# The orchestrator runs the LangGraph pipeline plus an MCP subprocess per tool call, so it gets the next size up.
ORCHESTRATOR_CPU, ORCHESTRATOR_MEMORY = "1", "2Gi"

# Postgres for the orchestrator (the specialists need no database).
PG_DATABASE = "ops_triage"
PG_VERSION = "16"
PG_SKU = "Standard_B1ms"  # Burstable, 1 vCore / 2 GiB
PG_STORAGE_GIB = "32"  # the minimum
ORCHESTRATOR_ROLE = "orchestrator"


@dataclass(frozen=True)
class Facts:
    """Everything the plan needs from the live Azure account (all read-only lookups)."""
    subscription_id: str
    deployer_object_id: str
    project_principal_id: str
    project_endpoint: str
    git_tag: str
    deployer_upn: str = ""  # your sign-in name; the Postgres admin is registered under it
    client_ip: str = ""  # your public IP, given with --client-ip; only the firewall_your_ip step needs it
    pg_zone: str = ""  # pin the server to availability zone 1/2/3 (--pg-zone); empty lets Azure choose

    @property
    def pg_server_name(self) -> str:
        # Server names are global, lowercase letters/digits/hyphens. Same subscription-derived suffix as the registry.
        return f"opstriage-pg-{self.subscription_id.replace('-', '')[:6]}"

    @property
    def pg_host(self) -> str:
        return f"{self.pg_server_name}.postgres.database.azure.com"

    @property
    def acr_name(self) -> str:
        # ACR names are global, 5-50 alphanumeric chars. A subscription-derived suffix keeps it unique.
        return f"opstriage{self.subscription_id.replace('-', '')[:6]}"

    @property
    def project_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{RESOURCE_GROUP}"
            f"/providers/Microsoft.CognitiveServices/accounts/{FOUNDRY_ACCOUNT}/projects/{FOUNDRY_PROJECT}"
        )

    @property
    def account_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{RESOURCE_GROUP}"
            f"/providers/Microsoft.CognitiveServices/accounts/{FOUNDRY_ACCOUNT}"
        )

    @property
    def acr_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{RESOURCE_GROUP}"
            f"/providers/Microsoft.ContainerRegistry/registries/{self.acr_name}"
        )

    @property
    def image(self) -> str:
        return f"{self.acr_name}.azurecr.io/{IMAGE_REPO}:{self.git_tag}"


@dataclass(frozen=True)
class AgentSpec:
    role: str
    name: str  # the name shown in Foundry (alphanumeric + hyphens, <= 63 chars)
    env: dict[str, str] = field(default_factory=dict)
    cpu: str = CPU
    memory: str = MEMORY


_MODEL_ENV = {
    # Keyless: a hosted agent calls models as its own Entra agent identity, no API key.
    "AZURE_AUTH_MODE": "entra",
    "AZURE_CHAT_DEPLOYMENT": "DeepSeek-V3.2",
    "AZURE_CHAT_DEPLOYMENT_FAST": "DeepSeek-V3.2",
    "AZURE_CHAT_DEPLOYMENT_SECURITY": "o4-mini",
    "AZURE_EMBEDDING_DEPLOYMENT": "text-embedding-3-small",
}


def pg_user(spec: AgentSpec) -> str:
    """The agent's Postgres role. Azure maps an Entra identity to the role NAME you register it under; we
    register each agent under its Foundry name, so the role is self-describing in pg_stat_activity."""
    return spec.name


def pg_database_url(facts: Facts, user: str) -> str:
    """A password-LESS URL — with DATABASE_AUTH_MODE=entra each connection presents a token instead. The user
    is URL-quoted because an Entra sign-in name contains '@'."""
    return f"postgresql+psycopg://{quote(user, safe='')}@{facts.pg_host}:5432/{PG_DATABASE}?sslmode=require"


def specialist_invocation_url(facts: Facts, role: str) -> str:
    return f"{facts.project_endpoint}/agents/ops-{role}/endpoint/protocols/invocations?api-version=v1"


def _orchestrator_spec(facts: Facts) -> AgentSpec:
    # Approver names/roles are not secrets (they are the RBAC table, not credentials). Read from the local
    # .env at plan time so the hosted orchestrator enforces the same approvers you use locally.
    from config import get_settings

    name = f"ops-{ORCHESTRATOR_ROLE}"
    env = {
        "OPS_ROLE": ORCHESTRATOR_ROLE,
        **_MODEL_ENV,
        "DATABASE_AUTH_MODE": "entra",
        "DATABASE_URL": pg_database_url(facts, name),
        # The agent's DB role may read/write rows but not create tables; init_checkpointer made them already.
        "CHECKPOINTER_AUTO_SETUP": "false",
        "SPECIALIST_AUTH": "entra",
        "SPECIALIST_URLS": ",".join(f"{r}={specialist_invocation_url(facts, r)}" for r in SPECIALIST_ROLES),
        "RAG_MIN_SIMILARITY": "0.2",
        "APPROVER_ROLES": get_settings().approver_roles,
    }
    return AgentSpec(role=ORCHESTRATOR_ROLE, name=name, env=env, cpu=ORCHESTRATOR_CPU, memory=ORCHESTRATOR_MEMORY)


def agent_spec(role: str, facts: Facts | None = None) -> AgentSpec:
    if role == ORCHESTRATOR_ROLE:
        if facts is None:
            raise SystemExit("the orchestrator's settings depend on the live Azure facts (its database host, the "
                             "specialists' URLs) — build it with agent_spec('orchestrator', facts)")
        return _orchestrator_spec(facts)
    if role not in SPECIALIST_ROLES:
        raise SystemExit(f"unknown agent '{role}' (expected {ORCHESTRATOR_ROLE} or one of {', '.join(SPECIALIST_ROLES)})")
    return AgentSpec(
        role=role,
        name=f"ops-{role}",
        env={"OPS_ROLE": role, **_MODEL_ENV},
    )


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    command: list[str] | None  # None = performed through the SDK / REST rather than a CLI command
    detail: str
    creates: str
    cost: str
    undo: str


def _specialist_plan(facts: Facts, spec: AgentSpec, agent_principal_id: str | None = None) -> list[Step]:
    return [
        Step(
            "grant_deployer_role",
            "Let you create agents in the Foundry project",
            ["az", "role", "assignment", "create", "--assignee-object-id", facts.deployer_object_id,
             "--assignee-principal-type", "User", "--role", "Foundry Project Manager", "--scope", facts.project_id],
            "The deploy guide requires 'Foundry Project Manager' at project scope. You hold Owner + Foundry "
            "User, which do not include it. SKIP this step if you already have the role.",
            "1 role assignment for YOU on the proj-app project (a permission change)",
            "free",
            "az role assignment delete (same arguments)",
        ),
        Step(
            "register_acr_provider",
            "Register the Container Registry resource provider (one-time, per subscription)",
            ["az", "provider", "register", "--namespace", "Microsoft.ContainerRegistry", "--wait"],
            "Azure makes each subscription opt in to a resource type once before it can be used. Creating the "
            "registry failed with MissingSubscriptionRegistration on this subscription. SKIP if "
            "`az provider show --namespace Microsoft.ContainerRegistry --query registrationState` already says "
            "Registered.",
            "a subscription-level registration of the namespace (no resource is created)",
            "free",
            "az provider unregister --namespace Microsoft.ContainerRegistry (only possible with no registries)",
        ),
        Step(
            "create_acr",
            "Create the container registry",
            ["az", "acr", "create", "--name", facts.acr_name, "--resource-group", RESOURCE_GROUP,
             "--location", LOCATION, "--sku", "Basic", "--role-assignment-mode", "rbac-abac"],
            "No registry exists in this subscription, so one is needed to hold the agent image. Created in "
            "'rbac-abac' mode because that is the mode Foundry's deploy guide assumes: it tells you to grant "
            "'Container Registry Repository Reader', and Microsoft documents that the legacy AcrPull/AcrPush "
            "roles are NOT honored in ABAC registries (and that Reader is documented only for them). The default "
            "'rbac' mode would make that role's effect unknown. Basic SKU supports repository-scoped permissions "
            "per Microsoft's SKU table, though no page states 'rbac-abac works on Basic' in so many words — the "
            "next step verifies it.",
            f"1 Azure Container Registry '{facts.acr_name}' (Basic SKU) in {RESOURCE_GROUP}/{LOCATION}",
            "~$5/month while it exists (Basic SKU; confirm on the Azure pricing page)",
            f"az acr delete --name {facts.acr_name} --resource-group {RESOURCE_GROUP}",
        ),
        Step(
            "verify_acr_mode",
            "Verify the registry is in ABAC mode",
            ["az", "acr", "show", "--name", facts.acr_name, "--resource-group", RESOURCE_GROUP,
             "--query", "roleAssignmentMode", "-o", "tsv"],
            "Read-only. Expect 'AbacRepositoryPermissions'. If it says anything else, STOP: the pull role in "
            "the next steps would not take effect.",
            "nothing",
            "free",
            "n/a",
        ),
        Step(
            "grant_registry_writer",
            "Let you push images to the registry",
            ["az", "role", "assignment", "create", "--assignee-object-id", facts.deployer_object_id,
             "--assignee-principal-type", "User", "--role", "Container Registry Repository Writer",
             "--scope", facts.acr_id],
            "In an ABAC registry, owners get no data-plane access and ACR build tasks lose their default access "
            "to the registry. The build step runs as you ('[caller]'), so you need push rights on the registry.",
            "1 role assignment for YOU, scoped to the registry",
            "free",
            "az role assignment delete (same arguments)",
        ),
        Step(
            "grant_project_pull",
            "Let the Foundry project pull images from the registry",
            ["az", "role", "assignment", "create", "--assignee-object-id", facts.project_principal_id,
             "--assignee-principal-type", "ServicePrincipal", "--role", "Container Registry Repository Reader",
             "--scope", facts.acr_id],
            "The platform pulls the image using the PROJECT's managed identity, with the role Foundry's deploy "
            "guide names (valid because the registry is in ABAC mode). If the first pull still fails with "
            "image_pull_failed, the deploy guide's troubleshooting also asks you to confirm the registry's "
            "azureADAuthenticationAsArmPolicy is enabled.",
            "1 role assignment for the project's identity, scoped to the registry",
            "free",
            "az role assignment delete (same arguments)",
        ),
        Step(
            "build_image",
            "Build the image in ACR (linux/amd64)",
            ["az", "acr", "build", "--registry", facts.acr_name, "--platform", "linux/amd64",
             "--image", f"{IMAGE_REPO}:{facts.git_tag}", "--file", "Dockerfile.hosted",
             "--source-acr-auth-id", "[caller]", str(BACKEND_DIR)],
            "Built remotely in ACR, so your Mac's ARM architecture doesn't matter. The context is backend/; "
            "its .dockerignore keeps .env out of the image. '--source-acr-auth-id [caller]' is required for "
            "ABAC registries (build tasks have no default registry access there). Role assignments can take a "
            "few minutes to propagate — if this fails with an auth error right after the writer grant, wait and retry.",
            f"image {facts.image} (~650 MB)",
            "a few ACR build-minutes (cents) + registry storage",
            f"az acr repository delete --name {facts.acr_name} --image {IMAGE_REPO}:{facts.git_tag}  (needs the Repository Contributor role in ABAC mode; deleting the registry is simpler)",
        ),
        Step(
            "create_agent_version",
            f"Register '{spec.name}' as a hosted agent",
            None,
            f"azure-ai-projects create_version(agent_name='{spec.name}', HostedAgentDefinition(Invocations "
            f"2.0.0, cpu={spec.cpu}, memory={spec.memory}, image={facts.image}, env={json.dumps(spec.env)})). "
            "The platform provisions compute and creates a dedicated Entra agent identity for it.",
            f"hosted agent '{spec.name}' (version 1) with its own endpoint + identity, visible in Foundry",
            "billed for CPU+memory only while a session is active; idle sessions are deprovisioned after "
            "the idle timeout (default 15 min). Current rates: the Foundry Agent Service pricing page.",
            f"project.agents.delete(agent_name='{spec.name}', force=True)",
        ),
        Step(
            "wait_active",
            "Wait for the agent version to become active",
            None,
            "Polls get_version until status is 'active' (typically under a minute) or 'failed' (prints the error).",
            "nothing",
            "free",
            "n/a",
        ),
        Step(
            "grant_agent_model_access",
            "Let the hosted agent's own identity call the models",
            ["az", "role", "assignment", "create", "--assignee-object-id",
             agent_principal_id or "<the agent's identity — looked up at run time>",
             "--assignee-principal-type", "ServicePrincipal", "--role", "Cognitive Services OpenAI User",
             "--scope", facts.account_id],
            "Found by the first live smoke test: the container started and its keyless login worked, but the "
            "model call returned 401 'lacks the required data action "
            "Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action'. Microsoft's docs say "
            "hosted agents get model access by default; on this project they did not. 'Cognitive Services OpenAI "
            "User' grants only inference actions (verified it includes that exact action); 'Foundry User' would "
            "also work but carries a Microsoft.CognitiveServices/* wildcard, far broader than a routing "
            "classifier needs. Scoped to the one Foundry account that hosts the models.",
            f"1 role assignment for the agent's identity, scoped to the {FOUNDRY_ACCOUNT} account",
            "free",
            "az role assignment delete (same arguments)",
        ),
        Step(
            "smoke_test",
            "Call the deployed agent once",
            None,
            f"POST {facts.project_endpoint}/agents/{spec.name}/endpoint/protocols/invocations?api-version=v1 "
            + ("with an Entra token. One read-only {\"action\": \"list\"} call: the container must start, reach Postgres "
               "through its own identity, and answer — it exercises the database path end to end."
               if spec.role == ORCHESTRATOR_ROLE else "with an Entra token. One small routing call."),
            "nothing (one read-only call)" if spec.role == ORCHESTRATOR_ROLE else "nothing (one model call)",
            "fractions of a cent",
            "n/a",
        ),
    ]


def database_grant_sql(role: str) -> list[str]:
    """What the agent's database role is allowed to do — shown in the step, and the only SQL it runs.
    Data access only: no CREATE (tables already exist), no superuser, no other database."""
    r = f'"{role}"'
    return [
        f"SELECT * FROM pgaadauth_create_principal_with_oid('{role}', '<the agent identity>', 'service', false, false)",
        f"GRANT CONNECT ON DATABASE {PG_DATABASE} TO {r}",
        f"GRANT USAGE ON SCHEMA public TO {r}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}",
        # POST /api/reset empties exactly these three tables; nothing else needs TRUNCATE.
        f"GRANT TRUNCATE ON checkpoints, checkpoint_blobs, checkpoint_writes TO {r}",
    ]


_ORCHESTRATOR_ONLY_KEYS = [
    "register_postgres_provider", "create_postgres", "set_postgres_entra_admin", "allow_pgvector",
    "firewall_azure_services", "firewall_your_ip", "create_database", "migrate_and_seed",
]


def _orchestrator_plan(facts: Facts, spec: AgentSpec, agent_principal_id: str | None = None) -> list[Step]:
    server, ip = facts.pg_server_name, facts.client_ip or "<your public IP — pass --client-ip>"
    upn = facts.deployer_upn or "<your sign-in name>"
    common = {s.key: s for s in _specialist_plan(facts, spec, agent_principal_id)}
    pg = ["az", "postgres", "flexible-server"]
    db_steps = [
        Step(
            "register_postgres_provider",
            "Register the PostgreSQL resource provider (one-time, per subscription)",
            ["az", "provider", "register", "--namespace", "Microsoft.DBforPostgreSQL", "--wait"],
            "Same opt-in the registry needed. `az provider show --namespace Microsoft.DBforPostgreSQL` said "
            "NotRegistered on this subscription. SKIP if it already says Registered.",
            "a subscription-level registration (no resource is created)", "free",
            "az provider unregister --namespace Microsoft.DBforPostgreSQL (only possible with no servers)",
        ),
        Step(
            "create_postgres",
            "Create the PostgreSQL server (Entra login only, no password)",
            [*pg, "create", "--name", server, "--resource-group", RESOURCE_GROUP, "--location", LOCATION,
             "--tier", "Burstable", "--sku-name", PG_SKU, "--storage-size", PG_STORAGE_GIB, "--version", PG_VERSION,
             "--microsoft-entra-auth", "Enabled", "--password-auth", "Disabled", "--public-access", "None", "--yes",
             *(["--zone", facts.pg_zone] if facts.pg_zone else [])],
            "Password login is disabled outright, so the server holds no password anyone could leak: callers prove "
            "who they are with an Entra token (db/entra.py). '--public-access None' means reachable over the public "
            "internet but with NO firewall rule yet — the next steps add exactly the ones needed. Not verified "
            "before running: capacity. The first live attempt failed with RegionalAllocationFailed in the zone Azure picked; pin another with --pg-zone 1|2|3. Newer az versions "
            "(2.86+) rename some of these arguments — check `az postgres flexible-server create --help`.",
            f"1 PostgreSQL Flexible Server '{server}' ({PG_SKU}, {PG_STORAGE_GIB} GiB, v{PG_VERSION}) in {RESOURCE_GROUP}/{LOCATION}",
            "~$19/month while it exists, running 24/7 (list price: compute $0.0199/h + 32 GiB at ~$0.137/GiB-month; "
            "backup storage beyond the free allowance extra). You are billed until you delete it.",
            f"az postgres flexible-server delete --name {server} --resource-group {RESOURCE_GROUP} --yes",
        ),
        Step(
            "set_postgres_entra_admin",
            "Make yourself the database administrator",
            [*pg, "microsoft-entra-admin", "create", "--resource-group", RESOURCE_GROUP, "--server-name", server,
             "--display-name", upn, "--object-id", facts.deployer_object_id, "--type", "User"],
            "With password login off there is no built-in admin, so an Entra user must be named. You need this to "
            "enable the extension, run the migrations and register the agent's database role.",
            "1 Entra administrator on the server (a permission change)", "free",
            f"az postgres flexible-server microsoft-entra-admin delete --resource-group {RESOURCE_GROUP} --server-name {server} --object-id {facts.deployer_object_id}",
        ),
        Step(
            "allow_pgvector",
            "Allow the pgvector extension",
            [*pg, "parameter", "set", "--resource-group", RESOURCE_GROUP, "--server-name", server,
             "--name", "azure.extensions", "--value", "VECTOR"],
            "Azure only lets CREATE EXTENSION load extensions on this allow-list (default: none). The first "
            "migration runs CREATE EXTENSION vector, so it fails without this.",
            "one server parameter", "free",
            "az postgres flexible-server parameter set (same arguments, --value \"\")",
        ),
        Step(
            "firewall_azure_services",
            "Let Azure-hosted services connect",
            [*pg, "firewall-rule", "create", "--resource-group", RESOURCE_GROUP, "--name", server,
             "--rule-name", "AllowAzureServices", "--start-ip-address", "0.0.0.0", "--end-ip-address", "0.0.0.0"],
            "The special 0.0.0.0 rule admits ANY Azure-hosted service — not just yours. It is broad. It is used "
            "because Foundry does not publish the hosted agents' outbound addresses; whether their traffic counts "
            "as 'Azure services' is NOT verified (the smoke test will tell). The real gate is Entra: without a "
            "database role mapped to a valid identity, a connection that gets through is still refused. The "
            "stricter alternative is a private network (VNet), which costs more and is a bigger change.",
            "1 firewall rule", "free", "az postgres flexible-server firewall-rule delete (same names)",
        ),
        Step(
            "firewall_your_ip",
            "Let your laptop connect (for migrations and seeding)",
            [*pg, "firewall-rule", "create", "--resource-group", RESOURCE_GROUP, "--name", server,
             "--rule-name", "deployer", "--start-ip-address", ip, "--end-ip-address", ip],
            "Needed only for the migrate_and_seed and grant_agent_database_access steps, which run from this "
            "machine. Pass your address with --client-ip. Delete the rule when you are done.",
            "1 firewall rule for one IP", "free", "az postgres flexible-server firewall-rule delete (same names)",
        ),
        Step(
            "create_database",
            f"Create the '{PG_DATABASE}' database",
            [*pg, "db", "create", "--resource-group", RESOURCE_GROUP, "--server-name", server, "--database-name", PG_DATABASE],
            "An empty database for the app's tables.", f"1 database '{PG_DATABASE}'", "free",
            f"az postgres flexible-server db delete --resource-group {RESOURCE_GROUP} --server-name {server} --database-name {PG_DATABASE} --yes",
        ),
        Step(
            "migrate_and_seed",
            "Create the tables and load the RAG corpus",
            None,
            "Runs from backend/ as YOU (the Entra admin), against the new server, with DATABASE_AUTH_MODE=entra: "
            "`python -m alembic upgrade head` (tables + pgvector), `python -m scripts.init_checkpointer` "
            "(the pipeline's pause/resume tables), `python -m scripts.seed` (embeds and loads 10 tickets + 5 "
            "runbooks — the one part that calls the embedding model; uses your local .env for it). Needs "
            "`az login`, the firewall_your_ip rule, and the admin role.",
            "all application tables, the vector extension, and the corpus",
            "15 small embedding calls (a fraction of a cent)",
            f"drop and recreate the database (az postgres flexible-server db delete / create)",
        ),
    ]
    agent_steps = [
        common["build_image"], common["create_agent_version"], common["wait_active"], common["grant_agent_model_access"],
        Step(
            "grant_agent_database_access",
            "Let the orchestrator's identity into the database (least privilege)",
            None,
            "Runs as you (the admin) over an Entra-token connection. Creates a database role for the agent's "
            "identity and grants it data access only:\n      " + "\n      ".join(database_grant_sql(pg_user(spec)))
            + "\n    The role mapping call is the documented way to register an Entra identity with Azure Postgres "
            "but is NOT yet verified against this server.",
            f"1 database role '{pg_user(spec)}' mapped to the agent's identity, with the grants above", "free",
            f"DROP OWNED BY \"{pg_user(spec)}\"; DROP ROLE \"{pg_user(spec)}\" (as admin)",
        ),
        Step(
            "grant_orchestrator_invoke_specialists",
            "Let the orchestrator call the four specialist agents",
            ["az", "role", "assignment", "create", "--assignee-object-id",
             agent_principal_id or "<the agent's identity — looked up at run time>",
             "--assignee-principal-type", "ServicePrincipal", "--role", "Foundry User", "--scope", facts.project_id],
            "A call into a hosted agent needs an Entra identity with permission on the project. 'Foundry User' is "
            "what you hold, and it works for your own calls; no narrower built-in role names an 'invoke agent' "
            "action in its definition, so this is the one known to work. It is broad (Microsoft.CognitiveServices/*) "
            "and is the first thing to tighten once the smoke test passes — try 'Foundry Project Runtime User' "
            "(responses/* only) and see whether Invocations calls still succeed. Until then, the fallback means a "
            "refused call degrades to in-process model calls rather than failing tickets.",
            "1 role assignment for the orchestrator's identity, scoped to the project", "free",
            "az role assignment delete (same arguments)",
        ),
        common["smoke_test"],
    ]
    return db_steps + agent_steps


def build_plan(facts: Facts, spec: AgentSpec, agent_principal_id: str | None = None) -> list[Step]:
    """Specialists: registry + image + agent + model access (the registry steps are one-time and shared).
    Orchestrator: the Postgres steps, then — reusing the registry the specialists already created — the image,
    the agent, and the grants it needs."""
    if spec.role == ORCHESTRATOR_ROLE:
        return _orchestrator_plan(facts, spec, agent_principal_id)
    return _specialist_plan(facts, spec, agent_principal_id)


def _smoke_payload(spec: AgentSpec) -> dict:
    # A request each specialist can answer with no database/evidence.
    ticket = {"summary": "Footer copyright year is wrong", "description": "Footer shows last year.", "component": "marketing-site"}
    return {
        "triage-generic": {"ticket": ticket, "similar_tickets": [], "severity_policy": "S1 outage, S2 degraded, S3 minor, S4 cosmetic"},
        "triage-security": {"ticket": ticket, "similar_tickets": [], "security_runbook": "Security incidents are S1."},
        "triage-trivial": {"ticket": ticket},
        "routing": {"ticket": {"component": "marketing-site"}, "ownership_doc": "marketing-site -> Growth team"},
        # Read-only: lists tickets, proving the container reached the database through its own identity.
        "orchestrator": {"action": "list"},
    }[spec.role]


# ---------------------------------------------------------------------------------------------
# Live lookups (read-only) and printing
# ---------------------------------------------------------------------------------------------

LOOKUP_TIMEOUT_SECONDS = 90


def _az(*args: str) -> str:
    """A read-only `az` lookup. Bounded: the first live step 7 hung for 7+ minutes with no output on
    one stalled `az ad signed-in-user show` (a transient network stall). A hang is the worst failure
    mode for a tool that spends money, so a stall must fail loudly and quickly instead."""
    try:
        return subprocess.run(
            ["az", *args], check=True, capture_output=True, text=True, timeout=LOOKUP_TIMEOUT_SECONDS
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"`az {' '.join(args[:3])} ...` did not answer within {LOOKUP_TIMEOUT_SECONDS}s — usually a transient "
            "network stall to Microsoft. Nothing was changed in Azure. Check your connection and retry."
        )


def gather_facts(tag: str | None = None, client_ip: str = "", pg_zone: str = "") -> Facts:
    sub = _az("account", "show", "--query", "id", "-o", "tsv")
    project_id = (
        f"/subscriptions/{sub}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.CognitiveServices"
        f"/accounts/{FOUNDRY_ACCOUNT}/projects/{FOUNDRY_PROJECT}"
    )
    project = json.loads(_az("resource", "show", "--ids", project_id, "--api-version", API_VERSION_PROJECT, "-o", "json"))
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=BACKEND_DIR).stdout.strip() or "dev"
    dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=BACKEND_DIR).stdout.strip()
    if dirty:
        # An image built from uncommitted changes must not carry a tag that claims a clean commit.
        sha += "-dirty"
    # An explicit tag pins to an image that already exists (e.g. after committing a tooling-only change,
    # HEAD no longer names the commit the image was built from).
    sha = tag or sha
    return Facts(
        subscription_id=sub,
        deployer_object_id=_az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"),
        deployer_upn=_az("ad", "signed-in-user", "show", "--query", "userPrincipalName", "-o", "tsv"),
        client_ip=client_ip,
        pg_zone=pg_zone,
        project_principal_id=project["identity"]["principalId"],
        project_endpoint=project["properties"]["endpoints"]["AI Foundry API"].rstrip("/"),
        git_tag=sha,
    )


def render_plan(facts: Facts, spec: AgentSpec) -> str:
    out = [
        f"DEPLOY PLAN — agent '{spec.name}' (role {spec.role})",
        f"  project : {facts.project_endpoint}",
        f"  image   : {facts.image}",
        "  Nothing below has been run. `plan` only reads Azure.",
        "",
    ]
    for i, s in enumerate(build_plan(facts, spec), 1):
        cmd = shlex.join(s.command) if s.command else "(SDK / REST call — see detail)"
        out += [
            f"[{i}] {s.key} — {s.title}",
            f"    command : {cmd}",
            f"    detail  : {s.detail}",
            f"    creates : {s.creates}",
            f"    cost    : {s.cost}",
            f"    undo    : {s.undo}",
            "",
        ]
    out += [
        "Run one step at a time:  python -m hosted.deploy apply --agent " + spec.role + " --step <key>",
        "Not covered by this plan (verify before relying on it): Application Insights is NOT connected to",
        "the proj-app project, so the platform's automatic tracing has nowhere to land until one is connected.",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# apply — ONE step, behind a y/N gate
# ---------------------------------------------------------------------------------------------

def _confirm(prompt: str, ask: Callable[[str], str] = input) -> bool:
    return ask(f"{prompt} [y/N] ").strip().lower() == "y"


def _project_client(facts: Facts):
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError:
        raise SystemExit('this step needs the SDK:  pip install "azure-ai-projects>=2.3.0" azure-identity')
    return AIProjectClient(endpoint=facts.project_endpoint, credential=DefaultAzureCredential())


def _create_agent_version(facts: Facts, spec: AgentSpec) -> None:
    from azure.ai.projects.models import (
        AgentEndpointProtocol,
        ContainerConfiguration,
        HostedAgentDefinition,
        ProtocolVersionRecord,
    )

    agent = _project_client(facts).agents.create_version(
        agent_name=spec.name,
        definition=HostedAgentDefinition(
            protocol_versions=[ProtocolVersionRecord(protocol=AgentEndpointProtocol.INVOCATIONS, version="2.0.0")],
            cpu=spec.cpu,
            memory=spec.memory,
            container_configuration=ContainerConfiguration(image=facts.image),
            environment_variables=spec.env,
        ),
    )
    print(f"created agent '{agent.name}', version {agent.version}")


def _wait_active(facts: Facts, spec: AgentSpec, version: str, timeout_s: int = 300) -> None:
    client = _project_client(facts)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        info = client.agents.get_version(agent_name=spec.name, agent_version=version)
        status = info["status"]
        print(f"status: {status}")
        if status == "active":
            return
        if status == "failed":
            raise SystemExit(f"provisioning failed: {info.get('error')}")
        time.sleep(5)
    raise SystemExit("timed out waiting for the agent to become active")


def _smoke_test(facts: Facts, spec: AgentSpec) -> None:
    import httpx
    from azure.identity import DefaultAzureCredential

    token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token
    url = f"{facts.project_endpoint}/agents/{spec.name}/endpoint/protocols/invocations"
    resp = httpx.post(url, params={"api-version": "v1"}, headers={"Authorization": f"Bearer {token}"},
                      json=_smoke_payload(spec), timeout=120)
    print(f"HTTP {resp.status_code}\n{resp.text[:600]}")


def _run_or_exit(run: Callable[..., object], command: list[str], key: str, **kwargs) -> None:
    try:
        run(command, check=True, **kwargs)
    except subprocess.CalledProcessError as e:
        # A failed command is an expected outcome, not a crash: say what happened and what did NOT happen,
        # instead of dumping a traceback.
        raise SystemExit(
            f"\nSTEP FAILED: '{key}' exited with status {e.returncode} (the error is printed above). "
            "Nothing further was run. Check Azure before retrying — `az resource list -g "
            f"{RESOURCE_GROUP}` shows what exists."
        )


def _migrate_and_seed(facts: Facts, run: Callable[..., object]) -> None:
    """Runs the app's own database setup against the Azure server, as the signed-in admin (keyless)."""
    env = {
        **os.environ,
        "DATABASE_URL": pg_database_url(facts, facts.deployer_upn),
        "DATABASE_AUTH_MODE": "entra",
        "CHECKPOINTER_AUTO_SETUP": "true",
    }
    for module in (["alembic", "upgrade", "head"], ["scripts.init_checkpointer"], ["scripts.seed"]):
        cmd = [sys.executable, "-m", *module]
        print(f"  running: {shlex.join(cmd)}")
        _run_or_exit(run, cmd, "migrate_and_seed", cwd=BACKEND_DIR, env=env)


def _grant_database_access(facts: Facts, spec: AgentSpec, principal_id: str) -> None:
    import re

    import psycopg

    from db.entra import database_token

    role = pg_user(spec)
    if not re.fullmatch(r"[a-z0-9-]+", role):  # it is interpolated into SQL below: only ever our own agent name
        raise SystemExit(f"refusing to use '{role}' as a database role name")
    with psycopg.connect(host=facts.pg_host, dbname=PG_DATABASE, user=facts.deployer_upn,
                         password=database_token(), sslmode="require", autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists:
            print(f"  database role '{role}' already exists — only (re)applying grants")
        else:
            conn.execute("SELECT * FROM pgaadauth_create_principal_with_oid(%s, %s, 'service', false, false)",
                         (role, principal_id))
            print(f"  created database role '{role}' for identity {principal_id}")
        for statement in database_grant_sql(role)[1:]:  # [0] is the role mapping done just above
            conn.execute(statement)
            print(f"  ok: {statement}")


def _agent_principal_id(facts: Facts, spec: AgentSpec) -> str:
    """The identity the platform created for the deployed agent (read-only lookup)."""
    agent = _project_client(facts).agents.get(agent_name=spec.name)
    identity = (agent if isinstance(agent, dict) else agent.as_dict()).get("instance_identity") or {}
    if not identity.get("principal_id"):
        raise SystemExit(f"'{spec.name}' has no instance identity yet — has create_agent_version run?")
    return identity["principal_id"]


def apply_step(facts: Facts, spec: AgentSpec, key: str, ask: Callable[[str], str] = input,
               run: Callable[..., object] = subprocess.run, version: str = "1",
               lookup_principal: Callable[[Facts, AgentSpec], str] = _agent_principal_id) -> bool:
    """Runs ONE step if (and only if) the user types 'y'. Returns whether it ran."""
    # The agent's identity only exists once the agent does, so resolve it (read-only) BEFORE showing the
    # command — the gate must display the exact command it is about to run.
    needs_agent_identity = ("grant_agent_model_access", "grant_agent_database_access", "grant_orchestrator_invoke_specialists")
    principal = lookup_principal(facts, spec) if key in needs_agent_identity else None
    steps = {s.key: s for s in build_plan(facts, spec, principal)}
    if key not in steps:
        raise SystemExit(f"unknown step '{key}' (expected one of: {', '.join(steps)})")
    step = steps[key]
    if key == "firewall_your_ip" and not facts.client_ip:
        raise SystemExit("this step opens the database firewall to ONE address — pass yours with --client-ip <address> "
                         "(find it with a 'what is my IP' site; nothing was changed)")
    if key in ("migrate_and_seed", "grant_agent_database_access") and not facts.deployer_upn:
        raise SystemExit("this step connects as you, but your sign-in name could not be determined (nothing was changed)")
    print(f"\nSTEP {step.key}: {step.title}")
    print(f"  command : {shlex.join(step.command) if step.command else '(SDK / REST call)'}")
    print(f"  creates : {step.creates}\n  cost    : {step.cost}\n  undo    : {step.undo}")
    if not _confirm("Run this step against your Azure subscription?", ask):
        print("not run.")
        return False
    if step.command:
        _run_or_exit(run, step.command, step.key)
    elif key == "migrate_and_seed":
        _migrate_and_seed(facts, run)
    elif key == "grant_agent_database_access":
        _grant_database_access(facts, spec, principal)
    elif key == "create_agent_version":
        _create_agent_version(facts, spec)
    elif key == "wait_active":
        _wait_active(facts, spec, version)
    elif key == "smoke_test":
        _smoke_test(facts, spec)
    return True


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="hosted.deploy", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument("--agent", required=True, help="orchestrator, or a specialist role such as routing")
        sp.add_argument("--tag", default=None, help="image tag to use instead of the current git commit")
        sp.add_argument("--pg-zone", default="", choices=["", "1", "2", "3"],
                        help="pin the PostgreSQL server to an availability zone (capacity differs per zone)")
        sp.add_argument("--client-ip", default="", help="your public IP, for the orchestrator's firewall_your_ip step")
        if name == "apply":
            sp.add_argument("--step", required=True)
            sp.add_argument("--version", default="1", help="agent version to wait on (create_agent_version prints it)")
    args = p.parse_args(argv)
    if args.agent != ORCHESTRATOR_ROLE:
        agent_spec(args.agent)  # fail fast on an unknown role, before any Azure lookup
    facts = gather_facts(args.tag, args.client_ip, args.pg_zone)
    spec = agent_spec(args.agent, facts)
    if args.cmd == "plan":
        print(render_plan(facts, spec))
    else:
        apply_step(facts, spec, args.step, version=args.version)


if __name__ == "__main__":
    main(sys.argv[1:])
