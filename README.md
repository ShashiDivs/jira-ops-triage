# Ops Triage — Multi-Agent Jira Administration

A multi-agent system that ingests incident/support tickets, classifies severity, routes them
to the right team, and pauses for human approval on anything high-risk before writing the final
ticket into a mock Jira store. Same Intake → Triage → Routing → HITL Gate → Action LangGraph
pipeline and human-in-the-loop policy as the original reference design, rebuilt on:

- **Frontend**: React + Vite (TypeScript)
- **Backend**: Python + FastAPI, LangGraph for agent orchestration, MCP for the tool interface
- **LLM**: Azure AI Foundry (chat completions + embeddings)
- **Data**: PostgreSQL + pgvector, Dockerized — both the RAG knowledge base and the mock Jira
  store live here, and the LangGraph checkpointer is Postgres-backed so pending approvals
  survive a backend restart.

## Human-in-the-loop policy

A ticket requires human sign-off before any action is taken if **any** of the following are true
(`backend/data/runbooks_sla.json` → `RUNBOOK-HITL`, also enforced in code as a safety net):

- Severity is **S1**
- The ticket is security-related (breach, exposure, unauthorized access, etc.), regardless of severity
- Triage confidence is **below 0.7**
- The ticket involves payment, billing, or PII

Everything else is auto-approved and logged for later audit.

## Project layout

```
jira-ops-triage/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── alembic/                  # migrations: pgvector extension + schema
│   ├── data/                     # synthetic historical tickets + runbook docs (RAG corpus)
│   ├── db/                       # SQLAlchemy models + session
│   ├── scripts/seed.py           # embeds data/*.json into Postgres via Azure Foundry
│   ├── rag/retriever.py          # pgvector cosine-similarity search
│   ├── mcp_server/               # mock Jira store + MCP server exposing it as tools
│   ├── agents/                   # LangGraph state/graph/runtime + Azure Foundry LLM client
│   └── api/                      # FastAPI app + routers (tickets, approvals, admin)
└── frontend/                     # React (Vite + TS) dashboard
```

## Setup

```bash
cp .env.example .env
# fill in AZURE_AI_FOUNDRY_ENDPOINT / AZURE_AI_FOUNDRY_API_KEY / deployment names
```

**Note:** unlike the original TF-IDF-based design, RAG retrieval here needs real embeddings —
there's no offline fallback for indexing/search. Triage and routing *do* still fall back to
keyword heuristics if no Azure credentials are set, but ticket submission also calls
`search_similar_tickets` / `search_runbooks`, so a configured Azure AI Foundry embeddings
deployment is required for the pipeline to run end-to-end.

```bash
docker compose up -d postgres
cd backend && pip install -r requirements.txt
alembic upgrade head
python -m scripts.seed          # requires Azure credentials in .env
```

## Running it

**1. MCP server smoke test:**
```bash
python -m mcp_server.test_server
```

**2. Agent graph smoke test** (3 tickets: auto-approved, HITL-approved, HITL-rejected):
```bash
python -m agents.test_graph
```

**3. Full stack:**
```bash
docker compose up -d
```
Backend: http://localhost:8000 · Frontend: http://localhost:5173

To reset all demo state (mock Jira tickets + pending LangGraph checkpoints):
```bash
curl -X POST http://localhost:8000/api/reset
```
