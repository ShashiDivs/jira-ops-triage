# Agentic AI Systems — Teaching Notes
## Case study: the Ops Triage multi-agent pipeline (jira-ops-triage)

These notes walk through every "agentic" element we built, in the order a student should
learn them: what it is, which file implements it, and why it exists — not as abstract theory,
but pointing at a real, running system.

---

## 1. What actually makes something "agentic"?

A chatbot answers one question and stops. An **agentic system**:
- makes a *sequence* of decisions, not just one
- calls *tools* to affect or read the outside world (not just generate text)
- can *branch* — different input takes a different path through the system
- has defined places where a **human**, not the model, makes the final call

Everything below is one of those four ideas, made concrete.

---

## 2. The orchestration framework: LangGraph

File: `backend/agents/graph.py`

Three concepts a student needs before anything else makes sense:

- **State** — one shared dictionary (`TicketState`, in `agents/state.py`) that flows through
  every step. Each node reads it, does something, and returns an updated version.
- **Nodes** — plain Python functions, one per step (`intake_node`, `guardrail_node`,
  `triage_generic_node`, ...). This is the "agent" unit — you can have as many as you want.
- **Conditional edges** — `add_conditional_edges()` lets a node's *output* decide which node
  runs next. This is what turns a straight pipeline into a system with real branching logic.

```
build_graph():
    START -> intake -> guardrail -> [blocked_action | supervisor]
    supervisor -> [triage_security | triage_trivial | triage] -> routing
    routing -> hitl_gate -> action -> END
```

---

## 3. Walking through every agent, in order

### 3.1 Intake
`agents/graph.py :: intake_node`
The simplest possible node — normalizes the raw ticket into state. No decisions, no LLM call.
Included mainly so every ticket has one obvious entry point to trace from.

### 3.2 Guardrail — input-safety agent
`agents/guardrails.py`, `agents/graph.py :: guardrail_node`
Runs **before any LLM sees the ticket**. Checks for prompt-injection phrasing ("ignore
previous instructions", etc.) and degenerate input (empty ticket). If it trips, the ticket is
marked `blocked` and the graph skips straight past every other agent — no wasted model calls,
no exposure to a hostile prompt.
**Teaching point:** a production agent system has to defend against adversarial input, not
just handle well-behaved input. This is the cheapest, earliest place to do it.

### 3.3 Supervisor — the orchestrator agent
`agents/graph.py :: supervisor_node`
Its *only* job is deciding which specialist handles this ticket — it does no triage itself.
Routes on keyword matches (`SECURITY_KEYWORDS`, `COSMETIC_KEYWORDS` in `agents/llm.py`):
- security language → **security specialist**
- cosmetic language → **fast/cheap path**
- everything else → **generic path**
**Teaching point:** this is the core "multi-agent" idea. Instead of one do-everything prompt,
you have a dispatcher and several specialists — each one simpler and better at its narrow job
than a single generalist prompt would be.

### 3.4 Triage — three specialist agents behind one contract
`agents/llm.py :: classify_severity_security / classify_severity_fast / classify_severity`

| Path | Grounded in | Model | Notable behavior |
|---|---|---|---|
| Security | security runbook (RAG) | main model | always forces `is_security_related=True`, no matter what the model concludes — defense in depth |
| Trivial/fast | nothing (skips RAG) | cheap model | optimizes cost for the "probably nothing" case |
| Generic | severity policy + similar tickets (RAG) | main model | the original, full-context path |

All three return the *same* schema (`TriageResult`), so every node downstream doesn't need to
know or care which specialist ran. That shared schema is the **Agent Contract**.

### 3.5 Routing agent
`agents/graph.py :: routing_node`, `agents/llm.py :: classify_team`
Assigns an owning team, grounded in a component→team ownership runbook (RAG again).

### 3.6 HITL Gate — the human-in-the-loop checkpoint
`agents/graph.py :: hitl_gate_node`
The single most important pattern in this whole system: **agents decide, a human approves
anything risky.** Implemented with LangGraph's `interrupt()` — this doesn't just "wait", it
actually *pauses the running graph* and persists that paused state to Postgres, so it survives
a server restart until a human calls the approve/reject API.
Policy (mirrors `RUNBOOK-HITL`): pause if severity is S1, the ticket is security-related,
confidence is below 0.7, or it involves payment/billing/PII. Otherwise, auto-approve.
**Teaching point:** full autonomy is often the *wrong* design goal. The actual skill is
deciding exactly where a human checkpoint belongs — and building the mechanism to pause there.

### 3.7 Action agent
`agents/graph.py :: action_node`
The only node with a real side effect on the outside world: writes the final decision and a
full audit trail (every prior agent's reasoning) into the ticket system. Every node before
this one was purely about *deciding* — this one *acts*.

---

## 4. The tools agents use

### 4.1 MCP (Model Context Protocol) — the tool interface
`mcp_server/server.py`, `agents/mcp_client.py`
Agents never call a Python function directly to touch Jira or the knowledge base — they call
a named **MCP tool** (`create_ticket`, `search_similar_tickets`, ...) over a standard protocol.
**Teaching point:** this means the entire backend (mock Jira → real Jira Cloud API) can be
swapped without touching a single line of agent logic. The agent only ever knows a tool's
name and shape, never its implementation.

### 4.2 RAG — Retrieval-Augmented Generation
`rag/retriever.py`, Postgres + pgvector
Before classifying, an agent retrieves similar historical tickets and relevant policy text by
vector similarity search, and puts them directly in the prompt.
**Teaching point:** this is how you ground an LLM's decision in *your organization's own
history and policy*, instead of relying only on what it learned during training.

---

## 7. Extension: scheduled/autonomous ticket generation

Diagram: `reference/scheduled-ticket-workflow.drawio`

Everything above is *reactive* — a human clicks Submit and the pipeline runs once. A natural
extension is to add a **scheduled** trigger: every N seconds, an `APScheduler` job inside the
backend (no new service/container) picks a template ticket from a pool spanning every severity
and submits it through the exact same `POST /api/tickets` the UI calls.

**Why this design and not, say, Airflow:** Airflow is a full orchestration platform (its own
webserver, scheduler, executor, metadata DB) — appropriate if teaching orchestration tooling is
itself a second objective, but a lot of infrastructure just to get "run this every 5 minutes."
`APScheduler` gets the same teaching payoff — *agents don't only respond to a click, they can
run on their own clock* — in a few lines, without a second platform competing for the lecture's
attention. The entry point either way is just an HTTP call, so swapping the trigger mechanism
later doesn't touch a single line of agent code.

**Key design point:** scheduled tickets flow through the *identical* pipeline as manual ones —
same `graph.py`, same `@traced_node` tracing — so they land in `tickets` (mock Jira log) and
`agent_runs` (trace table) exactly like any other ticket, but **never automatically** touch
`historical_tickets` (the curated RAG seed corpus). Promoting a resolved ticket into the RAG
corpus is a deliberate, separate, visible action — not a side effect of a timer — because
silently growing your retrieval corpus with unreviewed synthetic examples is a real way to
degrade RAG quality over time, which is itself worth teaching on purpose rather than by
accident.

**Cost/safety control:** `SCHEDULER_ENABLED` env var plus `/api/scheduler/pause` and `/resume`
endpoints — an unattended 5-minute loop calling a real LLM adds up if left running overnight.

---

## 5. Why "it works in a demo" isn't "it's a real system" — six hardening passes

| # | Pattern | What it stops |
|---|---|---|
| 1 | Agent contracts | An LLM returning malformed output silently corrupting a ticket |
| 2 | Guardrails | Prompt injection / garbage input reaching an agent at all |
| 3 | Orchestrator routing | One generalist prompt trying to handle every kind of ticket equally badly |
| 4 | Model routing | Paying capable-model prices for trivial tickets |
| 5 | Failure containment | One flaky dependency cascading into every ticket failing |
| 6 | Observability | Having no idea which agent is slow, expensive, or erroring in production |

Each of these was added *and tested* one at a time — every single one surfaced a real bug that
wasn't visible until that phase's test actually ran the system end-to-end.

---

## 6. Discussion / exercise ideas for students

- Trace one ticket by hand through the diagram: pick "Unauthorized access to admin panel" and
  list, in order, every agent it visits and every tool call each one makes.
- Why does the *security* triage path force `is_security_related=True` even if the model
  disagrees? What failure mode does that prevent?
- The fast/cheap triage path skips RAG entirely. What's the risk of that shortcut, and why is
  it an acceptable tradeoff here specifically?
- Where would you add a *seventh* hardening pattern? What real failure would it prevent?
