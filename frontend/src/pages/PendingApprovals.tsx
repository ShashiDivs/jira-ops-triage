import { useEffect, useState } from "react";
import { approveTicket, listPending, rejectTicket, type PendingTicket } from "../api/client";

export function PendingApprovals() {
  const [pending, setPending] = useState<PendingTicket[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      setPending(await listPending());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, []);

  async function decide(ticketId: string, decision: "approve" | "reject") {
    setBusy(ticketId);
    try {
      const fn = decision === "approve" ? approveTicket : rejectTicket;
      await fn(ticketId, notes[ticketId] ?? "");
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section>
      <h2>Pending approvals</h2>
      {error && <p className="error">{error}</p>}
      {pending.length === 0 && <p>Nothing waiting for human sign-off.</p>}
      {pending.map((t) => (
        <article key={t.ticket_id} className="pending-card">
          <header>
            <strong>{t.summary}</strong>
            <span className="badge">{t.severity ?? "?"}</span>
          </header>
          <p>{t.description}</p>
          <p>
            <b>Component:</b> {t.component} &nbsp; <b>Team:</b> {t.owning_team ?? "?"} &nbsp;{" "}
            <b>Confidence:</b> {t.confidence?.toFixed(2) ?? "?"}
          </p>
          <p>
            <b>Why HITL:</b> {t.hitl_reasons.join(", ")}
          </p>
          <details>
            <summary>Agent reasoning &amp; evidence</summary>
            <p>
              <b>Triage:</b> {t.triage_reasoning}
            </p>
            <p>
              <b>Routing:</b> {t.routing_reasoning}
            </p>
            <p>
              <b>Similar past tickets:</b>
            </p>
            <ul>
              {t.similar_tickets.map((s, i) => (
                <li key={i}>{JSON.stringify(s)}</li>
              ))}
            </ul>
          </details>
          <textarea
            placeholder="Optional notes"
            value={notes[t.ticket_id] ?? ""}
            onChange={(e) => setNotes({ ...notes, [t.ticket_id]: e.target.value })}
          />
          <div className="actions">
            <button disabled={busy === t.ticket_id} onClick={() => decide(t.ticket_id, "approve")}>
              Approve
            </button>
            <button disabled={busy === t.ticket_id} onClick={() => decide(t.ticket_id, "reject")}>
              Reject
            </button>
          </div>
        </article>
      ))}
    </section>
  );
}
