PROJECT WORKFLOW — TEACHING NOTES
Frontend + Backend + Hosted Agents
=====================================================================
Written 2026-10-27 from the real code and the real deployment. Where a
number appears, it was measured (see "Honest limits" at the end).
Plain text on purpose: it reads fine in Notepad.


0. THE ONE-PARAGRAPH VERSION
---------------------------------------------------------------------
Someone reports an incident ("checkout is down"). The system reads it,
decides how severe it is, decides which team owns it, and -- if the ticket
is risky -- STOPS and waits for a human to approve it before anything is
written. Several small AI steps do the thinking; ordinary code does the
safety, the routing and the record-keeping. Four of those AI steps now also
run as separate "hosted agents" inside Azure AI Foundry.


1. THE BIG PICTURE
---------------------------------------------------------------------

   BROWSER (React, port 5173)
      |   plain HTTP/JSON
      v
   API (FastAPI, port 8000) ----------------------------------+
      |                                                        |
      v                                                        |
   LANGGRAPH PIPELINE  (the "workflow": a state machine)       |
      |        |             |                |                |
      |        |             |                |                v
      |        |             |                |        POSTGRES (pgvector)
      |        |             |                |         - tickets, comments
      |        |             |                |         - RAG corpus
      |        |             |                |         - agent_runs, logs
      |        |             |                |         - pipeline checkpoints
      |        |             |                v
      |        |             |        MCP TOOL SERVER (a subprocess)
      |        |             |         search_similar_tickets, search_runbooks,
      |        |             |         create/update_ticket, add_comment ...
      |        |             v
      |        |       RETRIEVAL (RAG): "what happened last time?"
      |        v
      |   MODELS (Azure AI Foundry): DeepSeek-V3.2, o4-mini, embeddings
      v
   HUMAN IN THE LOOP: the pipeline PAUSES here until a person decides

Everything runs locally with `docker compose up` (postgres, backend,
frontend, adminer). Foundry is only used for the models, unless you also
run the hosted-agent topology (section 9).


2. THE FRONTEND
---------------------------------------------------------------------
Three tabs, one small React app. There is no login: the reviewer types
their name into "Approving as" (kept in the browser's localStorage) and the
SERVER decides whether that name is allowed (section 5).

  SUBMIT
    A form (summary, description, component, reporter) plus four preset
    buttons (Outage, PII exposure, Slow search, Footer typo) that fill it in.
    Calls:  POST /api/tickets
    The call returns when the pipeline finishes OR reaches the approval
    gate -- so it takes several seconds. That wait IS the pipeline running.

  PENDING APPROVALS
    One card per ticket waiting for a human. Each card shows: the ticket,
    the severity badge (S1 red ... S4 grey), the team, the model's
    confidence, WHY a human is needed (amber tags), and -- inside "Agent
    reasoning & evidence" -- the model's reasoning plus the similar past
    tickets it was shown, each with a similarity percentage. The reviewer
    types notes and clicks Approve or Reject.
    Calls:  GET /api/tickets/pending  (re-polled every 5 seconds)
            POST /api/tickets/{id}/approve | /reject

  TICKET LOG
    Every ticket: severity, team, status, who decided, when. Approved
    tickets have a "Promote to RAG" button (section 6).
    Calls:  GET /api/tickets  (re-polled every 8 seconds)
            POST /api/tickets/{id}/promote-to-rag

  RESET DEMO STATE (top right)
    POST /api/reset -- wipes tickets and paused pipelines for the next demo.

TEACHING POINT: the frontend contains no AI logic and no business rules.
It only shows state and sends decisions. All judgement lives in the backend.


3. THE BACKEND FOLDER MAP
---------------------------------------------------------------------
  backend/
    api/            FastAPI routes (tickets, approvals, admin)
    agents/
      graph.py      THE WORKFLOW: the nodes and how they connect
      state.py      the data that flows through the nodes (TicketState)
      runtime.py    starts the pipeline, owns the Postgres checkpointer
      llm/          client.py (talks to models) + contracts.py (output shapes)
      prompts/      the instruction text for each AI step (versioned: "v1")
      safety/       guardrails.py, access_control.py (RBAC), resilience.py
      integrations/ mcp_client.py, observability.py, specialist_client.py
      tests/        the test suites
    rag/            retriever.py (search), promotion.py, golden_set.py, evals
    mcp_server/     the tool server + the mock "Jira" store
    db/             database tables
    hosted/         server.py, specialist.py, deploy.py  (section 9)
    scheduler.py    optional: generates demo tickets on a timer (off by default)


4. LIFE OF A TICKET  (the workflow, step by step)
---------------------------------------------------------------------
The pipeline is a LangGraph state machine. Each box below is a "node". A
node reads the ticket state, does one job, and writes its result back.

  START
    |
    v
  [intake]        Plain code. Records the ticket as received.
    |
    v
  [guardrail]     Plain code. Looks for prompt-injection phrases ("ignore
    |             previous instructions", "system prompt" ...) or an empty
    |             ticket. If found: ticket is BLOCKED and the pipeline ends.
    v
  [supervisor]    Plain code, NOT an AI. Keyword rules choose a path:
    |               - security words (breach, unauthorized, exposed, leak,
    |                 compromise, exploit, hacked)        -> SECURITY path
    |               - cosmetic words (typo, copy, footer, spelling,
    |                 cosmetic)                           -> TRIVIAL path
    |               - everything else                     -> GENERIC path
    v
  [triage_*]      THE FIRST AI STEP. One of three, depending on the path:
    |               generic : looks up similar past tickets + the severity
    |                         policy, asks the model for S1-S4 + confidence
    |               security: same, but grounded in the security runbook and
    |                         uses the stronger model (o4-mini)
    |               trivial : no lookups, cheapest model, just "confirm or
    |                         correct this cosmetic guess"
    |             Output is a fixed shape: severity, confidence (0-1),
    |             is_security_related, reasoning.
    v
  [routing]       SECOND AI STEP. Looks up the component-ownership runbook,
    |             asks the model which team owns this component.
    v
  [hitl_gate]     Plain code. Decides whether a human must look (below).
    |             If yes: PAUSE here. If no: auto-approve and continue.
    v
  [action]        Plain code. Writes the final status, severity, team and an
    |             audit comment into the mock Jira store.
    v
  END

WHEN DOES A HUMAN HAVE TO APPROVE?  (any ONE of these)
    - severity is S1
    - the ticket is security-related
    - the model's confidence is below 0.7
    - it mentions payment, billing, or PII
  Everything else is auto-approved and still logged for audit.

THREE EXAMPLE TICKETS (try them)
    "Complete outage of checkout-service"
        -> generic path -> S1 -> routing: Platform team -> HUMAN needed
           (reason: severity is S1)
    "Unauthorized access to admin panel detected"
        -> security path (o4-mini) -> S1 -> Security team -> HUMAN needed,
           and only a security_approver may sign off
    "Footer copyright year is wrong"
        -> trivial path -> S4, high confidence -> AUTO-APPROVED, no human

WHICH STEPS ARE AI AND WHICH ARE NOT?
    AI (a model call):   triage (3 variants), routing, and embeddings
    Plain code:          intake, guardrail, supervisor, hitl_gate, action
  This split is the main design lesson: use a model only where judgement is
  needed. Safety rules and bookkeeping are ordinary, testable code.


5. THE PAUSE: HUMAN-IN-THE-LOOP AND WHO MAY APPROVE
---------------------------------------------------------------------
HOW THE PAUSE WORKS
  At the gate, the node calls LangGraph's interrupt(). The whole pipeline
  state is saved into Postgres (the "checkpointer"), and the request ends.
  The ticket stays "pending". Hours later -- or after a restart, because it
  is in the database, not in memory -- a person clicks Approve. The API
  resumes that exact saved pipeline with the decision, and the "action"
  node finishes the job.

  (This is why Submit returns with status "pending": the pipeline is not
   finished, it is parked.)

WHO MAY APPROVE  (role-based access control)
  Reviewers are a simple name->role list in settings:
      alice -> approver            may decide ordinary tickets
      bob   -> security_approver   may ALSO decide security/PII tickets
  Rules, enforced by the SERVER before anything changes:
    - an unknown name              -> 403 "not a recognized approver"
    - an "approver" on a security  -> 403 "requires a security_approver"
      or payment/PII ticket
  The decision, the name, and the notes are written to the audit comment
  ("... -> approved by bob"), and the ticket's decided_by field is set.

TEACHING POINT: the check is on the server. The "Approving as" box in the
browser is only a claim. Never trust the browser to enforce a rule.


6. THE KNOWLEDGE: RAG (how the system "remembers")
---------------------------------------------------------------------
THE CORPUS  (in Postgres)
    10 historical tickets (summary, description, resolution, severity)
     5 runbooks (severity policy, security runbook, outage runbook,
                 component-ownership map, human-approval policy)
  Each is converted to an "embedding" (a 1536-number fingerprint of its
  meaning) by the text-embedding-3-small model and stored with pgvector.

HOW A SEARCH WORKS
  1. The ticket text is embedded the same way.
  2. Postgres finds the closest stored fingerprints (cosine similarity).
  3. HYBRID SEARCH blends that with ordinary keyword matching
     (60% meaning, 40% keywords) so an exact name like "checkout-service"
     cannot lose to something that merely "sounds similar".
  4. Results below a similarity floor (0.2) are dropped; the top few go
     into the model's prompt WITH their similarity scores, so the model can
     tell a strong match from a weak analogy.
  Every search is logged (retrieval_log) so you can answer "which past
  tickets grounded this decision?" afterwards.

GROWING THE CORPUS ("Promote to RAG")
  An approved ticket can be added to the corpus with one click. It is
  deliberately manual, never automatic, because unreviewed growth makes
  retrieval worse. Two guards: the same ticket cannot be added twice, and a
  near-identical one (similarity >= 0.95) is refused as a duplicate.

HOW WE KNOW RETRIEVAL IS ANY GOOD
  A small golden set (15 queries) measures "did the right document come
  first?". On it, hybrid search scored 0.93 vs 0.87 for meaning-only.

WHY SEARCH GOES THROUGH "MCP"
  The nodes do not query the database directly. They call tools on an MCP
  server (a separate process): search_similar_tickets, search_runbooks,
  create_ticket, update_ticket, add_comment, get_ticket, list_tickets.
  MCP is a standard way to give an AI system tools. It also means swapping
  the mock Jira for a real one only changes the server, not the pipeline.


7. THE MODELS
---------------------------------------------------------------------
  Three tiers, chosen per path (all hosted in Azure AI Foundry):
      main      DeepSeek-V3.2  generic triage and routing  (fast, ~2 s)
      fast      DeepSeek-V3.2  trivial path
      security  o4-mini        security path  (a "reasoning" model: slower
                               but worth it where a mistake is costly)
  An unset tier falls back to the main model, so nothing breaks if you
  configure fewer.

  Why not one model for everything? Cost and speed. The cheap model handles
  the easy cases; the strong one is spent only where it pays off.

  Two ways to authenticate to Foundry:
      key    an API key in .env  (the default, simple)
      entra  keyless: the caller proves who it IS (az login on a laptop, a
             managed identity in the cloud). No secret to leak.


8. THE SAFETY NET  ("harness engineering")
---------------------------------------------------------------------
Everything that makes the AI trustworthy, none of it AI itself:

  OUTPUT CONTRACTS   The model must return a fixed shape. Wrong shape? Ask
                     once more; still wrong? Raise an error, don't guess.
  GUARDRAILS         Block injection attempts before any model sees them.
  RETRY + BREAKER    Transient failures are retried; after repeated
                     failures a circuit breaker stops calling the broken
                     dependency for 30 s.
  FALLBACKS          If the model is down, keyword rules classify the ticket
                     instead. The pipeline degrades; it does not crash.
  INVARIANTS         "Security tickets always need a human" is enforced in
                     code even if the model says otherwise.
  OBSERVABILITY      agent_runs: one row per node (time, tokens, error,
                     which model, which prompt version).
                     retrieval_log: one row per search.
  RBAC + AUDIT       section 5.
  TESTS + CI         ~20 test suites; free ones run on every push.

THE FOUR LAYERS (a good way to organize a lecture)
  Prompt engineering   the wording given to the model        (prompts/)
  Context engineering  what evidence it is shown             (rag/)
  Harness engineering  the safety net around it              (this section)
  Loop engineering     the control flow of the whole thing   (graph.py)
  (reference/agentic_engineering_layers.xlsx maps each to the code.)


9. HOSTED AGENTS  (the Azure Foundry part)
---------------------------------------------------------------------
WHAT IS A HOSTED AGENT?
  Normally our agents are just Python functions inside the backend. A
  "hosted agent" is the same code packaged as a CONTAINER and run by Azure
  AI Foundry for us. Foundry gives it:
      - its own NAME, shown in the Foundry portal
      - its own ENDPOINT (a URL you can call)
      - its own IDENTITY (who it is, for permissions) -- so no API keys
      - VERSIONS (each deploy is a new immutable version)
      - SESSIONS (see below), billed only while running

WHAT WE DEPLOYED  (4 of 5 agents)
      ops-routing           routing           DeepSeek-V3.2
      ops-triage-generic    triage-generic    DeepSeek-V3.2
      ops-triage-security   triage-security   o4-mini
      ops-triage-trivial    triage-trivial    DeepSeek-V3.2
  All four are ONE container image. An environment variable (OPS_ROLE)
  tells the container which agent to be. NOT deployed: the orchestrator
  (the pipeline itself) -- it needs a database the cloud agent can reach.

THE KEY DESIGN IDEA
  A specialist is a PURE MODEL STEP: it gets a request that already contains
  the evidence, asks the model, and returns the answer. It needs no
  database, no MCP, and no secrets. So the orchestrator does the retrieval
  and ships the evidence along:

      orchestrator --{ticket + similar tickets + policy}--> specialist
      orchestrator <--{severity, confidence, reasoning,
                       model, prompt version, tokens}------ specialist

  Because specialists are stateless and database-free, they are easy to
  host, easy to test, and easy to replace.

TWO WAYS TO RUN THE SAME PIPELINE
  Default (nothing to configure):

      [ orchestrator ]--calls-->[ model ]       everything in one process

  Hosted (set SPECIALIST_URLS + SPECIALIST_AUTH=entra):

      [ orchestrator ]--HTTPS-->[ ops-triage-generic  ]--> [ model ]
        (on a laptop,   |------>[ ops-triage-security ]--> [ o4-mini ]
         has Postgres)  |------>[ ops-triage-trivial  ]--> [ model ]
                        '------>[ ops-routing         ]--> [ model ]

  If a hosted specialist is down, slow, or returns something invalid, the
  orchestrator falls back to calling the model itself. A dead specialist
  degrades a ticket; it never fails one.

HOW A HOSTED CALL WORKS  (and why "sessions" matter)
  Each SESSION is its own container.

      request WITHOUT a session id  -->  Foundry starts a NEW container
                                         (a COLD START, ~6-8 seconds) and
                                         returns a session id
      request WITH that session id  -->  reuses the warm container (~1-4 s)

  Foundry shuts a container down after ~15 idle minutes. So the first ticket
  after a quiet period is slower. Our client remembers the session id per
  agent to stay warm.

HOW IT WAS DEPLOYED  (the workflow, in order)

   code --> IMAGE --> REGISTRY --> AGENT VERSION --> IDENTITY + ROLE --> CALL
     |        |          |              |                   |             |
   hosted/  Dockerfile  Azure        Foundry           the agent gets   smoke
   server   .hosted     Container    registers it,     permission to    test:
   .py      built in    Registry     starts it,        call the models  HTTP 200
            Azure       (private     gives it an       (one least-
            (amd64)     image        identity          privilege role)
                        store)

  Tooling:  python -m hosted.deploy plan --agent routing   (read-only)
            python -m hosted.deploy apply --agent routing --step <name>
  `plan` prints every step with its exact command, cost and undo. `apply`
  runs ONE step and does nothing unless you type "y". There is deliberately
  no "do everything" button -- every step spends money or changes
  permissions, so each is a conscious decision.

WHAT WENT WRONG ALONG THE WAY  (excellent teaching material)
  Real systems fail in specific, instructive ways. Each of these was found
  live, fixed, and given a regression test:
    1. Azure refused to create the registry: the subscription had never
       been registered for that service. (One-time opt-in.)
    2. The registry's permission mode and the role we granted did not match
       the documentation. (Read the docs' fine print.)
    3. The build failed on a RELATIVE file path that only worked from one
       directory.
    4. Foundry rejected our env var AGENT_ROLE: it reserves every name
       starting with AGENT_ or FOUNDRY_. Microsoft's docs only mention
       FOUNDRY_. (Docs can be incomplete -- trust the system's error.)
    5. The deploy tool HUNG for 7 minutes. A network stall in one helper
       command. Lesson: put a timeout on everything that can block.
    6. The deployed agent answered 500. The container's log showed 401: the
       agent's identity had no permission to call the model -- even though
       the docs said it would by default.
    7. Our first latency test was misleadingly slow: we never passed the
       session id, so every call cold-started a container.
    8. An intermittent 401 from the model side (3 of 64 calls) that we could
       not explain. Honest engineering includes "we don't know yet".

WHAT HOSTING COSTS YOU  (measured, laptop -> Foundry)
      one agent call:   in-process ~1.2-2.6 s   hosted warm ~1.6-3.8 s
                        hosted COLD ~6.7-8.5 s
      a whole ticket:   in-process ~8 s   hosted warm ~9.4 s (+~16%)
                        first ticket after idle ~16 s (cold starts)
  Hosting does not make anything faster. What it buys is architecture:
  separately named agents, each with its own identity and permissions,
  visible and manageable in Foundry.


10. A CLASSROOM DEMO SCRIPT  (about 20 minutes)
---------------------------------------------------------------------
  Setup:   docker compose up -d      then open http://localhost:5173

  A. The auto-approved ticket (3 min)
     Submit tab -> "Footer typo" -> Submit.
     Show: it finishes in seconds. Ticket log: status auto_approved,
     decided_by empty. Point: low risk = no human.

  B. The paused ticket (5 min)
     Submit -> "Outage". Open Pending approvals. Walk the card: severity,
     the amber reason, then expand "Agent reasoning & evidence" and show the
     similar past tickets with their percentages. Point: the human sees the
     evidence, not just a verdict.

  C. Who may approve (4 min)
     Type "mallory" and Approve  -> 403, unknown approver.
     Submit "PII exposure". As "alice" try to approve -> 403, needs a
     security_approver. Switch to "bob" -> works. Point: rules live in the
     server. Show Ticket log: decided_by bob.

  D. Learning from the result (2 min)
     Ticket log -> "Promote to RAG" on an approved ticket. Click it twice:
     the second is refused (already promoted).

  E. Under the hood (3 min)
     Open Adminer (http://localhost:8080) and look at agent_runs: one row
     per node, with duration, model, and prompt version. Then retrieval_log.

  F. Hosted agents (3 min, no live Azure needed)
     Show the Foundry portal's agent list (four ops-* agents), then
     docs/hosted-agents-deployment.md -- walk the "what went wrong" table.

EXERCISES FOR STUDENTS
    1. Add a fifth guardrail phrase and write a test that proves it blocks.
    2. Change the HITL confidence threshold from 0.7 to 0.9. Which of the
       sample tickets now need a human? Why?
    3. Edit a prompt in agents/prompts/ and bump its PROMPT_VERSION. Find the
       new version in agent_runs.
    4. Turn RAG_HYBRID_SEARCH_ENABLED off and run the evaluation. What
       changes, and why?
    5. Kill a hosted specialist (or set a wrong URL). Submit a ticket. What
       happens, and why does the ticket still complete?


11. GLOSSARY
---------------------------------------------------------------------
  Agent         a step that uses a model to make a judgement
  Node          one box in the LangGraph workflow
  State machine a workflow defined as nodes + the allowed transitions
  HITL          human-in-the-loop: the pipeline pauses for a person
  Checkpointer  saves the paused pipeline to the database so it can resume
  RAG           retrieval-augmented generation: show the model relevant
                evidence from your own data before it answers
  Embedding     a list of numbers that captures the meaning of text
  pgvector      a Postgres extension that searches embeddings
  Hybrid search meaning-based + keyword-based search combined
  MCP           Model Context Protocol: a standard way to give AI tools
  Contract      the exact output shape a model must produce
  Circuit       stops calling a failing service for a while instead of
  breaker       hammering it
  RBAC          role-based access control: what a role is allowed to do
  Hosted agent  your container, run and managed by Azure Foundry
  Session       one running container instance of a hosted agent
  Cold start    the delay when a new session's container must start
  Identity      the "who am I" an agent presents instead of a password
  Image         the packaged code a container is started from
  Registry      a private store for container images


12. HONEST LIMITS
---------------------------------------------------------------------
  - The "Jira" is a mock. The 10-ticket corpus is tiny, so retrieval looks
    better than it would on real data; recall@1 had a known miss.
  - The model's severity answers vary between runs on vague tickets. The
    confidence threshold sends the uncertain ones to a human -- that is the
    safety net doing its job, not a bug.
  - Latency numbers are from one laptop with small samples; differences
    under ~1 s are noise.
  - Hosted agents: the billing rate was never looked up; teardown commands
    are untested; an intermittent 401 is unexplained; Foundry shows the
    endpoint protocol as "responses" although we requested Invocations
    (calls still work).
  - Hosted-agent calls in the demo came from an orchestrator on a laptop.
    The orchestrator itself is not hosted yet.
  - Token counts in agent_runs undercount when a provider omits usage data
    (DeepSeek on Azure does ~60% of the time). Do not build cost reports on
    them without accounting for that.


13. WHERE TO LOOK NEXT
---------------------------------------------------------------------
  reference/agentic-teaching-notes.md        pattern-by-pattern walkthrough
  reference/agentic_engineering_layers.xlsx  prompt/context/harness/loop map
  reference/*.drawio                         flow diagrams
  docs/hosted-agents-deployment.md           the full deployment record
  docs/submitting-tickets.md                 sample tickets per severity
  backend/agents/docs/AGENTS_OVERVIEW.md     module-by-module reference
