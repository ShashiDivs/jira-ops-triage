"""Keyless (Microsoft Entra) authentication to Azure Database for PostgreSQL.

With DATABASE_AUTH_MODE=entra the database password is not a stored secret: it is a short-lived
Entra access token that the caller fetches with its own identity (`az login` on a laptop, the
agent's identity when hosted in Foundry) and presents in place of a password at connect time.
Hosted agents are told not to carry secrets in env vars, so this is the mode the hosted
orchestrator runs in. The token is checked only while connecting, so a connection that is already
open keeps working after the token expires; every NEW connection gets a fresh token.

Imported lazily by its callers so password-mode users (and the free CI tier) never need
azure-identity to be importable.
"""
from psycopg import AsyncConnection

# The token audience for Azure Database for PostgreSQL / MySQL (same value in every cloud region).
DB_TOKEN_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

_provider = None


def database_token() -> str:
    """A bearer token to use as the Postgres password. The provider caches the token and only
    re-acquires it near expiry, so calling this per connection is cheap."""
    global _provider
    if _provider is None:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        _provider = get_bearer_token_provider(DefaultAzureCredential(), DB_TOKEN_SCOPE)
    return _provider()


def attach_token_auth(engine) -> None:
    """Make every NEW connection of a SQLAlchemy engine present a fresh Entra token as its password.
    Shared by the app's engine (db/session.py) and Alembic's, so migrations authenticate the same way."""
    from sqlalchemy import event

    @event.listens_for(engine, "do_connect")
    def _inject_entra_token(dialect, conn_rec, cargs, cparams) -> None:
        cparams["password"] = database_token()


class EntraAsyncConnection(AsyncConnection):
    """psycopg connection class that substitutes a fresh Entra token for the password on every
    new connection. Used by the checkpointer's pool, which opens connections over its lifetime."""

    @classmethod
    async def connect(cls, conninfo: str = "", **kwargs):
        import asyncio

        # Token acquisition can do blocking network I/O on a cold cache — keep it off the event loop.
        kwargs["password"] = await asyncio.to_thread(database_token)
        return await super().connect(conninfo, **kwargs)
