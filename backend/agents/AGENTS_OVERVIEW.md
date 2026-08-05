# Agents: Component Overview

This file documents the purpose and key responsibilities of each module under `backend/agents`.

## Summary
- Purpose: implement an Intake → Triage → Routing → HITL → Action pipeline using LangGraph.
- Key responsibilities are split across small modules: LLM client & fallbacks, agent contracts,
  circuit breakers/retries, MCP tool client (mock-Jira + RAG), graph nodes, observability, and
  runtime wiring.

---

## Modules

- **`backend/agents/__init__.py`**: package marker (empty).

- **`backend/agents/contracts.py`**: Pydantic output schemas used for structured-agent output.
  - `TriageResult`: JSON schema for triage responses: `severity`, `confidence`, `is_security_related`, `reasoning`.
  - `RoutingResult`: JSON schema for routing output: `team`, `reasoning`.

- **`backend/agents/llm.py`**: LLM and embeddings wrapper with graceful fallbacks.
  - Provides `embed_text(s)` for embeddings (requires configured provider).
  - Implements structured JSON chat helper (`_chat_json`) that validates model output against `contracts.py`.
  - Exposes triage/routing helpers used by graph nodes:
    - `classify_severity`, `classify_severity_security`, `classify_severity_fast`
    - `classify_team`
    - `format_runbook_doc`
  - Implements heuristics fallback (keyword-based) when no LLM is configured or a circuit-breaker trips.
  - Handles token-usage accounting for observability via a per-node context var.

- **`backend/agents/graph.py`**: LangGraph pipeline definition.
  - Builds the state graph (`build_graph()`) with nodes:
    - `intake`, `guardrail`, `supervisor`, `triage` / `triage_security` / `triage_trivial`, `routing`, `hitl_gate`, `action`.
  - Each node is a traced node (observability) and calls into `llm.py` helpers and the MCP client for RAG lookups.
  - Implements supervisor routing (security vs trivial vs generic), per-path triage logic, HITL decisioning,
    and final action that updates the mock Jira store via MCP tools.

- **`backend/agents/mcp_client.py`**: MCP (tool) client wrapper.
  - Calls the local `mcp_server` over stdio to use tools such as `search_similar_tickets`, `search_runbooks`,
    `create_ticket`, `update_ticket`, and `add_comment`.
  - Uses an internal circuit breaker and retry logic; read-only RAG lookups degrade to an empty list
    if the MCP server is failing, while writes raise to avoid silently losing state.
  - Exposes `get_mcp_client()` returning an `MCPClient` with `call_tool()`.

- **`backend/agents/runtime.py`**: graph lifecycle & checkpointer.
  - Initializes the compiled LangGraph with a Postgres checkpointer (so HITL interrupts persist across restarts).
  - Provides `init_graph()`, `shutdown_graph()`, and `get_graph()` for app startup and other code.

- **`backend/agents/state.py`**: `TicketState` TypedDict describing the shared state shape flowing through nodes.
  - Lists expected keys such as `ticket_id`, `summary`, `severity`, `confidence`, `owning_team`, `requires_hitl`, etc.

- **`backend/agents/guardrails.py`**: pre-triage input safety checks.
  - `check_guardrails(summary, description)` returns a block reason for prompt-injection-like content or empty tickets.
  - Runs before supervisor/triage to block malformed or clearly malicious inputs.

- **`backend/agents/observability.py`**: per-node tracing and persistence.
  - `traced_node(node_name)` decorator wraps nodes to log timing, token usage, and errors.
  - Persists `AgentRun` rows to the DB (`db.models.AgentRun`) but never breaks the pipeline if persistence fails.

- **`backend/agents/resilience.py`**: retry and circuit-breaker utilities used across agents.
  - `CircuitBreaker` trips after consecutive failures and enforces a cooldown.
  - `call_with_retry` and `acall_with_retry` provide retry-with-backoff primitives.

---

## Tests
- There are unit tests covering contracts, graph behaviour, guardrails, observability, resilience, and supervisor paths (`test_*.py`).

## How the pieces interact (high level)
- `runtime.init_graph()` compiles the graph created by `graph.build_graph()` and attaches an AsyncPostgresSaver checkpointer.
- When a ticket enters the graph, nodes call LLM helpers in `llm.py` to classify severity/routing. Those helpers may call the LLM provider or fall back to heuristics.
- RAG and mock-Jira operations are performed via `mcp_client.MCPClient.call_tool()` which talks to `mcp_server`.
- `observability.traced_node` wraps each node to persist per-node metrics, including token usage reported by `llm._token_usage_var`.
- `resilience` primitives protect the LLM client and MCP client so external failures degrade gracefully instead of breaking the whole pipeline.

## Suggested next steps (if you want improvements)
- Add README cross-links from the repository root to this file for discoverability.
- Expand examples showing a full ticket through the graph with sample inputs/outputs.
- Add API-level docs linking router endpoints (`backend/api/`) to graph entrypoints.

---

File created by automation: `backend/agents/AGENTS_OVERVIEW.md`
