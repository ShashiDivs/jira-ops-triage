"""Seed the historical_tickets and runbooks tables from data/*.json, embedding each doc
via Azure AI Foundry. Run once after migrations: `python -m scripts.seed`.
"""
import json
from pathlib import Path

from agents.llm import embed_texts, llm_configured
from db.models import HistoricalTicket, Runbook
from db.session import SessionLocal

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _historical_ticket_text(t: dict) -> str:
    return f"{t['summary']}\n{t['description']}\nResolution: {t['resolution']}"


def _runbook_text(r: dict) -> str:
    return f"{r['title']}\n{r['content']}"


def seed() -> None:
    if not llm_configured():
        raise SystemExit(
            "Azure AI Foundry is not configured. Set AZURE_AI_FOUNDRY_ENDPOINT and "
            "AZURE_AI_FOUNDRY_API_KEY before seeding — embeddings must come from a real deployment."
        )

    historical_tickets = json.loads((DATA_DIR / "historical_tickets.json").read_text())
    runbooks = json.loads((DATA_DIR / "runbooks_sla.json").read_text())

    ticket_embeddings = embed_texts([_historical_ticket_text(t) for t in historical_tickets])
    runbook_embeddings = embed_texts([_runbook_text(r) for r in runbooks])

    db = SessionLocal()
    try:
        deleted_tickets = db.query(HistoricalTicket).delete()
        deleted_runbooks = db.query(Runbook).delete()

        for t, emb in zip(historical_tickets, ticket_embeddings, strict=True):
            db.add(
                HistoricalTicket(
                    summary=t["summary"],
                    description=t["description"],
                    component=t.get("component", ""),
                    severity=t.get("severity", ""),
                    resolution=t.get("resolution", ""),
                    embedding=emb,
                )
            )

        for r, emb in zip(runbooks, runbook_embeddings, strict=True):
            db.add(
                Runbook(
                    doc_type=r["doc_type"],
                    title=r["title"],
                    content=r["content"],
                    embedding=emb,
                )
            )

        db.commit()
        print(
            f"Seeded {len(historical_tickets)} historical tickets "
            f"(replaced {deleted_tickets}) and {len(runbooks)} runbooks (replaced {deleted_runbooks})."
        )
    finally:
        db.close()


if __name__ == "__main__":
    seed()
