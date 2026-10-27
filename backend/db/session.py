from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)

if settings.database_auth_mode == "entra":
    from db.entra import database_token

    @event.listens_for(engine, "do_connect")
    def _inject_entra_token(dialect, conn_rec, cargs, cparams) -> None:
        # Runs for every NEW pooled connection, so a long-lived process always connects with a fresh token.
        cparams["password"] = database_token()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
