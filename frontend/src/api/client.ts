const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export interface Comment {
  author: string;
  body: string;
  created_at: string;
}

export interface Ticket {
  id: string;
  summary: string;
  description: string;
  component: string;
  reporter: string;
  severity: string | null;
  confidence: number | null;
  owning_team: string | null;
  status: string;
  reasoning: Record<string, unknown>;
  created_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  comments: Comment[];
}

export interface PendingTicket {
  ticket_id: string;
  summary: string;
  description: string;
  component: string;
  severity: string | null;
  confidence: number | null;
  owning_team: string | null;
  triage_reasoning: string | null;
  routing_reasoning: string | null;
  hitl_reasons: string[];
  similar_tickets: Record<string, unknown>[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export function submitTicket(payload: {
  summary: string;
  description: string;
  component: string;
  reporter: string;
}) {
  return request<{ ticket_id: string; status: string }>("/api/tickets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listTickets(status?: string) {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<Ticket[]>(`/api/tickets${qs}`);
}

export function listPending() {
  return request<PendingTicket[]>("/api/tickets/pending");
}

export function approveTicket(ticketId: string, notes: string) {
  return request<Ticket>(`/api/tickets/${ticketId}/approve`, {
    method: "POST",
    body: JSON.stringify({ notes }),
  });
}

export function rejectTicket(ticketId: string, notes: string) {
  return request<Ticket>(`/api/tickets/${ticketId}/reject`, {
    method: "POST",
    body: JSON.stringify({ notes }),
  });
}

export function resetDemoState() {
  return request<{ status: string }>("/api/reset", { method: "POST" });
}
