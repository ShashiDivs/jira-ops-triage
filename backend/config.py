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
    azure_embedding_deployment: str = "text-embedding-3-small"
    azure_embedding_dim: int = 1536

    # Stopgap while the Azure subscription is disabled: plain OpenAI, used in place of Azure
    # whenever OPENAI_API_KEY is set. Remove it from .env to switch back to Azure.
    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_chat_model_fast: str = ""
    openai_embedding_model: str = "text-embedding-3-small"

    cors_origins: list[str] = ["http://localhost:5173"]

    # Scheduled/autonomous ticket generation (teaching extension) — off by default so it never
    # silently starts burning API calls; flip on in .env or via /api/scheduler/resume.
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 300

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
    def psycopg_dsn(self) -> str:
        """Plain postgresql:// DSN for langgraph's Postgres checkpointer (psycopg), as
        opposed to the SQLAlchemy `postgresql+psycopg://` URL used elsewhere."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
