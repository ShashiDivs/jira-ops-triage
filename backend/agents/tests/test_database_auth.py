"""Unit test for keyless (Entra) Postgres authentication — no database, no Azure.

Two seams carry the database password: SQLAlchemy (all ticket/RAG/observability queries, via
db.session) and psycopg's pool (the LangGraph checkpointer). In entra mode each must present a FRESH
token on every new connection, and in password mode neither may touch Azure at all.
"""
import asyncio
import os
import sys

os.environ["DATABASE_AUTH_MODE"] = "entra"  # before db.session is imported: it reads settings once at import

import psycopg  # noqa: E402

import db.entra as entra  # noqa: E402


def _fake_token_source():
    counter = {"n": 0}

    def token() -> str:
        counter["n"] += 1
        return f"tok-{counter['n']}"

    return token, counter


def test_sqlalchemy_gets_a_fresh_token_per_connection() -> None:
    token, _ = _fake_token_source()
    entra.database_token = token  # db.session does `from db.entra import database_token` at import
    from db.session import engine

    seen = []
    for _ in range(2):
        cparams: dict = {"user": "agent"}
        engine.dialect.dispatch.do_connect(engine.dialect, None, (), cparams)
        seen.append(cparams["password"])
    assert seen == ["tok-1", "tok-2"], f"each new connection must ask for a token, got {seen}"
    print("test_sqlalchemy_gets_a_fresh_token_per_connection: PASS")


def test_pool_connection_class_injects_token() -> None:
    token, _ = _fake_token_source()
    entra.database_token = token
    captured: dict = {}

    async def fake_connect(cls, conninfo="", **kwargs):
        captured.update(conninfo=conninfo, **kwargs)
        return "conn"

    original = psycopg.AsyncConnection.connect
    psycopg.AsyncConnection.connect = classmethod(fake_connect)
    try:
        result = asyncio.run(entra.EntraAsyncConnection.connect("postgresql://u@h/db", autocommit=True))
    finally:
        psycopg.AsyncConnection.connect = original
    assert result == "conn"
    assert captured["password"] == "tok-1"
    assert captured["autocommit"] is True and captured["conninfo"] == "postgresql://u@h/db"
    print("test_pool_connection_class_injects_token: PASS")


def test_password_mode_never_loads_azure_identity() -> None:
    import subprocess

    code = (
        "import sys, os; os.environ['DATABASE_AUTH_MODE']='password'; "
        "import db.session, agents.runtime; "
        "assert 'azure.identity' not in sys.modules and 'db.entra' not in sys.modules, 'password mode touched Azure'; "
        "print('ok')"
    )
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_AUTH_MODE"}
    env["DATABASE_AUTH_MODE"] = "password"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-500:]
    print("test_password_mode_never_loads_azure_identity: PASS")


if __name__ == "__main__":
    test_sqlalchemy_gets_a_fresh_token_per_connection()
    test_pool_connection_class_injects_token()
    test_password_mode_never_loads_azure_identity()
    print("All database auth tests passed.")
