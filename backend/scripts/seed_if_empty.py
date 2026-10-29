"""Seed the RAG corpus only if it is empty — for a throwaway demo database that starts blank every time.

`python -m scripts.seed` always wipes and reloads; this is the startup-safe version: a restart of a
container that already has its corpus does nothing and spends no embedding calls.
"""
from db.models import HistoricalTicket, Runbook
from db.session import SessionLocal
from scripts.seed import seed


def main() -> None:
    db = SessionLocal()
    try:
        have = db.query(HistoricalTicket).count(), db.query(Runbook).count()
    finally:
        db.close()
    if all(have):
        print(f"corpus already present ({have[0]} tickets, {have[1]} runbooks) — not reseeding")
        return
    print("corpus is empty — seeding")
    seed()


if __name__ == "__main__":
    main()
