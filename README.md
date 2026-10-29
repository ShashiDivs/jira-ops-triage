# Ops Triage — a multi-agent ticket-triage system, built to teach agentic AI

Someone reports an incident ("checkout is down"). The system reads it, decides how severe it is,
decides which team owns it, and — if the ticket is risky — **stops and waits for a human to approve
it** before anything is written. Small AI steps do the judgement; ordinary, tested code does the
safety, routing and record-keeping.

It is a working example of the pieces a real agentic system needs, kept small enough to read in a day:

| Concept | Where to see it |
|---|---|
| Workflow as a state machine, with a human pause | LangGraph — `backend/agents/graph.py` |
| Tools behind a standard interface | MCP server — `backend/mcp_server/` |
| Grounding answers in your own data (RAG) | pgvector, hybrid search, promotion — `backend/rag/` |
| Typed outputs, retries, guardrails, circuit breakers | `backend/agents/llm/`, `backend/agents/safety/` |
| Who may approve what (RBAC) and an audit trail | `backend/agents/safety/access_control.py` |
| Model routing (cheap model vs. reasoning model) | `backend/agents/llm/client.py` |
| Versioned prompts, traceable per decision | `backend/agents/prompts/` + `agent_runs` table |
| Agents deployed to **Azure AI Foundry as hosted agents** | `backend/hosted/`, [docs/hosted-agents-deployment.md](docs/hosted-agents-deployment.md) |

**New here? Start with [reference/project-workflow-teaching-notes.md](reference/project-workflow-teaching-notes.md)** —
a plain-text walkthrough of the whole system (frontend, backend, hosted agents) with a 20-minute classroom demo script.

## How it works

```
 Browser (React)  ->  FastAPI  ->  LangGraph pipeline  ->  Postgres + pgvector
                                     |        |    |         (tickets, RAG corpus,
                                     |        |    |          audit tables, paused runs)
                                     |        |    +--> MCP tool server (a subprocess)
                                     |        +-------> models (Azure AI Foundry)
                                     +--> pauses here until a human decides

 intake -> guardrail -> supervisor -> triage -> routing -> hitl_gate -> action
              |            |                                  |
          blocked      security / trivial / generic       PAUSE if risky
```

- **Plain code:** intake, guardrail (prompt-injection check), supervisor (keyword rules pick the path),
  HITL gate, action. **AI steps:** triage (three variants) and routing.
- The three triage paths use different models: a fast one for cosmetic tickets, the main one for
  generic tickets, and a stronger reasoning model for security tickets.

### Human-in-the-loop policy

A ticket needs human sign-off if **any** of these is true (`backend/data/runbooks_sla.json` →
`RUNBOOK-HITL`, also enforced in code as a safety net):

- severity is **S1**
- the ticket is security-related, whatever its severity
- triage confidence is **below 0.7**
- it involves payment, billing, or PII

Everything else is auto-approved and logged. Approval is role-based: an `approver` can decide
ordinary tickets; security and PII tickets need a `security_approver`. The server enforces this —
the "Approving as" box in the browser is only a claim.

## Quick start

You need Docker, and a model provider. Pick **one**:

| Provider | Set in `.env` |
|---|---|
| Azure AI Foundry, API key | `AZURE_AI_FOUNDRY_ENDPOINT`, `AZURE_AI_FOUNDRY_API_KEY`, the `AZURE_CHAT_DEPLOYMENT*` names |
| Azure AI Foundry, **keyless** (Entra) | the endpoint, `AZURE_AUTH_MODE=entra`, and `az login` on the host |
| OpenAI (simplest to try) | `OPENAI_API_KEY` — used instead of Azure whenever it is set |

```bash
cp .env.example .env        # then fill in one provider (never commit .env)
docker compose up -d
docker compose exec backend python -m scripts.seed    # embeds the RAG corpus (one time)
```

Open **http://localhost:5173**. The API is on :8000 and a database browser (Adminer) on :8080.
Migrations run automatically when the backend starts.

> RAG search needs a real embeddings deployment, so a configured provider is required to run the full
> pipeline. With no provider, triage and routing fall back to keyword rules, but ticket submission still
> searches the corpus and will fail.

Set `APPROVER_ROLES` (already `alice:approver,bob:security_approver` in `.env.example`) — an empty value
refuses every approval, on purpose.

### Try it (2 minutes)

1. **Submit** tab → *Footer typo* → Submit. It finishes in seconds and is auto-approved.
2. *Outage* → Submit. Open **Pending approvals**: see the severity, why a human is needed, and the similar
   past tickets the model was shown.
3. Approve it as `alice`. Then submit *PII exposure* — `alice` is refused (403); `bob` can approve it.
4. **Ticket log** → *Promote to RAG* on an approved ticket to add it to the knowledge base.

More example tickets for each severity: [docs/submitting-tickets.md](docs/submitting-tickets.md).
Reset demo state at any time with the *Reset demo state* button, or `curl -X POST localhost:8000/api/reset`.

## Putting it on a public URL

For a supervised demo you can run the whole app in Azure Container Apps and delete it afterwards with one
command — passcode-gated, ticket-capped, about $1–4 for a working day. See [docs/public-demo.md](docs/public-demo.md).

## Project layout

```
jira-ops-triage/
├── docker-compose.yml          postgres (pgvector), backend, frontend, adminer
├── .env.example                every setting, documented
├── backend/
│   ├── api/                    FastAPI app + routers (tickets, approvals, admin)
│   ├── agents/
│   │   ├── graph.py            the workflow: nodes and how they connect
│   │   ├── runtime.py          starts the pipeline; Postgres checkpointer (pauses survive restarts)
│   │   ├── llm/                model client (key / Entra auth, routing tiers) + output contracts
│   │   ├── prompts/            versioned prompt text per agent (+ DSPy port)
│   │   ├── safety/             guardrails, RBAC, retry + circuit breaker
│   │   ├── integrations/       MCP client, observability, specialist (hosted-agent) client
│   │   ├── tests/              unit and integration suites
│   │   └── docs/               AGENTS_OVERVIEW.md, GOVERNANCE_RECOMMENDATIONS.md
│   ├── rag/                    retriever (hybrid search), promotion, golden-set evaluation
│   ├── mcp_server/             mock Jira store + the MCP server that exposes it as tools
│   ├── hosted/                 run an agent as a Foundry hosted agent + the deploy tool
│   ├── db/  alembic/  data/    models + migrations + the synthetic RAG corpus
│   └── scripts/                seed, checkpoint setup, Azure checks
├── frontend/                   React + Vite + TypeScript dashboard
├── Dockerfile.demo             one image: React app + API, for the public demo
├── docs/                       hosted-agents-deployment.md, public-demo.md, submitting-tickets.md
└── reference/                  teaching notes, diagrams (.drawio), engineering-layers workbook
```

## Hosted agents on Azure AI Foundry

Four of the five agents also run as **hosted agents** in Foundry — each a container with its own name,
version, endpoint and Entra identity (no API keys): `ops-triage-generic`, `ops-triage-security`,
`ops-triage-trivial`, `ops-routing`. They are pure model steps; the orchestrator does retrieval and sends
the evidence along.

- **Off by default.** The pipeline calls models in-process unless you set `SPECIALIST_URLS` (and
  `SPECIALIST_AUTH=entra`). If a specialist is down, the orchestrator falls back in-process.
- **The orchestrator is deliberately not hosted:** it is a workflow over a database, not a model step.
  The reasoning is in the deployment doc.
- Measured on one laptop: a warm hosted call costs about 0–1.4 s more than in-process; a cold start
  (new container session) is 6–8 s.
- Deploying is a step-by-step tool, never one button — every step spends money or changes permissions:
  `python -m hosted.deploy plan --agent routing` (read-only), then `apply --step <name>` behind a y/N gate.

Full record — what exists, what went wrong, measured behaviour, what is not verified:
[docs/hosted-agents-deployment.md](docs/hosted-agents-deployment.md).

## Tests

```bash
cd backend && pip install -r requirements.txt
# no database, no model calls — these run on every push:
python -m agents.tests.test_contracts
python -m agents.tests.test_model_routing
python -m agents.tests.test_resilience
python -m agents.tests.test_access_control
python -m agents.tests.test_prompt_versioning
python -m agents.tests.test_azure_auth
python -m agents.tests.test_database_auth
python -m agents.tests.test_specialist_agents
python -m agents.tests.test_hosted_deploy_plan
python -m agents.tests.test_demo_guard
python -m agents.tests.test_demo_deploy_plan
```

These need Postgres running and a configured model provider (they make real, small, paid calls):
`agents.tests.test_graph`, `test_guardrails`, `test_supervisor`, `test_observability`, `rag.test_retriever`,
`rag.test_hybrid_search`, `rag.test_promotion`, `rag.test_retrieval_observability`, and
`rag.test_eval_harness` (recall@k on a 15-query golden set: hybrid search beats vector-only).

## CI

`.github/workflows/ci.yml` runs two tiers:

- **Free tier** (every push and PR, no secrets): the unit suites above, plus a frontend type-check and build.
- **Integration tier** (push to `main` only): the full pipeline and RAG suites against a Postgres+pgvector
  service container, with real model calls — so it costs a little each run. It **currently uses OpenAI**, not
  Azure, and skips cleanly if the secret is missing. To enable it: *Settings → Secrets and variables →
  Actions → New repository secret* → `OPENAI_API_KEY`.

## Known limits

Say these out loud when teaching — they are part of the lesson.

- The "Jira" is a mock and the RAG corpus is tiny (10 tickets, 5 runbooks), so retrieval looks better than it
  would on real data.
- Severity answers vary between runs on vague tickets; the confidence threshold sends the uncertain ones to a
  human. That is the safety net working, not a bug.
- `agents/` shadows the OpenAI Agents SDK's package name; the hosted containers log a harmless import error at
  startup because of it.
- `test_dspy_port` only works with an OpenAI key (the DSPy port is OpenAI-only), and DSPy optimization is not
  done yet.
- Token counts in `agent_runs` undercount when a provider omits usage data (DeepSeek on Azure does about 60%
  of the time).
- An intermittent model-side 401 hit about 3 of 64 hosted calls, cause unknown.
- Governance items beyond RBAC (see `backend/agents/docs/GOVERNANCE_RECOMMENDATIONS.md`) are not all done.
