PROJECT STRUCTURE — WHAT EVERY FOLDER AND FILE IS FOR
=====================================================================
Ops Triage: a multi-agent ticket-triage system built to teach agentic AI.
Written 2026-10-29 from the real files (133 files tracked in git).
Plain text on purpose: it reads fine in Notepad.


0. THE SHORT VERSION
---------------------------------------------------------------------
   frontend/    what people see in the browser (React)
   backend/     everything else, split by JOB:

      api/         the front door: HTTP routes the browser calls
      agents/      the BRAIN: the workflow, the AI steps, the safety
      rag/         the MEMORY: search over past tickets and runbooks
      mcp_server/  the TOOLS the agents use (a mock "Jira" + search)
      db/          the DATABASE tables and how we connect to them
      hosted/      run an agent as an Azure Foundry HOSTED AGENT, and
                   the tools that deploy it / the public demo
      scripts/     one-off setup jobs (load the corpus, check Azure)
      data/        the raw knowledge: sample past tickets + runbooks
      alembic/     database migrations (the history of the tables)

   docs/        how-to and deployment records
   reference/   teaching notes, diagrams, the workbook
   .github/     CI: tests that run on every push


1. HOW THE PIECES CALL EACH OTHER (READ THIS FIRST)
---------------------------------------------------------------------

   browser (frontend/)
        |  HTTP + JSON
        v
   api/routers/*  --------------------------------------+
        |                                               |
        v                                               | approve /
   agents/graph.py   <-- the workflow (a state machine) | reject
        |                                               |
        |   AI steps: agents/llm/client.py              |
        |        |--> a model in Foundry  (default)     |
        |        '--> agents/integrations/              |
        |             specialist_client.py              |
        |             --> a HOSTED agent in Foundry     |
        |                (hosted/specialist.py)         |
        |                                               |
        |   tools: agents/integrations/mcp_client.py    |
        |        --> mcp_server/server.py (subprocess)  |
        |              |-- rag/retriever.py  (search)   |
        |              '-- mcp_server/jira_store.py     |
        v                                               v
   db/  (Postgres + pgvector: tickets, corpus, audit, paused runs)

   Plain code: intake, guardrail, supervisor, HITL gate, action.
   AI: triage (3 variants), routing, embeddings.


2. frontend/  — THE BROWSER APP (React + Vite + TypeScript)
---------------------------------------------------------------------
   src/App.tsx                the shell: three tabs + "Reset demo state"
   src/main.tsx               starts React
   src/api/client.ts          EVERY call to the backend lives here, plus
                              the passcode header for the public demo
   src/pages/SubmitTicket.tsx the form + four sample-ticket buttons
   src/pages/PendingApprovals.tsx
                              cards for tickets waiting for a human: the
                              reasons, similar past tickets, Approve/Reject
   src/pages/TicketLog.tsx    all tickets, who decided, "Promote to RAG"
   src/components/PasscodeGate.tsx
                              the passcode prompt shown on a public demo
   src/index.css              all styling
   src/vite-env.d.ts          TypeScript typing for build-time settings
   index.html, vite.config.ts, tsconfig.json, package.json
                              build/tooling configuration
   Dockerfile                 runs the dev server (used by docker compose)
   Teaching point: the frontend has NO AI logic and no business rules.


3. backend/api/  — THE FRONT DOOR (FastAPI)
---------------------------------------------------------------------
   main.py                 builds the app: routes, CORS, the passcode gate,
                           starts/stops the pipeline, and serves the built
                           frontend when STATIC_DIR is set
   routers/tickets.py      POST /api/tickets (submit), GET list/pending/{id},
                           promote-to-rag
   routers/approvals.py    POST .../approve and .../reject, with the RBAC
                           check (who may decide what)
   routers/admin.py        reset demo state, scheduler pause/resume/status
   routers/gate.py         "is a passcode required?" + "is mine right?"
   schemas.py              the request/response shapes
   demo_guard.py           the passcode gate and the ticket limiter used
                           by the public demo


4. backend/agents/  — THE BRAIN
---------------------------------------------------------------------
   graph.py        THE WORKFLOW. Nodes and edges of the LangGraph state
                   machine: intake -> guardrail -> supervisor -> triage
                   (generic / security / trivial) -> routing -> HITL gate
                   -> action. Read this one file to understand the app.
   state.py        the data that flows between nodes (TicketState)
   runtime.py      starts the compiled pipeline and owns its Postgres
                   "checkpointer": a pool of connections that saves paused
                   pipelines so a pending approval survives a restart

   llm/            talking to models
      client.py      chat + embeddings (Azure Foundry or OpenAI), model
                     routing tiers (fast / main / security), keyless
                     Entra login, retry + breaker, and the choice between
                     calling the model directly or a hosted specialist
      contracts.py   the exact output shape a model must return
                     (severity, confidence, ...); wrong shape = retry,
                     then escalate

   prompts/        the instruction text, versioned so any decision can be
                   traced to the wording that produced it
      triage_generic.py, triage_security.py, triage_trivial.py, routing.py
                     one file per AI path, each with a PROMPT_VERSION
      dspy_port.py   the same four prompts re-expressed as DSPy signatures
                     (phase 1 only; no optimisation done yet)

   safety/         the safety net around the AI
      guardrails.py    blocks prompt-injection phrases and empty input
      access_control.py  who may approve what (approver vs
                         security_approver)
      resilience.py    retry with backoff + circuit breaker

   integrations/   the doors to other systems
      mcp_client.py    calls the MCP tools (one subprocess per call)
      observability.py wraps each node: timing, tokens, model, prompt
                       version, saved to the agent_runs table
      specialist_client.py  calls a hosted specialist agent over HTTPS,
                       reuses its session, falls back if it fails

   docs/           AGENTS_OVERVIEW.md (module reference) and
                   GOVERNANCE_RECOMMENDATIONS.md (what is done / open)
   tests/          see section 11


5. backend/rag/  — THE MEMORY (retrieval-augmented generation)
---------------------------------------------------------------------
   retriever.py    search over the corpus in Postgres: vector similarity
                   (pgvector) blended with keyword search (hybrid), a
                   minimum-similarity floor, and logging of every search
   promotion.py    "Promote to RAG": adds an approved ticket to the corpus
                   by hand, refusing exact and near-duplicates
   golden_set.py   15 hand-written test queries with the right answers, used
                   to measure whether retrieval works (recall@k)
   Tests:          test_retriever.py, test_hybrid_search.py,
                   test_promotion.py, test_retrieval_observability.py,
                   test_eval_harness.py
   The corpus itself: backend/data/ (section 8). How it is loaded:
   backend/scripts/seed.py.
   Also see rag.md at the top level: a general explainer of RAG.


6. backend/mcp_server/  — THE TOOLS
---------------------------------------------------------------------
   server.py       an MCP server exposing seven tools: create_ticket,
                   update_ticket, add_comment, get_ticket, list_tickets,
                   search_similar_tickets, search_runbooks
   jira_store.py   the mock "Jira": tickets and comments stored in Postgres
   test_server.py  a standalone smoke test of the tools
   Why MCP: the agents call tools through a standard interface, so
   replacing the mock Jira with a real one changes only this folder.


7. backend/db/  — THE DATABASE
---------------------------------------------------------------------
   models.py       the tables: tickets, comments, historical_tickets
                   (past tickets, with embeddings), runbooks (policies,
                   with embeddings), agent_runs (one row per node: time,
                   tokens, model, prompt version), retrieval_log (one row
                   per search). The checkpoint tables are created by
                   LangGraph itself.
   session.py      the connection engine (password or keyless Entra)
   entra.py        keyless login to Azure Postgres: a short-lived token
                   instead of a stored password
   alembic/        the migrations, in order:
      0001 initial schema + pgvector       0005 corpus provenance/dedup
      0002 agent_runs (observability)      0006 full-text search indexes
      0003 which embedding model           0007 model + prompt version on
      0004 retrieval_log                        agent_runs
   alembic.ini     migration tool settings


8. backend/data/, scripts/, scheduler.py, config.py
---------------------------------------------------------------------
   data/historical_tickets.json   10 sample past tickets (the RAG corpus)
   data/runbooks_sla.json         5 runbooks: severity policy, security
                                  runbook, outage runbook, component
                                  ownership, human-approval policy
   scripts/seed.py            wipes and reloads the corpus (embeds it)
   scripts/seed_if_empty.py   loads it only if empty (used by the demo)
   scripts/check_azure.py     checks the model provider works
   scripts/init_checkpointer.py  creates the pause/resume tables once
   scheduler.py               optional: submits a sample ticket on a timer
                              (off by default; each one spends model calls)
   config.py                  EVERY setting, read from the environment or
                              .env (one place; see .env.example)


9. backend/hosted/  — AGENTS AS AZURE FOUNDRY HOSTED AGENTS
---------------------------------------------------------------------
   server.py      the entry point a hosted container runs; OPS_ROLE picks
                  which agent it is (a specialist or the orchestrator)
   specialist.py  the four specialist agents as plain functions: request in,
                  validated answer + model/prompt/tokens out
   deploy.py      deploys the four agents to Foundry: plan, then ONE step
                  at a time behind a y/N gate (also holds the 15-step
                  orchestrator plan we decided not to run)
   demo.py        brings the public demo up on Azure Container Apps in 11
                  gated steps, and takes it all down with one command
   Dockerfile.hosted (in backend/) builds the agent image;
   Dockerfile.demo (at the top) builds the one-container demo image.


10. TOP-LEVEL FILES
---------------------------------------------------------------------
   docker-compose.yml     local stack: postgres, backend, frontend, adminer
   .env.example           every setting, documented (the real .env is
                          gitignored and holds the secrets)
   .dockerignore, backend/.dockerignore, frontend/.dockerignore
                          what must NOT go into images (notably .env)
   Dockerfile.demo        the one-image demo build (React + API)
   backend/Dockerfile, backend/Dockerfile.hosted, frontend/Dockerfile
   backend/requirements-base.txt   what the app needs
   backend/requirements.txt        base + DSPy (for the DSPy experiment)
   README.md              front page: what it is, quick start, limits
   rag.md                 a general RAG explainer
   test.py, backend/test_scheduler.py
                          test.py is a SCRATCH file (a one-off model
                          connectivity check; safe to delete).
                          test_scheduler.py is a real smoke test for the
                          scheduler.
   .github/workflows/ci.yml   the tests that run on every push:
                          a free tier (no secrets) and an integration tier

   docs/
      submitting-tickets.md        sample tickets for each severity
      hosted-agents-deployment.md  record of the hosted-agent deployment
      public-demo.md               runbook: demo up, during, down
   reference/   (teaching material)
      project-workflow-teaching-notes.md  the app's flow, with a demo script
      deployment-process-explained.md     what we deployed and why
      deployment-workflow.excalidraw      the two workflow diagrams
      agentic-teaching-notes.md           pattern-by-pattern walkthrough
      agentic_engineering_layers.xlsx     prompt/context/harness/loop map
      *.drawio                            pipeline, RAG, scheduler diagrams


11. THE TESTS (backend/agents/tests/ and backend/rag/)
---------------------------------------------------------------------
   FREE (no database, no model calls; run on every push):
      test_contracts          output shapes are enforced
      test_model_routing      the right model tier is chosen
      test_resilience         retry + circuit breaker
      test_access_control     who may approve what
      test_prompt_versioning  prompts are versioned and traceable
      test_azure_auth         key vs keyless login
      test_database_auth      keyless database login
      test_demo_guard         passcode gate and ticket limits
      test_specialist_agents  the four specialists
      test_hosted_deploy_plan the agent deploy tool
      test_demo_deploy_plan   the demo deploy tool
   NEED POSTGRES (and sometimes a model provider; small real calls):
      test_graph, test_guardrails, test_supervisor, test_observability,
      test_checkpointer_resilience (kills the DB connection on purpose),
      test_dspy_port (OpenAI key only), and the rag/ tests listed above


12. "I WANT TO CHANGE X — WHICH FILE?"
---------------------------------------------------------------------
   change what the AI is told ................. agents/prompts/*.py
                                                (bump PROMPT_VERSION)
   change the workflow order or add a step ..... agents/graph.py
   change who needs human approval ............. agents/graph.py
                                                (_hitl_reasons)
   change who may approve ...................... APPROVER_ROLES in .env;
                                                agents/safety/access_control.py
   change search quality ....................... rag/retriever.py
   add knowledge ............................... backend/data/*.json then
                                                scripts/seed.py
   add a new tool for the agents ............... mcp_server/server.py
   add a database column ....................... db/models.py + a new
                                                alembic migration
   add a page ................................. frontend/src/pages/ and
                                                frontend/src/api/client.ts
   change a setting ............................ config.py + .env.example
   deploy / take down the demo ................. hosted/demo.py


13. A READING ORDER FOR STUDENTS
---------------------------------------------------------------------
   1  README.md                              what it is
   2  reference/project-workflow-teaching-notes.md   how a ticket flows
   3  backend/agents/graph.py                the workflow itself
   4  backend/agents/llm/contracts.py + prompts/   what the AI is asked
   5  backend/agents/safety/                  how it is kept safe
   6  backend/rag/retriever.py                how it remembers
   7  backend/mcp_server/server.py            how it uses tools
   8  backend/hosted/ + docs/hosted-agents-deployment.md   how it is deployed


14. WHAT IS NOT IN GIT (AND WHY)
---------------------------------------------------------------------
   .env (real secrets), node_modules/, dist/ (built frontend),
   __pycache__/, the Postgres data volume, virtual environments.
   backend/.env is a link to the top-level .env.
