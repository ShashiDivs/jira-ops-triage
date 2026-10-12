from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://ops_triage:ops_triage@localhost:5432/ops_triage"

    azure_ai_foundry_endpoint: str = ""
    azure_ai_foundry_api_key: str = ""
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
    def azure_configured(self) -> bool:
        return bool(self.azure_ai_foundry_endpoint and self.azure_ai_foundry_api_key)

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
