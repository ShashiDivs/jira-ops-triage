from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://ops_triage:ops_triage@localhost:5432/ops_triage"
    # "password" (default): the password inside DATABASE_URL. "entra": keyless — DATABASE_URL carries
    # NO password and every new connection presents a Microsoft Entra token instead (see db/entra.py).
    # For Azure Database for PostgreSQL; a hosted agent has no place to keep a secret, so it runs this way.
    database_auth_mode: str = "password"

    azure_ai_foundry_endpoint: str = ""
    azure_ai_foundry_api_key: str = ""
    # "key" (default): authenticate with AZURE_AI_FOUNDRY_API_KEY. "entra": keyless — use a
    # Microsoft Entra token from DefaultAzureCredential (az login locally, the agent identity or
    # a managed identity when hosted in Foundry). Hosted agents are told not to carry secrets in
    # images/env vars, so this is the mode they run in.
    azure_auth_mode: str = "key"
    # Injected by the Foundry hosted-agent platform at runtime (…/api/projects/<name>). Used as
    # the endpoint when AZURE_AI_FOUNDRY_ENDPOINT is unset; the client normalizes it to the
    # resource root.
    foundry_project_endpoint: str = ""
    azure_chat_deployment: str = "gpt-4o"
    # Cheaper/faster deployment for the supervisor's trivial/fast-path (Model Routing). Opt-in:
    # empty means "use azure_chat_deployment for everything", same as before this existed.
    azure_chat_deployment_fast: str = ""
    # Stronger/slower deployment reserved for the security triage path, where deeper reasoning
    # is worth the latency (Model Routing). Opt-in: empty means "use azure_chat_deployment".
    azure_chat_deployment_security: str = ""
    azure_embedding_deployment: str = "text-embedding-3-small"
    azure_embedding_dim: int = 1536

    # Stopgap while the Azure subscription is disabled: plain OpenAI, used in place of Azure
    # whenever OPENAI_API_KEY is set. Remove it from .env to switch back to Azure.
    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_chat_model_fast: str = ""
    openai_chat_model_security: str = ""
    openai_embedding_model: str = "text-embedding-3-small"

    # --- Hosted-agent topology (Foundry hosted agents) ---
    # Which agent this process serves when started via `python -m hosted.server`:
    # "orchestrator" (the LangGraph pipeline + approvals) or one specialist — "triage-generic",
    # "triage-security", "triage-trivial", "routing". Unused by the normal FastAPI app.
    # NOTE: named OPS_ROLE, not AGENT_ROLE — the Foundry platform rejects any container env var that
    # starts with FOUNDRY_ or AGENT_ ("reserved for platform use"), which the first live deploy hit.
    ops_role: str = "orchestrator"
    # Orchestrator side: where each specialist lives, as "role=url" pairs, comma-separated, e.g.
    # "triage-generic=http://localhost:8101/invocations,routing=http://localhost:8104/invocations".
    # Empty (the default) means every classification runs in-process exactly as before. A
    # specialist that is unreachable falls back to the in-process path, so this is safe to set.
    specialist_urls: str = ""
    # "none" for plain local HTTP; "entra" to send an Entra bearer token (Foundry-hosted agents).
    specialist_auth: str = "none"
    specialist_timeout_seconds: float = 60.0

    cors_origins: list[str] = ["http://localhost:5173"]

    # Access control for HITL approve/reject decisions (Governance pattern — Access Control /
    # RBAC). No login/session system for this teaching demo — instead a static "name:role" map
    # configured here. Two roles: "approver" (can approve/reject ordinary tickets) and
    # "security_approver" (required for tickets flagged security-related or PII/payment-
    # sensitive — least-privilege: not everyone should be able to sign off on those). Empty by
    # default; an empty map means every approval is refused until this is configured, which is
    # deliberately fail-closed rather than fail-open.
    approver_roles: str = ""

    # Scheduled/autonomous ticket generation (teaching extension) — off by default so it never
    # silently starts burning API calls; flip on in .env or via /api/scheduler/resume.
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 300

    # RAG retrieval hardening. Default 0.0 (permissive) so existing behavior doesn't change
    # unless a threshold is deliberately configured — a result is dropped only if its
    # similarity (1 - cosine_distance) falls below this.
    rag_min_similarity: float = 0.0

    # Semantic duplicate guard on promotion (RAG Phase 3.5). Deliberately much stricter than
    # rag_min_similarity, which asks "is this related enough to be worth surfacing" — this asks
    # "is this essentially the same content already in the corpus," to stop two different
    # tickets describing the same recurring incident from both landing as near-identical
    # vectors and skewing future retrieval.
    rag_duplicate_similarity_threshold: float = 0.95

    # Hybrid search (RAG Phase 4) — blends vector similarity with Postgres full-text keyword
    # rank so an exact component/service-name match doesn't lose to a vaguely-similar-sounding
    # but wrong document. Toggleable so the before/after is easy to demo in class; the blend
    # only changes *ranking*, not the RAG_MIN_SIMILARITY relevance floor above (that still
    # filters on raw vector similarity).
    rag_hybrid_search_enabled: bool = True
    # Weight given to vector similarity in the blend; keyword rank gets (1 - this). Both sides
    # of the blend are normalized to [0, 1) before combining, so 0.5 is a genuine even split.
    rag_hybrid_vector_weight: float = 0.6

    # The shared .env file also carries POSTGRES_*/VITE_* vars used by docker-compose and the
    # frontend, not by this Settings model — ignore rather than reject those as unknown keys.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def specialist_url_map(self) -> dict[str, str]:
        """Parses SPECIALIST_URLS ('role=url,role=url') into a role -> url dict. Malformed
        entries are skipped rather than raising, same convention as approver_role_map."""
        urls: dict[str, str] = {}
        for entry in self.specialist_urls.split(","):
            role, sep, url = entry.strip().partition("=")
            if sep and role.strip() and url.strip():
                urls[role.strip()] = url.strip()
        return urls

    @property
    def azure_endpoint(self) -> str:
        """The configured Foundry endpoint: the explicit setting, else the platform-injected
        project endpoint."""
        return self.azure_ai_foundry_endpoint or self.foundry_project_endpoint

    @property
    def azure_configured(self) -> bool:
        has_credentials = bool(self.azure_ai_foundry_api_key) or self.azure_auth_mode == "entra"
        return bool(self.azure_endpoint) and has_credentials

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def llm_configured(self) -> bool:
        return self.openai_configured or self.azure_configured

    @property
    def approver_role_map(self) -> dict[str, str]:
        """Parses APPROVER_ROLES ('alice:approver,bob:security_approver') into a name -> role
        dict. Malformed entries (no ':') are skipped rather than raising, so a typo in .env
        degrades to "that name isn't recognized" instead of crashing the app."""
        roles: dict[str, str] = {}
        for entry in self.approver_roles.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            name, _, role = entry.partition(":")
            name, role = name.strip(), role.strip()
            if name and role:
                roles[name] = role
        return roles

    @property
    def psycopg_dsn(self) -> str:
        """Plain postgresql:// DSN for langgraph's Postgres checkpointer (psycopg), as
        opposed to the SQLAlchemy `postgresql+psycopg://` URL used elsewhere."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
