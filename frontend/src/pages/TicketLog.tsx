import { useEffect, useState } from "react";
import { listTickets, type Ticket } from "../api/client";

export function TicketLog() {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [error, setError] = useState<string | null>(null);

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
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {tickets.map((t) => (
            <tr key={t.id}>
              <td>{t.summary}</td>
              <td>{t.component}</td>
              <td>{t.severity ?? "-"}</td>
              <td>{t.owning_team ?? "-"}</td>
              <td>{t.status}</td>
              <td>{t.created_at ? new Date(t.created_at).toLocaleString() : "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
