import { useEffect, useState } from "react";
import { approveTicket, listPending, rejectTicket, type PendingTicket } from "../api/client";

// Thresholds reflect what this corpus/embedding model actually produces (see
// reference/agentic-teaching-notes.md section 7) — a genuine match here scores ~0.42-0.59,
// not 0.9+, so "strong" is calibrated to this data rather than a generic intuition.
function similarityClass(similarity: number): string {
  if (similarity >= 0.55) return "similarity-strong";
  if (similarity >= 0.35) return "similarity-moderate";
  return "similarity-weak";
}

export function PendingApprovals() {
  const [pending, setPending] = useState<PendingTicket[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Reviewer identity (RBAC — Governance Access Control). Not per-ticket: whoever's at this
  // browser signs their name once, same as swiping a badge at the start of a shift. Persisted
  // in localStorage purely so a page refresh doesn't lose it; enforcement itself happens
  // server-side (agents/access_control.py) — this is just the identity claim, not the check.
  const [approver, setApprover] = useState(() => localStorage.getItem("approverName") ?? "");

  useEffect(() => {
    localStorage.setItem("approverName", approver);
  }, [approver]);

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
    if (!approver.trim()) {
      setError("Enter your name in \"Approving as\" before approving or rejecting.");
      return;
    }
    setBusy(ticketId);
    try {
      const fn = decision === "approve" ? approveTicket : rejectTicket;
      await fn(ticketId, approver.trim(), notes[ticketId] ?? "");
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
      <div className="approver-bar">
        <label>
          Approving as
          <input placeholder="your name" value={approver} onChange={(e) => setApprover(e.target.value)} />
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      {pending.length === 0 && <p className="muted">Nothing waiting for human sign-off.</p>}
      {pending.map((t) => (
        <article key={t.ticket_id} className="pending-card">
          <header>
            <strong>{t.summary}</strong>
            {t.severity && <span className="badge" data-sev={t.severity}>{t.severity}</span>}
          </header>
          <p>{t.description}</p>
          <p>
            <b>Component:</b> {t.component} &nbsp; <b>Team:</b> {t.owning_team ?? "?"} &nbsp;{" "}
            <b>Confidence:</b> {t.confidence?.toFixed(2) ?? "?"}
          </p>
          <div className="hitl-reasons">
            {t.hitl_reasons.map((r) => (
              <span key={r} className="reason-tag">{r}</span>
            ))}
          </div>
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
            {t.similar_tickets.length === 0 && <p className="muted">No similar past tickets retrieved.</p>}
            <ul className="similar-tickets">
              {t.similar_tickets.map((s) => (
                <li key={s.id}>
                  <span className={`similarity-badge ${similarityClass(s.similarity)}`}>
                    {Math.round(s.similarity * 100)}% similar
                  </span>
                  <span className="badge" data-sev={s.severity}>{s.severity}</span> {s.summary}
                  <br />
                  <span className="muted">Resolved by: {s.resolution}</span>
                </li>
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
