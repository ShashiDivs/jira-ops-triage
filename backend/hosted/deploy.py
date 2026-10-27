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
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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


@dataclass(frozen=True)
class Facts:
    """Everything the plan needs from the live Azure account (all read-only lookups)."""
    subscription_id: str
    deployer_object_id: str
    project_principal_id: str
    project_endpoint: str
    git_tag: str

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


def agent_spec(role: str) -> AgentSpec:
    if role == "orchestrator":
        raise SystemExit(
            "The orchestrator isn't deployable yet: it needs a Postgres database the hosted agent can "
            "reach (an Azure Database for PostgreSQL decision + cost). Deploy the four specialists first."
        )
    if role not in SPECIALIST_ROLES:
        raise SystemExit(f"unknown agent '{role}' (expected one of {', '.join(SPECIALIST_ROLES)})")
    return AgentSpec(
        role=role,
        name=f"ops-{role}",
        env={
            "OPS_ROLE": role,
            # Keyless: the hosted agent calls models as its own Entra agent identity, no API key.
            "AZURE_AUTH_MODE": "entra",
            "AZURE_CHAT_DEPLOYMENT": "DeepSeek-V3.2",
            "AZURE_CHAT_DEPLOYMENT_FAST": "DeepSeek-V3.2",
            "AZURE_CHAT_DEPLOYMENT_SECURITY": "o4-mini",
            "AZURE_EMBEDDING_DEPLOYMENT": "text-embedding-3-small",
        },
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


def build_plan(facts: Facts, spec: AgentSpec, agent_principal_id: str | None = None) -> list[Step]:
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
            f"2.0.0, cpu={CPU}, memory={MEMORY}, image={facts.image}, env={json.dumps(spec.env)})). "
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
            "with an Entra token. One small routing call.",
            "nothing (one model call)",
            "fractions of a cent",
            "n/a",
        ),
    ]


def _smoke_payload(spec: AgentSpec) -> dict:
    # A request each specialist can answer with no database/evidence.
    ticket = {"summary": "Footer copyright year is wrong", "description": "Footer shows last year.", "component": "marketing-site"}
    return {
        "triage-generic": {"ticket": ticket, "similar_tickets": [], "severity_policy": "S1 outage, S2 degraded, S3 minor, S4 cosmetic"},
        "triage-security": {"ticket": ticket, "similar_tickets": [], "security_runbook": "Security incidents are S1."},
        "triage-trivial": {"ticket": ticket},
        "routing": {"ticket": {"component": "marketing-site"}, "ownership_doc": "marketing-site -> Growth team"},
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


def gather_facts(tag: str | None = None) -> Facts:
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
            cpu=CPU,
            memory=MEMORY,
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
    principal = lookup_principal(facts, spec) if key == "grant_agent_model_access" else None
    steps = {s.key: s for s in build_plan(facts, spec, principal)}
    if key not in steps:
        raise SystemExit(f"unknown step '{key}' (expected one of: {', '.join(steps)})")
    step = steps[key]
    print(f"\nSTEP {step.key}: {step.title}")
    print(f"  command : {shlex.join(step.command) if step.command else '(SDK / REST call)'}")
    print(f"  creates : {step.creates}\n  cost    : {step.cost}\n  undo    : {step.undo}")
    if not _confirm("Run this step against your Azure subscription?", ask):
        print("not run.")
        return False
    if step.command:
        try:
            run(step.command, check=True)
        except subprocess.CalledProcessError as e:
            # A failed az command is an expected outcome, not a crash: say what happened and what did
            # NOT happen, instead of dumping a traceback.
            raise SystemExit(
                f"\nSTEP FAILED: '{step.key}' exited with status {e.returncode} (the az error is printed above). "
                "Nothing further was run. Check Azure before retrying — `az resource list -g "
                f"{RESOURCE_GROUP}` shows what exists."
            )
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
        sp.add_argument("--agent", required=True, help="specialist role, e.g. routing")
        sp.add_argument("--tag", default=None, help="image tag to use instead of the current git commit")
        if name == "apply":
            sp.add_argument("--step", required=True)
            sp.add_argument("--version", default="1", help="agent version to wait on (create_agent_version prints it)")
    args = p.parse_args(argv)
    spec = agent_spec(args.agent)
    facts = gather_facts(args.tag)
    if args.cmd == "plan":
        print(render_plan(facts, spec))
    else:
        apply_step(facts, spec, args.step, version=args.version)


if __name__ == "__main__":
    main(sys.argv[1:])
