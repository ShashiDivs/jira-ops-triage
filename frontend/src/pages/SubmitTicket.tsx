import { useState } from "react";
import { submitTicket } from "../api/client";

const PRESETS = [
  {
    label: "Outage",
    summary: "Complete outage of order-service, all customers affected",
    description: "order-service is returning 500s for every request. No orders are completing.",
    component: "order-service",
  },
  {
    label: "PII exposure",
    summary: "Customer PII exposed in support export",
    description: "The support dashboard CSV export includes unredacted customer emails and phone numbers.",
    component: "support-dashboard",
  },
  {
    label: "Slow search",
    summary: "Search results slow to load",
    description: "Product search queries are taking 6-8 seconds to return results.",
    component: "search-service",
  },
  {
    label: "Footer typo",
    summary: "Footer copyright year is wrong",
    description: "The marketing site footer still shows last year's copyright year.",
    component: "marketing-site",
  },
];

export function SubmitTicket() {
  const [summary, setSummary] = useState("");
  const [description, setDescription] = useState("");
  const [component, setComponent] = useState("");
  const [reporter, setReporter] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setResult(null);
    try {
      const res = await submitTicket({ summary, description, component, reporter });
      setResult(`Ticket ${res.ticket_id} — status: ${res.status}`);
    } catch (err) {
      setResult(`Error: ${(err as Error).message}`);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section>
      <h2>Submit a ticket</h2>
      <div className="presets">
        {PRESETS.map((p) => (
          <button
            key={p.label}
            type="button"
            onClick={() => {
              setSummary(p.summary);
              setDescription(p.description);
              setComponent(p.component);
            }}
          >
            {p.label}
          </button>
        ))}
      </div>
      <form onSubmit={handleSubmit}>
        <label>
          Summary
          <input value={summary} onChange={(e) => setSummary(e.target.value)} required />
        </label>
        <label>
          Description
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} required rows={4} />
        </label>
        <label>
          Component
          <input value={component} onChange={(e) => setComponent(e.target.value)} />
        </label>
        <label>
          Reporter
          <input value={reporter} onChange={(e) => setReporter(e.target.value)} />
        </label>
        <button type="submit" disabled={submitting}>
          {submitting ? "Submitting…" : "Submit ticket"}
        </button>
      </form>
      {result && <p className="result">{result}</p>}
    </section>
  );
}
