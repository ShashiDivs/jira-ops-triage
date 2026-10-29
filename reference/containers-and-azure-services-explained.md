CONTAINERS AND AZURE SERVICES — FROM RESOURCE GROUP TO DEPLOYMENT
=====================================================================
How Ops Triage is containerised, which Azure services it uses, and what
each one is for. Written 2026-10-29 from the real Azure account (read with
`az` commands) and the real Dockerfiles. Plain text: reads in Notepad.

Placeholders used on purpose (this repo is public): <acr> is the registry
name ("opstriage" + six characters derived from the subscription ID);
no subscription ID, tenant, user name, key, passcode or public address
appears in this file.


0. THE ANSWER IN FIVE LINES
---------------------------------------------------------------------
   Containers   4 Dockerfiles -> 3 image families (agent, demo app,
                database) + the local docker compose stack (4 services)
   Azure        6 services we actually run (Foundry, Container Registry,
                Container Apps, Managed Identity, Log Analytics, and
                Entra/RBAC underneath all of them)
   Resource groups   2: "proj-foundryapp" (permanent) and
                "ops-triage-demo" (temporary, deleted with one command)
   Resources we created   1 registry, 4 hosted agents, and 5 demo
                resources; plus 10 role assignments and 3 provider opt-ins
   Not used (on purpose)   Azure PostgreSQL (no capacity), App Insights,
                private networks, Key Vault


1. CONTAINERISATION: WHAT IS A CONTAINER HERE, AND WHY
---------------------------------------------------------------------
A container is the app plus everything it needs (Python, libraries, our
code) packed into one image that runs the same on a laptop, in Docker, and
in Azure. We use containers for three different jobs:

   JOB 1  run the whole app locally          docker compose
   JOB 2  run the four AI agents in Foundry  Dockerfile.hosted
   JOB 3  run the public demo on Azure       Dockerfile.demo

1.1 The four Dockerfiles

   backend/Dockerfile          LOCAL development image
       python:3.12-slim; installs requirements.txt (includes DSPy);
       copies the code; runs the API on port 8000.

   frontend/Dockerfile         LOCAL development image
       node:20-slim; npm install; runs the Vite DEV server on 5173
       (hot reload; not for the internet).

   backend/Dockerfile.hosted   the AGENT image (one image, five roles)
       python:3.12-slim; installs ONLY requirements-base.txt (no DSPy);
       precompiles Python bytecode so a cold start is faster; serves the
       Foundry "Invocations" protocol on port 8088. An environment variable
       OPS_ROLE decides which agent a container is (orchestrator,
       triage-generic, triage-security, triage-trivial, routing). The role is
       NOT baked into the image; it is set per agent version.

   Dockerfile.demo             the DEMO image (React + API in ONE container)
       Multi-stage build (see 1.2). Final image: python:3.12-slim + the
       built React files in /app/static. On start it runs the database
       migrations, loads the RAG corpus if the database is empty, then
       serves everything on port 8000.

1.2 Multi-stage build, in plain words (Dockerfile.demo)
   Stage 1 "web":  a Node image builds the React app into plain files.
   Stage 2 final: a Python image copies ONLY those built files across.
   Node and its hundreds of packages are thrown away, so the final image
   has no build tools and is smaller and safer.

       [ node:20 ]  npm run build  -->  dist/ (HTML, JS, CSS)
                                           |  copied across
       [ python:3.12 ]  pip install  +  backend code  +  dist/  -->  IMAGE

1.3 The local stack (docker-compose.yml): 4 services
   postgres   pgvector/pgvector:pg16, port 5432, data kept in a named volume
   backend    built from backend/, runs "alembic upgrade head" then uvicorn
   frontend   built from frontend/, the Vite dev server on 5173
   adminer    a small web page to look inside the database, port 8080
   Start it all with:  docker compose up -d

1.4 The three image families that live in Azure
   Repository          Built from          Tag             Used by
   ------------------  ------------------  --------------  ---------------
   ops-triage-agent    Dockerfile.hosted   git commit      the 4 hosted
                                                           agents
   ops-triage-demo     Dockerfile.demo     demo-<commit>   the demo app
   pgvector            copied from Docker  pg16            the demo database
                       Hub (az acr import)
   Tags are git commits, so an image is always traceable to the code that
   built it; the demo tool refuses to build from a dirty tree without
   labelling it "-dirty".

1.5 Why images are built IN Azure, not on the laptop
   The Mac is ARM; Foundry and Container Apps run linux/amd64. Building on
   the Mac would produce the wrong architecture. `az acr build` uploads the
   source and builds remotely on amd64 (about 1.5 minutes, cents).

1.6 What must NOT be in an image: secrets
   - .dockerignore keeps .env out of every build.
   - LESSON: az packs the upload with its own matcher; "**/.env" did not
     match the top-level .env, so the real .env was UPLOADED to the
     registry's temporary build storage once (not into the image).
     Fixed by listing ".env" and ".env.*" explicitly, plus a test, plus a
     "rm -f /app/.env" line in the demo Dockerfile as a second guard.
   - Settings reach a running container as ENVIRONMENT VARIABLES or
     platform SECRETS, never inside the image.

1.7 How a container learns its settings at run time
   agent container    OPS_ROLE, AZURE_AUTH_MODE=entra, the model
                      deployment names. No secret at all.
   demo app           DATABASE_URL and DEMO_PASSCODE (as Container Apps
                      SECRETS), ticket caps, model names, the identity's
                      client id, optional SPECIALIST_* addresses.
   demo database      POSTGRES_PASSWORD (as a secret), user and db name.


2. AZURE: THE BIG PICTURE
---------------------------------------------------------------------

   SUBSCRIPTION
     |
     |-- opted in to resource providers (one-time, free):
     |     Microsoft.ContainerRegistry, Microsoft.App,
     |     Microsoft.DBforPostgreSQL
     |
     |-- RESOURCE GROUP  proj-foundryapp         (permanent, swedencentral)
     |     |-- Foundry account  "proj-app"       (Cognitive Services)
     |     |     |-- Foundry project "proj-app"
     |     |     |     '-- 4 HOSTED AGENTS (ops-triage-generic / -security /
     |     |     |         -trivial, ops-routing)
     |     |     '-- 3 model deployments: DeepSeek-V3.2, o4-mini,
     |     |           text-embedding-3-small
     |     '-- Container Registry <acr>  (Basic, ABAC mode)
     |           repos: ops-triage-agent, ops-triage-demo, pgvector
     |
     '-- RESOURCE GROUP  ops-triage-demo         (temporary, tagged
           |                                      purpose=temporary-demo)
           |-- Managed identity   ops-demo-identity
           |-- Container Apps environment   ops-demo-env
           |     |-- Container App  ops-demo-db   (Postgres + pgvector)
           |     '-- Container App  ops-demo-app  (React + API, public URL)
           '-- Log Analytics workspace (container logs)

   Note: proj-foundryapp also holds a SECOND Foundry resource with its own
   project, an Application Insights, a log workspace and an alert action
   group. They were created earlier by Azure's Foundry setup, not by this
   project, and nothing here uses them. Our project (proj-app) has no
   Application Insights connected.


3. THE AZURE SERVICES, ONE BY ONE
---------------------------------------------------------------------
   #  Service                What it is, in one line     Our resources
   -- ---------------------- --------------------------- -----------------
   1  Resource groups        folders that hold related   proj-foundryapp,
                             resources and can be        ops-triage-demo
                             deleted together
   2  Azure AI Foundry       the AI platform: hosts      account + project
      (models)               model deployments you call  "proj-app"; 3
                             over HTTPS                  deployments (below)
   3  Foundry Agent          runs YOUR container as a    4 hosted agents
      Service (hosted        named agent with its own
      agents)                identity, endpoint and
                             versions; billed while a
                             session runs
   4  Azure Container        a private store of images   <acr>, Basic tier,
      Registry (ACR)         (+ builds them remotely)    ABAC permissions
   5  Azure Container Apps   runs containers without     1 environment +
                             managing servers; public    2 apps (temporary)
                             HTTPS or internal-only
   6  Managed Identity       an Azure-held identity a    ops-demo-identity
                             container uses INSTEAD of
                             a password (the 4 agents
                             get one automatically)
   7  Log Analytics          collects container logs     1 workspace
                             (queried with `az monitor   (temporary)
                             log-analytics query`)
   8  Microsoft Entra ID +   who you are and who may do  every sign-in and
      Azure RBAC             what: tokens, roles, role   10 role
                             assignments                 assignments (sec. 5)

   The three model deployments (all "GlobalStandard", pay per token):
      DeepSeek-V3.2           main + fast model (triage, routing)
      o4-mini                 reasoning model for the security path
      text-embedding-3-small  turns text into vectors for RAG search


4. FROM RESOURCE GROUP TO DEPLOYMENT, IN ORDER
---------------------------------------------------------------------
Each arrow is a step you approve; every Azure step prints its command,
cost and undo first, and runs only after you type y.

4.1 Foundation (set up before the deployment work)
   subscription -> resource group "proj-foundryapp" -> Foundry account +
   project "proj-app" -> 3 model deployments

4.2 Deployment 1: the four hosted agents  (python -m hosted.deploy)

   register ACR provider
        |
   create the registry (ABAC mode) --> verify the mode
        |
   grant roles:  you = Repository Writer;  Foundry project = Repository
        |        Reader (so Foundry can pull);  you = Project Manager
   build the agent image in the registry (amd64, tagged with the commit)
        |
   create the agent version  (Foundry starts the container, creates the
        |                     agent's own identity)
   wait until active
        |
   grant that identity "Cognitive Services OpenAI User" on the account
        |
   smoke test: HTTP 200
   (repeat the last four steps for each of the four agents)

4.3 Deployment 2: orchestrator in the cloud  (attempted, stopped)
   Needed a managed Postgres. Creating it failed (no capacity in the
   region, all three zones). Decision: keep the orchestrator local.
   Created in Azure: nothing except a free provider opt-in.

4.4 Deployment 3: the public demo  (python -m hosted.demo)

   opt in to Container Apps
        |
   create resource group "ops-triage-demo"  (tagged, so `down` can find it)
        |
   create the managed identity
        |
   give it three roles:
        |    - Repository Reader on the registry   (pull images)
        |    - OpenAI User on the Foundry account  (call the models)
        |    - Foundry User on the project         (call the hosted agents;
        |                                           broad, added last)
   build the demo image in the registry
   copy the pgvector image into the registry
        |
   create the Container Apps environment (consumption plan, no env fee)
        |
   start ops-demo-db   (internal only, no disk, password = a secret)
        |
   start ops-demo-app  (public HTTPS, passcode, 30-ticket cap)
        |
   smoke test: page loads, gate says "passcode required", API refuses
        |       a call without it
   point the app at the 4 hosted agents (new revision, old one stays up
                until the new one is healthy)

4.5 Taking it down
   python -m hosted.demo down  -> removes the demo identity's role
   assignments first, then deletes "ops-triage-demo". It refuses any group
   not tagged purpose=temporary-demo, and never touches proj-foundryapp.
   Left behind: the images in the registry (cents a month).


5. WHO CAN DO WHAT (ROLE ASSIGNMENTS WE MADE)
---------------------------------------------------------------------
   Who                     Role                              Where
   ----------------------  --------------------------------  ------------
   You (deployer)          Foundry Project Manager           the project
   You (deployer)          Container Registry Repo. Writer   the registry
   Foundry project's       Container Registry Repo. Reader   the registry
     own identity
   each of the 4 agents    Cognitive Services OpenAI User    Foundry account
     (4 assignments)
   demo identity           Container Registry Repo. Reader   the registry
   demo identity           Cognitive Services OpenAI User    Foundry account
   demo identity           Foundry User  (broad)             the project
   ------------------------------------------------------------------
   Total: 10. Principle: each identity holds only what it needs; nothing
   uses a stored password or API key to reach the models.
   The registry is in "ABAC" mode, where the old AcrPull/AcrPush roles do
   NOT work; the Repository Reader / Writer roles are the ones that count.


6. HOW THE RUNNING SYSTEM IS CONNECTED
---------------------------------------------------------------------

   anyone with the passcode
        |  public HTTPS (the only door from the internet)
        v
   ops-demo-app  --internal TCP, short name ops-demo-db:5432-->  ops-demo-db
        |   (no address on the internet; password in a secret)
        |  keyless Entra tokens (the demo identity)
        v
   Foundry:  3 model deployments  +  4 hosted agents
             (each hosted agent calls the models with ITS OWN identity)

   Networking: no virtual network or private endpoints. The demo app is
   public by design; Foundry endpoints are public but require an Entra
   token; the demo database accepts connections only from inside its
   Container Apps environment.


7. COST MAP (list prices, swedencentral; check your own billing)
---------------------------------------------------------------------
   Container Registry (Basic) .... ~$5 / month, permanent
   Container Apps (demo) ......... billed per second while running:
                                   ~$0.05/hour idle, up to ~$0.16/hour active
                                   (both containers) => ~$1-4 per day
   Log Analytics ................. per GB ingested; a few MB here
   Model deployments ............. per token, only when tickets run
   Hosted agents ................. per running session
   Managed identity, resource groups, role assignments, provider opt-ins:
   free.
   After `down`: only the registry (and its images) keeps billing.


8. CONSIDERED BUT NOT USED
---------------------------------------------------------------------
   Azure Database for PostgreSQL   would give a permanent, keyless
                                   database (~$19/month); no capacity was
                                   available, and a throwaway container is
                                   enough for a demo. Plan kept in the repo.
   Key Vault                       we kept secrets in Container Apps'
                                   secret store; a real production system
                                   should put them in Key Vault.
   Application Insights            not connected to our project, so the
                                   platform's automatic agent tracing has
                                   nowhere to land.
   Virtual network / private       would remove the public door, at the
   endpoints                       cost of complexity and money.
   Static Web Apps / App Service   not needed: Container Apps serves both
                                   the page and the API.


9. NAMING (SO YOU CAN FIND THINGS)
---------------------------------------------------------------------
   proj-foundryapp           the permanent resource group
   proj-app                  the Foundry account AND its project
   ops-triage-generic / -security / -trivial, ops-routing
                             the four hosted agents (names shown in Foundry)
   <acr>                     the registry ("opstriage" + 6 characters)
   ops-triage-demo           the temporary resource group
   ops-demo-env / -app / -db / -identity
                             the demo's environment, two apps and identity
   OPS_ROLE                  which agent a container is
   (we avoid any env var starting with AGENT_ or FOUNDRY_: Foundry
   reserves those names)


10. COMMANDS TO SEE IT YOURSELF
---------------------------------------------------------------------
   az group list -o table
   az resource list -g proj-foundryapp -o table
   az resource list -g ops-triage-demo -o table
   az containerapp list -g ops-triage-demo -o table
   az cognitiveservices account deployment list -g proj-foundryapp
        -n proj-app -o table
   az role assignment list --all --assignee <identity-id> -o table
   cd backend && python -m hosted.demo status
