DEPLOYMENT PROCESS — WHAT WE DID, STEP BY STEP, AND WHY
=====================================================================
Ops Triage: from a laptop project to agents hosted in Azure AI Foundry
and a public, passcode-protected demo.

Written 2026-10-29 from what actually happened, including the failures.
Plain text on purpose: it reads fine in Notepad. Nothing secret is in
this file (no subscription IDs, keys, passcodes or addresses).


0. THE WHOLE STORY IN ONE PARAGRAPH
---------------------------------------------------------------------
We started with a multi-agent ticket-triage app that ran on one laptop and
called OpenAI. We moved its models to Azure AI Foundry (keyless), split its
AI work into four small specialist agents, and deployed those four as
HOSTED AGENTS that Foundry runs for us. We tried to host the fifth part (the
orchestrator) too, hit an Azure capacity wall, and decided on purpose to
keep it local. Then we made the whole app safe to put on the internet
(passcode, ticket limits), packaged it as one container, and built a
tool that brings it up on Azure Container Apps in 11 gated steps and takes it
all down with one command. Along the way, real deployments failed in
instructive ways, and every failure got a fix and a regression test.


1. THE THREE DEPLOYMENTS (THE MAP)
---------------------------------------------------------------------

   DEPLOYMENT 1: HOSTED SPECIALIST AGENTS           (done, verified)
        four small agents, run by Foundry

   DEPLOYMENT 2: HOSTED ORCHESTRATOR                (attempted, stopped
        needs a database in the cloud                 on purpose)

   DEPLOYMENT 3: PUBLIC DEMO                        (done; hosted-agent
        the whole app on a URL, then deleted          calls still to be
                                                      confirmed)

Where each lives:

   Azure resource group "proj-foundryapp"   (permanent)
      Foundry project + models
      4 hosted agents
      container registry (images)

   Azure resource group "ops-triage-demo"   (temporary: made and deleted
      app container + database container     by the demo tool)
      identity, environment, log workspace


2. DEPLOYMENT 1 — THE FOUR HOSTED AGENTS
---------------------------------------------------------------------

2.1 Why split the app first
  Before: one process did everything (guardrail, retrieval, 3 triage paths,
  routing, approval, writing the ticket).
  After:  four agents are PURE MODEL STEPS. The orchestrator does the
  retrieval and ships the evidence inside the request, so a specialist
  needs no database, no tools and no secrets. That is what makes them easy
  to host.

      orchestrator --{ticket + similar tickets + policy}--> specialist
      orchestrator <--{severity, confidence, reasoning,
                       model, prompt version, tokens}------ specialist

  The four agents (one container image, an env var OPS_ROLE picks which):
      ops-triage-generic    DeepSeek-V3.2
      ops-triage-security   o4-mini  (reasoning model, for risky tickets)
      ops-triage-trivial    DeepSeek-V3.2
      ops-routing           DeepSeek-V3.2

2.2 The deployment flow

   +-------+   +-------+   +----------+   +---------+   +----------+
   | code  |-->| image |-->| registry |-->|  agent  |-->| identity |
   |       |   |       |   |  (ACR)   |   | version |   | + roles  |
   +-------+   +-------+   +----------+   +---------+   +----------+
     hosted/    Dockerfile   built IN       Foundry       the agent
     server.py  .hosted      Azure (the     creates       gets ONE
                (linux/      Mac is ARM,    the agent,    least-
                amd64)       Foundry needs  starts it,    privilege
                             amd64)         gives it an   role to call
                                            identity      the models
                                                            |
                                                            v
                                                       smoke test:
                                                       HTTP 200

2.3 The tool: plan first, then ONE step at a time
  python -m hosted.deploy plan --agent routing        (reads only)
  python -m hosted.deploy apply --agent routing --step <name>   (y/N)

  Design rule: every step spends money or changes permissions, so there is
  deliberately NO "do everything" button. Each step prints its exact
  command, what it creates, what it costs and how to undo it, and does
  nothing unless you type y.

  The 11 steps: grant_deployer_role, register_acr_provider, create_acr,
  verify_acr_mode, grant_registry_writer, grant_project_pull, build_image,
  create_agent_version, wait_active, grant_agent_model_access, smoke_test.

2.4 Roles used (the "who can do what" table)
  You (deployer)           Foundry Project Manager       create agents
  You (deployer)           Container Registry Repo.      push images
                           Writer
  Foundry project          Container Registry Repo.      pull images
  identity                 Reader
  Each agent's own         Cognitive Services OpenAI     call the models
  identity                 User                          (inference only)
  The registry runs in "rbac-abac" mode, where the old AcrPull role is NOT
  honoured; the Repository Reader/Writer roles are the ones that count.

2.5 What went wrong (every one was found live and fixed)
   1  Registry creation failed: the subscription had never been opted in
      to that Azure service. Fix: a register-provider step.
   2  Registry permission mode did not match the role in the guide.
      Fix: create it in rbac-abac mode and verify the mode in a step.
   3  Image build failed on a RELATIVE path that only worked from one
      folder. Fix: absolute path from the tool's own location.
   4  Foundry rejected env var AGENT_ROLE: it reserves every name that
      starts with AGENT_ or FOUNDRY_ (the docs only mention FOUNDRY_).
      Fix: renamed to OPS_ROLE + a test that forbids reserved prefixes.
   5  The deploy tool HUNG for 7 minutes on one stalled helper command.
      Fix: a 90-second timeout on every lookup.
   6  The agent answered 500; its log showed 401: its identity had no
      permission to call the models, although the docs said it would by
      default. Fix: the grant_agent_model_access step.
   7  Our first latency test was misleading: we never passed the session
      id, so every call cold-started a container. Fix: session reuse.
   8  An intermittent 401 (3 of 64 calls, all on ops-routing after ~20 idle
      minutes). CAUSE NOT FOUND. Written down honestly as open.

2.6 What it costs to host, measured
  one agent call:  in-process ~1.2-2.6 s | hosted warm ~1.6-3.8 s |
                   hosted cold start ~6.7-8.5 s
  whole ticket:    in-process ~8 s | hosted warm ~9.4 s (+~16%) |
                   first ticket after idle ~16 s
  Hosting does not make anything faster. What it buys is architecture:
  separately named agents, each with its own identity and permissions,
  visible and manageable in Foundry.

2.7 Sessions (why the first ticket is slow)
  Each SESSION is its own container. A call without a session id starts a
  new container (a COLD START). A call WITH the id from an earlier reply
  reuses the warm one. Foundry releases a container after ~15 idle minutes.


3. DEPLOYMENT 2 — THE ORCHESTRATOR (ATTEMPTED, THEN STOPPED ON PURPOSE)
---------------------------------------------------------------------
Goal: host the fifth part too, so all five show in Foundry.
Problem: the orchestrator is not a model step. It owns tickets, the RAG
corpus and the paused (human-approval) pipelines, so it needs a Postgres
database a cloud agent can reach.

What we built (kept in the repo, tested):
   - a 15-step plan: Azure PostgreSQL with PASSWORD LOGIN DISABLED
     (Entra identity only), pgvector, firewall, migrations, a database role
     for the agent that can read/write rows but not create tables
   - keyless database login in the code (DATABASE_AUTH_MODE=entra)

What happened live:
   provider registered ........................ OK (free)
   create PostgreSQL (B1ms, swedencentral) .... FAILED
       RegionalAllocationFailed: no capacity for that size in the region.
       Retried pinned to availability zones 1, 2 and 3: same error, so it
       is regional, not per-zone.
   nothing else was created.

The decision (and the reasoning to give students):
   Keep the orchestrator local. It is a workflow over a database, not a
   model step. Hosting it would add a 24/7 database (~$19/month), a
   gateway, and four unverified items - without making anything faster or
   smarter. The thinking is in the cloud agents; the workflow and its data
   stay together.


4. DEPLOYMENT 3 — THE PUBLIC DEMO
---------------------------------------------------------------------

4.1 The goal
  A URL you can open anywhere, show live, and then DELETE. Short and
  supervised - not a permanent service.

4.2 Making the app safe to put on the internet (before any Azure step)
  The app had no login: "Approving as" was just a typed name. On the
  internet anyone could type "bob" and approve a security ticket, or submit
  tickets all day at your expense. So:

   passcode gate      DEMO_PASSCODE: every /api call except health and
                      "do I need a passcode?" must carry it. The page
                      itself is public but contains no data.
   cost caps          TICKET_SUBMISSION_CAP (30 in total) and
                      TICKET_RATE_LIMIT_PER_MINUTE (5 per client). The total
                      cap is the real guard: a per-client limit can be
                      dodged by changing address.
   scheduler off      the ticket generator would spend model calls alone.
   one URL            the API serves the built React page itself (same
                      origin, no CORS to configure).
   one image          Dockerfile.demo builds React + API into one container.
   throwaway DB       seed_if_empty loads the corpus only when it is empty.
  All verified locally in a real browser: wrong passcode refused, right one
  accepted, a ticket auto-approved, the 4th ticket refused at cap 3.

4.3 The picture of what runs

   PEOPLE (any browser)
        |  https + passcode
        v
   +---------------------------------------------------------------+
   | AZURE CONTAINER APPS            resource group ops-triage-demo |
   |                                                                |
   |   ops-demo-app   (1 container, always on, 1 vCPU / 2 GiB)      |
   |     React page + FastAPI + LangGraph pipeline                  |
   |        |                                                       |
   |        | internal TCP only, by short name ops-demo-db:5432     |
   |        v                                                       |
   |   ops-demo-db    (Postgres 16 + pgvector, 0.5 vCPU / 1 GiB,    |
   |                   no disk, NO address on the internet)         |
   +--------|--------------------------------|---------------------+
            |  keyless: the demo's managed identity
            v                                v
   +-----------------------------------------------------------+
   | FOUNDRY PROJECT (resource group proj-foundryapp, permanent)|
   |   models:  DeepSeek-V3.2, o4-mini, text-embedding-3-small  |
   |   hosted agents: ops-triage-generic / -security / -trivial |
   |                  ops-routing                               |
   |   container registry: holds the two images the demo uses   |
   +-----------------------------------------------------------+

4.4 The bring-up tool: python -m hosted.demo
   plan | apply --step <name> | status | down
   Same rules as before: plan only reads; apply runs ONE step behind y/N;
   everything the demo creates goes in ONE resource group, so "down" is one
   delete and nothing keeps billing.

4.5 The 11 steps, in order, with what each one does

   1  register_app_provider   opt the subscription in to Container Apps
   2  create_resource_group   the demo's own group, tagged
                              purpose=temporary-demo
   3  create_identity         a managed identity (no password anywhere)
   4  grant_registry_pull     identity may PULL images (Repository Reader)
   5  grant_model_access      identity may call the models (OpenAI User)
   6  build_image             build the app image in the registry
   7  import_postgres_image   copy the pgvector image into OUR registry
                              (Docker Hub throttles cloud addresses)
   8  create_environment      the shared network + log area
   9  create_database_app     throwaway Postgres, internal only
  10  create_web_app          the app, public HTTPS URL, passcode + caps
  11  smoke_test              page loads, API refuses calls without passcode

   Flow of dependencies:

   provider -> group -> identity -> roles -> images -> environment
                                                  \
                                      database -> app -> smoke test

4.6 Secrets: who knows what
   database password   generated at run time inside the tool, never
                       printed, kept ONLY in Azure's secret store; the app
                       step reads it back from Azure rather than a file
   demo passcode       YOU choose it and put DEMO_DEPLOY_PASSCODE in your
                       own gitignored .env; the tool never generates,
                       prints or logs it; the displayed command shows a
                       placeholder, and a test proves the executed command
                       differs from the displayed one only in the secrets
   model access        no key at all: the managed identity

4.7 What went wrong during the real bring-up (all fixed, all tested)
   a  Step 6 failed: "Unable to find Dockerfile.demo". az looks for the
      file in the CURRENT directory, whatever its help says. Fix: run the
      build from the repo root.
   b  SECRETS FINDING on the retry: az packs the build upload with its own
      matcher, where **/.env does NOT match a .env at the top level. The
      real root .env was uploaded to the registry's temporary build
      storage. It was NOT in the image (the Dockerfile copies only
      frontend/ and backend/, and deletes any .env). Found by running az's
      own packing code locally. Fix: list .env and .env.* explicitly +
      a test. (Earlier agent builds were not affected: their folder had
      no .env.) Retention of the uploaded archive is unknown, so rotating
      the keys in .env is the cautious option; that is the owner's call.
   c  The first app start crashed: "connection timeout expired" reaching
      the database. We had used the long internal name
      <app>.internal.<domain>. Fix: the short name <app>:<exposed port>.
   d  The passcode was typed into .env.example (the committed template)
      instead of .env. Caught before any commit; moved without printing it.
   e  After a few idle minutes the live site returned
      500 Internal Server Error. Cause: the pipeline's saved-state layer
      (the LangGraph checkpointer) held ONE database connection forever;
      Azure's internal proxy closed it when idle, and nothing could
      replace it. Fix: a health-checked connection POOL plus TCP
      keepalives. Proven: a test kills the connections on the Postgres
      side - the old code fails exactly like the live demo, the new code
      survives. Rolled out with update_web_app_image: the site kept
      answering during the swap.

4.8 Using the hosted agents from the demo (the "show them" requirement)
   By default the demo app calls the Foundry MODELS directly. To make it call
   the four hosted agents:
     1. grant the demo identity "Foundry User" on the project
        (BROAD: Microsoft.CognitiveServices/*; no narrower built-in role
        names an invoke action - tighten later)
     2. enable_hosted_specialists: sets SPECIALIST_AUTH and SPECIALIST_URLS
        on the running app (a new revision; the old one stays up until the
        new one is healthy)
   If an agent refuses or fails, the app logs a "falling back" warning and
   calls the model itself - so a failure never fails a ticket.
   STATUS: configured and the settings were read back from Azure. Whether
   the agents actually answered live is confirmed only when the log shows
   tickets processed with NO fallback warnings and the agents show new
   sessions in the Foundry portal.

4.9 Swapping a revision without downtime
   az containerapp update creates a NEW revision. Azure moves traffic only
   once it is healthy; if it fails to start, the old one keeps serving.
   Used for: enabling the hosted agents, and rolling out the checkpointer
   fix. The site returned HTTP 200 on every check during both.


5. HOW ONE TICKET FLOWS THROUGH THE LIVE SYSTEM
---------------------------------------------------------------------
   1  browser sends the ticket with the passcode header
   2  the passcode gate checks it (401 if wrong) and the limiter counts it
   3  guardrail (plain code) blocks prompt-injection phrases
   4  supervisor (plain code) picks path: security / trivial / generic
   5  retrieval: embeds the ticket (embedding model, keyless) and searches
      pgvector in ops-demo-db for similar tickets and runbooks
   6  triage: sends ticket + evidence to the hosted agent for that path
      (or the model directly, if the hosted call fails) - first call after
      idle pays a cold start
   7  routing: same, to ops-routing
   8  hitl_gate (plain code): S1, security, low confidence or PII/payment
      => PAUSE, saved in Postgres; otherwise auto-approve
   9  a person approves as alice (ordinary) or bob (security/PII);
      the server checks the role, writes the audit comment
  10  the ticket is written to the (mock) Jira tables


6. BRINGING IT UP AND DOWN (THE CHEAT SHEET)
---------------------------------------------------------------------
   Before:  commit your work (the image tag is the git commit);
            put DEMO_DEPLOY_PASSCODE=<your choice> in .env
            cd backend && python -m hosted.demo plan
   Up:      apply --step register_app_provider ... smoke_test, one at a
            time (add --tag <commit> to pin an image already built)
   Check:   python -m hosted.demo status
   Logs:    az containerapp logs show --name ops-demo-app
              --resource-group ops-triage-demo --tail 80
   Down:    python -m hosted.demo down
            lists what it will delete, asks once, removes the identity's
            role assignments first, then the group; REFUSES any group not
            tagged purpose=temporary-demo.
   Left over after down: the two images in the registry (cents a month).


7. WHAT IT COSTS (list prices, swedencentral; check your billing)
---------------------------------------------------------------------
   hosted agents ........... billed only while a session runs
   container registry ...... ~$5/month (permanent), plus cents for images
   demo, both containers ... ~$0.05/hour idle, up to ~$0.16/hour fully
                             active  =>  roughly $1-4 for a working day
   log workspace ........... a few MB, effectively nothing
   model calls ............. per ticket; the 30-ticket cap bounds it
   the Postgres we did NOT build: ~$19/month, 24/7 - this is why the
   demo uses a throwaway container instead.


8. SAFETY DESIGN (WHAT TO TELL STUDENTS)
---------------------------------------------------------------------
   - Every costly or permission-changing step is gated: shown first,
     typed y, one at a time. No run-everything mode.
   - Least privilege: each identity holds only the roles it needs.
   - No secret in an image, in the plan, in a log or in the repo.
   - Everything temporary lives in one tagged group that "down" removes.
   - Failures are expected: the tool stops at the first one and says what
     did NOT happen.
   Honest limits: the passcode is shared (not user accounts) and 8
   characters is the minimum; wrong guesses are not locked out; anyone with
   the passcode can type "bob" and approve a security ticket; the ticket
   counter is in memory (one process), and a database restart loses pending
   approvals.


9. LESSONS WORTH TEACHING
---------------------------------------------------------------------
   1  "Works on my laptop" hides network behaviour: idle connections,
      DNS names, firewalls. The 500 and the timeout only existed in Azure.
   2  Read the system's error, not just the docs: AGENT_ was reserved,
      --file was resolved against the working directory.
   3  Test with the real tool: we only found the .env upload by running
      az's own packer; our Docker test used a different matcher.
   4  A failing step should cost nothing: gated, one at a time, and it
      stops. Every failure here cost at most cents.
   5  Fix the bug AND write the test that reproduces it (we killed the
      connection on purpose to prove the 500 and its cure).
   6  Know when to stop: the orchestrator host was a legitimate "no",
      recorded with reasons, not a failure.
   7  Secrets: generate them where they are used, never print them, keep
      them in the platform's secret store.


10. VERIFIED VS NOT YET
---------------------------------------------------------------------
   Verified live:  four hosted agents answer (HTTP 200); the demo app is
   up behind its passcode; migrations and corpus seeding ran with the
   keyless identity; the database is reachable internally; the checkpointer
   fix swapped in with the site up throughout; settings read back from
   Azure.
   NOT yet confirmed: that the hosted agents answered tickets from the demo
   (watch for no "falling back" warnings); the cause of the intermittent
   401 on hosted calls; hosted-agent billing rates; the teardown (`down`)
   against real resources; the retention of the build archive that
   contained .env.


11. WHERE THE CODE AND DOCS ARE
---------------------------------------------------------------------
   backend/hosted/deploy.py     deploy the four agents (plan/apply)
   backend/hosted/demo.py       bring the public demo up / down
   backend/hosted/server.py     the agent entry point (OPS_ROLE)
   backend/api/demo_guard.py    passcode gate + ticket limiter
   backend/agents/runtime.py    checkpointer pool (the 500 fix)
   backend/db/entra.py          keyless database login
   Dockerfile.demo, .dockerignore   the one-image demo build
   docs/hosted-agents-deployment.md   record of Deployments 1 and 2
   docs/public-demo.md          runbook for Deployment 3
   reference/project-workflow-teaching-notes.md   the app itself
