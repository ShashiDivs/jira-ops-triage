# Foundry hosted agents — deployment record

*Written 2026-10-27, after the first live deployment. This is a record of what was actually done and
observed, not a design document: where something is unverified or unknown, it says so.*

## Summary

Four of the pipeline's agents run as **hosted agents** in the `proj-app` Azure AI Foundry project
(swedencentral). Each is its own named entry in Foundry with its own identity and endpoint, built
from **one container image**, with `OPS_ROLE` selecting which agent a container is. Each answered a
real smoke-test call with HTTP 200 using **keyless** authentication (its own Entra identity — no API
key anywhere).

| Hosted agent | `OPS_ROLE` | Model it calls | Version | Smoke test |
|---|---|---|---|---|
| `ops-routing` | `routing` | `DeepSeek-V3.2` | 1 | 200 — correct team |
| `ops-triage-generic` | `triage-generic` | `DeepSeek-V3.2` | 1 | 200 — S4 for a cosmetic ticket |
| `ops-triage-security` | `triage-security` | `o4-mini` | 1 | 200 |
| `ops-triage-trivial` | `triage-trivial` | `DeepSeek-V3.2` | 1 | 200 — S4 |

**Not deployed:** the orchestrator (it needs a Postgres database the hosted agent can reach — an
unmade decision with an ongoing cost).

**Not wired in:** the running pipeline still calls the models in-process. The hosted agents answer
correctly on their own, but a ticket does not yet flow through them. Wiring is `SPECIALIST_URLS` +
`SPECIALIST_AUTH=entra` on the orchestrator (see `.env.example`); it has been tested locally against
real processes over HTTP, not yet against the hosted endpoints.

## What exists in Azure

| Resource | Name | Notes |
|---|---|---|
| Container registry | `opstriageb24a32` | Basic SKU, **ABAC mode** (`AbacRepositoryPermissions`), swedencentral. About $5/month while it exists (confirm on Azure's pricing page). |
| Image | `ops-triage-agent:1877562` | `linux/amd64`, ~135 MB compressed in the registry. Tag = the git commit it was built from. |
| Hosted agents | the four above | 0.5 vCPU / 1 GiB each; each version is immutable. |
| Resource provider | `Microsoft.ContainerRegistry` | Registered on the subscription (one-time; free). |

Role assignments (everything here is least-privilege; nothing broader than needed):

| Who | Role | Scope | Why |
|---|---|---|---|
| The deploying user | `Foundry Project Manager` | the `proj-app` project | Required to create agents. |
| The deploying user | `Container Registry Repository Writer` | the registry | In an ABAC registry, owners have no data-plane rights, and the build runs as the user. |
| The Foundry project's managed identity | `Container Registry Repository Reader` | the registry | The platform pulls the image with the project's identity. |
| Each of the 4 agents' own identity | `Cognitive Services OpenAI User` | the `proj-app` account | Lets the agent call the models. Inference actions only — deliberately not `Foundry User` (a `Microsoft.CognitiveServices/*` wildcard) or Owner/Contributor. |

Application Insights is **not** connected to `proj-app`, so the platform's automatic tracing has
nowhere to land. (A different App Insights exists in the resource group for another project.)

## How it was done

Everything went through `python -m hosted.deploy` (`backend/hosted/deploy.py`): `plan` only reads Azure
and prints every step with its exact command, cost and undo; `apply --step <key>` runs **one** step and
does nothing unless you type `y`. There is deliberately no run-everything mode. Each step below was
approved before it ran; the final three agents (step 4) were approved as one batch, then run through
the same per-step gate.

1. `grant_deployer_role`, `register_acr_provider`, `create_acr`, `verify_acr_mode`
2. `grant_registry_writer`, `grant_project_pull`
3. `build_image` — built remotely in ACR (`az acr build --platform linux/amd64`), so an ARM Mac is fine.
4. For `routing` first, then the other three as one batch: `create_agent_version` → `wait_active` →
   `grant_agent_model_access` → `smoke_test`. Pass `--tag <existing image tag>` once the git HEAD has
   moved past the commit the image was built from.

The Python SDK path (ACR build → `create_version`) was used rather than `azd`, because the `azure.yaml`
schema for container-mode agents is not published (Microsoft's own samples deploy from a zip of source).

## What went wrong, and what it taught us

The deploy tooling had never touched real Azure, so the first run was its first real test. Every
problem below was found live, fixed in the tool, and covered by a regression test.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `create_acr` failed: `MissingSubscriptionRegistration` | The subscription had never used Container Registry; each subscription must opt in once per resource type. Failed before creating anything. | New `register_acr_provider` step. |
| 2 | Registry role semantics unclear | The default registry mode is `rbac`; the role Foundry's guide names (`Repository Reader`) is an **ABAC** role, and Microsoft documents that legacy `AcrPull`/`AcrPush` are *not honored* in ABAC registries. | Create with `--role-assignment-mode rbac-abac`; new `verify_acr_mode` step. Verified live: **Basic SKU supports ABAC.** |
| 3 | (anticipated, then confirmed) ACR build tasks lose default registry access in ABAC mode | Documented behavior. | New `grant_registry_writer` step; `az acr build --source-acr-auth-id '[caller]'`. Worked first time. |
| 4 | `build_image`: `'backend' doesn't exist` | The build context was the relative path `backend`; the tool was run from inside `backend/`. Failed locally; nothing uploaded. | Context is now absolute and derived from the module's own location. |
| 5 | `create_version` rejected: `Environment variable 'AGENT_ROLE' is reserved` | The platform reserves **every** env var starting with `FOUNDRY_` **or `AGENT_`**. Microsoft's docs list only `FOUNDRY_*`. Validation error — nothing created. | Renamed to `OPS_ROLE`; regression test forbids reserved prefixes. The image had to be rebuilt (the old name was baked into its config). |
| 6 | The tool hung for 7+ minutes with no output | It never reached Foundry: it was blocked on its own startup `az ad signed-in-user show` — a transient network stall. Found with `ps`/`lsof`, not by guessing. Nothing created or billed. | Every `az` lookup now has a 90 s timeout and fails loudly. |
| 7 | Default image tag no longer matched any built image | The tag is the current git commit; committing a tooling-only change moved HEAD past the commit the image was built from. | `--tag` pins an existing image. |
| 8 | Smoke test returned `HTTP 500 {"error":"specialist failed"}` | The container log (via the SDK's session log stream) showed `401 … principal … lacks the required data action …/chat/completions/action`. Microsoft's docs say hosted agents get model access by default; **on this project they did not.** Our code was fine. | New `grant_agent_model_access` step (least-privilege role above). Needed once **per agent identity** — each agent has its own. |

**Reading container logs:** `project.agents.get_session_log_stream(agent_name, agent_version, session_id)`
returns a byte stream (server-sent events); `list_sessions(agent_name)` gives the session ids. This is
how problem 8 was diagnosed. The hosted endpoint returning only the generic 500 is by our own design
(`hosted/server.py` hides internals), so the log stream is the way to see the real error.

## Verified vs. not verified

**Verified live:** all four agents `active`; each smoke test returned 200 with a correct, contract-valid
result and its model / prompt version / token metadata; each agent identity holds exactly the one
role above (checked across all scopes, and matched against Foundry's own identity listing); the
`triage-security` response metadata reports `model: o4-mini`, so that path works with the same role; the image is `amd64` in the registry with a matching digest.

**Not verified / open:**
- **Latency** of the hosted calls, warm or cold — never measured. (The ~13 s seen in a smoke test is
  mostly the deploy tool's own startup lookups, not the agent.)
- **Hosted-agent cost.** Billing is for CPU + memory while a session is active; sessions are released
  after the idle timeout (default 15 min). The per-hour rate was not looked up. The smoke tests left
  live sessions that wind down on their own.
- **An oddity:** Foundry lists each agent's endpoint protocol as `responses` with an empty
  `container_protocol_versions`, even though the definition requested Invocations 2.0.0. The
  `/invocations` URL works (every smoke test used it). Not understood; worth a look before relying on
  it.
- The **teardown commands below have not been run.**
- The orchestrator, its database, and end-to-end tickets through the hosted agents.

## Calling an agent

```
POST {project_endpoint}/agents/{agent_name}/endpoint/protocols/invocations?api-version=v1
Authorization: Bearer <Entra token, scope https://ai.azure.com/.default>
```
`project_endpoint` is `https://proj-app.services.ai.azure.com/api/projects/proj-app`. Request/response
shapes are in `backend/hosted/specialist.py` (the payload carries the retrieved evidence; the
specialist needs no database).

## Adding or changing an agent

- **New version of an agent:** rebuild the image (`build_image`), then `create_agent_version`. Versions
  are immutable.
- **A new agent role:** add it to `SPECIALIST_ROLES` / `hosted/specialist.py`, then run the four steps
  above for it. It gets a **new identity**, so it needs `grant_agent_model_access` too.
- Never put secrets in the agent's environment: the plan's tests forbid `KEY`/`SECRET`/`TOKEN`/
  `PASSWORD`/`CONNECTION` env names and reserved prefixes, and `.env` is excluded from the image by
  `.dockerignore`.

## Teardown (untested)

Run in this order. Replace the placeholders; `hosted.deploy plan` prints the exact ids.

```bash
# 1. the four hosted agents (removes their versions and sessions)
python - <<'EOF'
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
c = AIProjectClient(endpoint="https://proj-app.services.ai.azure.com/api/projects/proj-app",
                    credential=DefaultAzureCredential())
for name in ("ops-routing", "ops-triage-generic", "ops-triage-security", "ops-triage-trivial"):
    c.agents.delete(agent_name=name, force=True)
EOF

# 2. the registry (this also removes the image) — stops the ~$5/month
az acr delete --name opstriageb24a32 --resource-group proj-foundryapp

# 3. role assignments: each agent identity's 'Cognitive Services OpenAI User' on the account (these
#    become orphaned when the agents are deleted), the user's roles on the project and registry, and
#    the project identity's registry Reader.
az role assignment list --scope <account-id> --query "[?roleDefinitionName=='Cognitive Services OpenAI User']"
az role assignment delete --ids <assignment-id>   # per assignment listed

# 4. (optional) the provider registration is free and harmless to leave:
#    az provider unregister --namespace Microsoft.ContainerRegistry   # only with no registries
```

## Where the code is

| Piece | File |
|---|---|
| One-agent-per-process server (Invocations protocol) | `backend/hosted/server.py` |
| The four specialists | `backend/hosted/specialist.py` |
| Deploy tooling (`plan` / `apply`) | `backend/hosted/deploy.py` |
| Orchestrator → specialist client (retry, circuit breaker, in-process fallback) | `backend/agents/integrations/specialist_client.py` |
| Keyless Entra auth | `backend/agents/llm/client.py` (`AZURE_AUTH_MODE=entra`) |
| Image | `backend/Dockerfile.hosted`, `backend/requirements-base.txt` |
| Tests | `backend/agents/tests/test_hosted_deploy_plan.py`, `test_specialist_agents.py`, `test_azure_auth.py` |
