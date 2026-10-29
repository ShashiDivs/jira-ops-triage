// An EMPTY value means "same origin": the public demo serves this page from the API itself.
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

// A public demo can sit behind a shared passcode (the server decides; see /api/gate). It is kept for this
// browser tab only, never in localStorage, so closing the tab forgets it.
const PASSCODE_KEY = "ops-triage-passcode";
export const LOCKED_EVENT = "ops-triage:locked";

function storedPasscode(): string {
  try {
    return sessionStorage.getItem(PASSCODE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function savePasscode(passcode: string) {
  try {
    sessionStorage.setItem(PASSCODE_KEY, passcode);
  } catch {
    /* storage can be blocked; the passcode then lasts until reload */
  }
}

export function getStoredPasscode(): string {
  return storedPasscode();
}

function clearPasscode() {
  try {
    sessionStorage.removeItem(PASSCODE_KEY);
  } catch {
    /* ignore */
  }
}

/** Does this deployment ask for a passcode? Open to everyone. Unreachable API => false, so the normal errors show. */
export async function gateRequired(): Promise<boolean> {
  try {
    const res = await fetch(`${BASE_URL}/api/gate`);
    return res.ok ? Boolean((await res.json()).required) : false;
  } catch {
    return false;
  }
}

/** Checks a passcode with the server and, if it is right, remembers it for this tab. */
export async function verifyPasscode(passcode: string): Promise<boolean> {
  try {
    const res = await fetch(`${BASE_URL}/api/session`, { headers: { "X-Demo-Passcode": passcode } });
    if (res.ok) savePasscode(passcode);
    return res.ok;
  } catch {
    return false;
  }
}

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

export interface SimilarTicket {
  id: string;
  summary: string;
  description: string;
  component: string;
  severity: string;
  resolution: string;
  similarity: number;
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
  similar_tickets: SimilarTicket[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const passcode = storedPasscode();
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(passcode ? { "X-Demo-Passcode": passcode } : {}),
      ...init?.headers,
    },
  });
  if (res.status === 401) {
    // The server no longer accepts our passcode (changed, or never given): send the user back to the prompt.
    clearPasscode();
    window.dispatchEvent(new Event(LOCKED_EVENT));
  }
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

export function approveTicket(ticketId: string, approver: string, notes: string) {
  return request<Ticket>(`/api/tickets/${ticketId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approver, notes }),
  });
}

export function rejectTicket(ticketId: string, approver: string, notes: string) {
  return request<Ticket>(`/api/tickets/${ticketId}/reject`, {
    method: "POST",
    body: JSON.stringify({ approver, notes }),
  });
}

export function resetDemoState() {
  return request<{ status: string }>("/api/reset", { method: "POST" });
}

export function promoteToRag(ticketId: string) {
  return request<{ id: string; source_ticket_id: string; summary: string; resolution: string }>(
    `/api/tickets/${ticketId}/promote-to-rag`,
    { method: "POST" },
  );
}
