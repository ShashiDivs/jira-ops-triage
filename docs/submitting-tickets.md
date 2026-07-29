# Submitting Tickets by Severity

How to raise tickets through the dashboard, what determines their severity (S1–S4), and which
ones will pause for human approval.

## Severity levels

Defined in `backend/data/runbooks_sla.json` → `RUNBOOK-SEVERITY`, used by the Triage agent:

| Severity | Meaning | Response target |
|---|---|---|
| **S1** — Critical | Complete outage, data loss, security breach, or unauthorized access affecting any customers | Immediate |
| **S2** — High | Partial outage or major degradation affecting a significant subset of customers, or a core workflow (checkout, login, search) | Within 1 hour |
| **S3** — Medium | Degraded functionality/performance affecting a limited subset of users, or a non-core workflow | Within 1 business day |
| **S4** — Low | Cosmetic issues, minor copy errors, non-blocking bugs with no functional impact | Next regular sprint |

Severity isn't fixed by keyword rules alone — the Triage agent (or the keyword-heuristic
fallback, if no LLM provider is configured) classifies it from the ticket text, grounded in the
severity policy and similar historical tickets.

## Human-in-the-loop (HITL) approval

A submitted ticket pauses on the **Pending approvals** tab instead of auto-filing if **any** of
these are true, regardless of its severity:

- Severity is **S1**
- The ticket is **security-related**
- Triage **confidence is below 0.7**
- The ticket involves **payment, billing, or PII**

Everything else auto-approves immediately and shows up straight in **Ticket log**.

## How to submit

1. Open the dashboard at **http://localhost:5173**.
2. On the **Submit** tab, either click a preset (**Outage**, **PII exposure**, **Slow search**,
   **Footer typo**) or fill in your own Summary / Description / Component / Reporter.
3. Click **Submit ticket**.
4. If it needs sign-off, go to **Pending approvals** — expand "Agent reasoning & evidence" to see
   why, then **Approve** or **Reject** (with optional notes).
5. **Ticket log** shows every ticket's final severity, team, and status.
6. **Reset demo state** (top right) wipes everything back to empty.

## Example tickets, by severity

### S1 — Critical

| Summary | Description | Component | Also triggers HITL via |
|---|---|---|---|
| Complete outage of order-service, all customers affected | order-service is returning 500s for every request. | order-service | Severity itself |
| Unauthorized access to internal admin panel detected | Repeated logins from an unrecognized IP using a decommissioned service account. | admin-panel | Security-related |
| Customer PII exposed in support ticket export | CSV export included unredacted customer emails and phone numbers. | support-dashboard | PII |
| Billing double-charged a batch of customers | A retry didn't check idempotency; ~40 customers were charged twice. | billing-service | Payment/billing |

### S2 — High

| Summary | Description | Component |
|---|---|---|
| order-service intermittent 502s under peak load | ~8% of requests returned 502 during a flash sale. | order-service |
| Search results ranking noticeably degraded for product queries | A re-index job used a stale synonym dictionary. | search-service |

### S3 — Medium

| Summary | Description | Component |
|---|---|---|
| Slow page load on user profile page for large accounts | Accounts with 500+ saved addresses take 8–10s to load. | profile-service |
| Notification emails delayed by up to 20 minutes | The email queue consumer scaled down overnight. | notifications-service |

### S4 — Low

| Summary | Description | Component |
|---|---|---|
| Footer copyright year still shows last year | Hardcoded year constant never updates. | marketing-site |
| Typo in password reset email subject line | Subject reads "Reset Your Passwrod". | notifications-service |

## Reference: what the agents actually match on

The **Supervisor** routes each ticket to a specialized triage path based on these keyword lists
(`backend/agents/llm.py`) before the LLM ever sees it — and the same lists back the offline
keyword-heuristic fallback used when no LLM provider is configured:

- **Security path** (forces HITL) — matches: `breach`, `unauthorized`, `exposure`, `exposed`,
  `compromise`, `exploit`, `leak`, `hacked`
- **Trivial/fast path** (cheap model) — matches: `typo`, `copy`, `cosmetic`, `footer`, `spelling`
- **Sensitive** (forces HITL regardless of severity) — matches: `payment`, `billing`, `charge`,
  `charged`, `pii`, `personal data`, `phone number`, `email address`
- Everything else goes through the **generic path** (full RAG-grounded triage).
