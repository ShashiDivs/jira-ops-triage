import { useEffect, useState } from "react";
import { listTickets, promoteToRag, type Ticket } from "../api/client";

export function TicketLog() {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [promoteResult, setPromoteResult] = useState<Record<string, string>>({});

  async function refresh() {
    try {
      setTickets(await listTickets());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 8000);
    return () => clearInterval(id);
  }, []);

  async function promote(ticketId: string) {
    setBusy(ticketId);
    try {
      const result = await promoteToRag(ticketId);
      setPromoteResult({ ...promoteResult, [ticketId]: `Added to RAG corpus (id ${result.id.slice(0, 8)})` });
    } catch (err) {
      setPromoteResult({ ...promoteResult, [ticketId]: (err as Error).message });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section>
      <h2>Ticket log (mock Jira)</h2>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>Summary</th>
            <th>Component</th>
            <th>Severity</th>
            <th>Team</th>
            <th>Status</th>
            <th>Decided by</th>
            <th>Created</th>
            <th>RAG corpus</th>
          </tr>
        </thead>
        <tbody>
          {tickets.map((t) => (
            <tr key={t.id}>
              <td>{t.summary}</td>
              <td>{t.component}</td>
              <td>{t.severity ? <span className="badge" data-sev={t.severity}>{t.severity}</span> : "-"}</td>
              <td>{t.owning_team ?? "-"}</td>
              <td><span className="status-pill" data-status={t.status}>{t.status.replace("_", " ")}</span></td>
              <td>{t.decided_by ?? "-"}</td>
              <td>{t.created_at ? new Date(t.created_at).toLocaleString() : "-"}</td>
              <td>
                {t.status === "approved" ? (
                  <>
                    <button disabled={busy === t.id} onClick={() => promote(t.id)}>
                      Promote to RAG
                    </button>
                    {promoteResult[t.id] && <div className="muted">{promoteResult[t.id]}</div>}
                  </>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
