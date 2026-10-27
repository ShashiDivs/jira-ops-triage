# Agents: Component Overview

This file documents the purpose and key responsibilities of each module under `backend/agents`,
organized by subpackage (see the reorganization that grouped these by nature — orchestration,
prompts, LLM client, safety/harness, integrations).

## Summary
- Purpose: implement an Intake → Guardrail → Supervisor → Triage → Routing → HITL → Action
  pipeline using LangGraph.
- Key responsibilities are split by nature into subpackages: prompts (versioned instruction
  text + the DSPy experimental port), the LLM client + structured-output contracts, safety/
  harness patterns (guardrails, access control, resilience), integrations (MCP, observability),
  and the orchestration core at the top level.

---

## Top-level — orchestration core

- **`agents/graph.py`**: LangGraph pipeline definition.
  - Builds the state graph (`build_graph()`) with nodes: `intake`, `guardrail`, `supervisor`,
    `triage` / `triage_security` / `triage_trivial`, `routing`, `hitl_gate`, `action`.
  - Each node is a traced node (observability) and calls into `llm/client.py` helpers and the
    MCP client for RAG lookups.
  - Implements supervisor routing (security vs. trivial vs. generic), per-path triage logic,
    HITL decisioning (including RBAC-gated approve/reject), and final action that updates the
    mock Jira store via MCP tools.

- **`agents/state.py`**: `TicketState` TypedDict describing the shared state shape flowing
  through nodes — `ticket_id`, `summary`, `severity`, `confidence`, `owning_team`,
  `requires_hitl`, `human_approver`, etc.

- **`agents/runtime.py`**: graph lifecycle & checkpointer.
  - Initializes the compiled LangGraph with a Postgres checkpointer (so HITL interrupts persist
    across restarts).
  - Provides `init_graph()`, `shutdown_graph()`, `get_graph()`, and
    `get_pending_interrupt_payload()` (the hitl_reasons/etc. carried by a paused interrupt).

## `agents/prompts/` — versioned instruction text

- **`triage_generic.py`, `triage_security.py`, `triage_trivial.py`, `routing.py`**: each holds
  a `SYSTEM_PROMPT` string and a `PROMPT_VERSION` tag, extracted out of `llm/client.py` so
  prompt wording is diffable/reviewable on its own instead of buried in function bodies. Bump
  `PROMPT_VERSION` whenever the text changes.
- **`dspy_port.py`**: experimental DSPy port of the same four prompts as typed Signatures —
  proves the prompts reproduce today's behavior (see `agents/tests/test_dspy_port.py`), a
  prerequisite for eventual automated prompt optimization. **Not wired into production** —
  `agents/graph.py` still calls `llm/client.py` directly. OpenAI-only for now (see the module's
  own docstring on the Azure gap).

## `agents/llm/` — LLM client & structured output

- **`client.py`**: LLM and embeddings wrapper with graceful fallbacks (was `agents/llm.py`).
  - Provides `embed_text(s)` for embeddings (requires a configured provider).
  - Implements the structured JSON chat helper (`_chat_json`) that validates model output
    against `contracts.py`, retrying once then raising `LLMContractError`.
  - Exposes triage/routing helpers used by graph nodes — `classify_severity`,
    `classify_severity_security`, `classify_severity_fast`, `classify_team`,
    `format_runbook_doc` — each pulling its instruction text from `agents/prompts/`.
  - Implements the keyword-heuristic fallback when no LLM is configured or the circuit breaker
    has tripped.
  - Handles token-usage accounting for observability via a per-node context var.
- **`contracts.py`**: Pydantic output schemas (was top-level `agents/contracts.py`).
  - `TriageResult`: `severity`, `confidence`, `is_security_related`, `reasoning`.
  - `RoutingResult`: `team`, `reasoning`.

## `agents/safety/` — harness/hardening patterns

- **`guardrails.py`**: pre-triage input safety. `check_guardrails(summary, description)`
  returns a block reason for prompt-injection-like content or empty tickets.
- **`access_control.py`**: RBAC for HITL approve/reject decisions. Static name→role map
  (`approver` / `security_approver`), least-privilege enforced on security/PII-flagged tickets.
- **`resilience.py`**: retry and circuit-breaker utilities shared by the LLM and MCP clients.
  `CircuitBreaker` trips after consecutive failures and enforces a cooldown; `call_with_retry`/
  `acall_with_retry` provide retry-with-backoff primitives.

## `agents/integrations/` — external system clients

- **`mcp_client.py`**: MCP (tool) client wrapper. Calls the local `mcp_server` over stdio for
  `search_similar_tickets`, `search_runbooks`, `create_ticket`, `update_ticket`, `add_comment`.
  Read-only RAG lookups degrade to an empty list on failure; writes raise, to avoid silently
  losing state.
- **`observability.py`**: per-node tracing. `traced_node(node_name)` decorator wraps nodes to
  log timing, token usage, and errors, persisting `AgentRun` rows without ever breaking the
  pipeline if persistence itself fails.

## `hosted/` — running the agents as separately-hosted services

Lives beside `agents/` (it serves them; it isn't one of them). See the commit history for the
design rationale.

- **`server.py`**: `python -m hosted.server` serves ONE agent over the Foundry Invocations
  protocol (`POST /invocations`, `GET /readiness`, port `$PORT` or 8088). `OPS_ROLE` picks it:
  `orchestrator`, `triage-generic`, `triage-security`, `triage-trivial`, or `routing`.
- **`specialist.py`**: the four specialists as plain functions (payload -> validated result +
  model/prompt-version/token metadata). Pure model steps: the orchestrator does retrieval and
  sends the evidence along, so a specialist needs no database, MCP, or secrets.
- **`deploy.py`**: `python -m hosted.deploy plan|apply` — a read-only deployment plan, then ONE
  confirmed step at a time against Azure (registry, image build, agent registration). No
  run-everything mode by design. Its Azure-facing commands have not yet been run live.
- What is actually deployed in Foundry, how, what broke, and how to tear it down:
  `docs/hosted-agents-deployment.md`.
- The orchestrator reaches specialists through `agents/integrations/specialist_client.py`
  (configured by `SPECIALIST_URLS`; falls back in-process if a specialist is unavailable).
- `backend/Dockerfile.hosted` builds the image (one image for all roles) from
  `requirements-base.txt` — the runtime set, without the experimental DSPy dependency.

## `agents/tests/`

All `test_*.py` files — contracts, model routing, resilience, access control, guardrails,
observability, supervisor, graph, and the DSPy port smoke test. Invoked as
`python -m agents.tests.test_<name>`.

---

## How the pieces interact (high level)

- `runtime.init_graph()` compiles the graph from `graph.build_graph()` and attaches an
  `AsyncPostgresSaver` checkpointer.
- When a ticket enters the graph, nodes call helpers in `llm/client.py` to classify severity/
  routing, using instruction text from `prompts/`. Those helpers may call the LLM provider or
  fall back to heuristics.
- RAG and mock-Jira operations go through `integrations/mcp_client.py`'s `MCPClient.call_tool()`,
  which talks to `mcp_server`.
- `integrations/observability.py`'s `traced_node` wraps each node to persist per-node metrics,
  including token usage reported by `llm/client.py`'s `_token_usage_var`.
- `safety/resilience.py` primitives protect the LLM client and MCP client so external failures
  degrade gracefully instead of breaking the whole pipeline. `safety/access_control.py` gates
  who may act on a paused HITL decision.

---

File maintained alongside `backend/agents/` — see `backend/agents/docs/GOVERNANCE_RECOMMENDATIONS.md`
for the open governance gaps referenced throughout this project.
