# Governance Recommendations for Agents

This document lists additional governance controls, patterns, and practical implementation notes you can add to the current `backend/agents` safeguards (guardrails, HITL policy, circuit-breakers, audit rows).

## High-level goals
- Reduce risk from incorrect or malicious model behavior
- Maintain auditability and traceability for decisions
- Protect sensitive data and enforce privacy obligations
- Ensure resilience and principled fallbacks when dependencies fail

## Categories & Controls

1. Access Control & Identity
   - Enforce least-privilege IAM for components that call LLMs, databases, and MCP tools.
   - Use short-lived credentials and scoped service principals for the LLM and database clients.
   - Require RBAC for human approvers: only designated groups can `approve` high-risk decisions.

2. Auditability & Immutable Evidence
   - Persist immutable audit records for every triage decision (inputs, model output, final decision, user who acted).
   - Store hashes of any LLM prompt/context and model response alongside the human decision to enable later repro and non-repudiation.
   - Retain the RAG retrieval snapshot (which runbook/docs and similarity scores were used) with the audit row.

3. Input/Output Validation & Contracts
   - Extend `agents/llm/contracts.py` with stricter JSON schemas and schema evolution policy (versioned contracts).
   - Enforce an output vetting layer: validate shapes, ranges, and enumerations; reject or escalate on mismatch.
   - Add a transformation sandbox that strips/normalizes PII before sending to LLMs, and canonicalizes model outputs.

4. Prompt & Response Guardrails
   - Implement layered guardrails: syntactic (already present), semantic (detect improbable claims), and provenance checks.
   - Maintain an allowlist/blocklist for sensitive terms and disallowed actions; block or require explicit HITL.
   - Use prompt templates with injected, immutable safety instructions (policy-as-code) rather than freeform prompt-building.

5. Data Governance & Provenance
   - Tag embeddings and RAG corpus rows with `source`, `embedding_model`, and `embedding_version` to detect stale indexes.
   - Track dataset provenance and retention windows; remove or redact PII from historical tickets per policy.
   - Enforce differential access controls for corpora that contain sensitive runbooks.

6. Model Versioning & Canarying
   - Pin model deployment names; record model id + prompt template in audit rows.
   - Canary new model variants on a small percentage of tickets and compare outputs vs baseline (A/B and differential testing).
   - Rollback automatically if error, drift, or safety metric regressions exceed thresholds.

7. Testing, Monitoring & Metrics
   - Add drift detectors (distributional and semantic) for inputs, similarity scores, and model outputs.
   - Track safety metrics: contract failure rate, HITL trigger rate, human override rate, false accept/reject rates.
   - Create dashboards and alerts for sudden shifts (e.g., spike in contract violations or circuit-breaker trips).

8. Resilience, Rate Limits & Cost Controls
   - Expand circuit-breaker logic with tiered fallbacks (fast-path heuristic → restricted mode → stop accepting writes).
   - Apply request-rate quotas per upstream client/service to limit blast radius and cost.
   - Implement budget guards that alert and optionally throttle embedding/indexing jobs when spend thresholds are crossed.

9. Human-in-the-loop Policies & Workflows
   - Formalize approval policies: explicit criteria, SLAs, and audit checklists recorded at approval time.
   - Provide reviewers with structured context: top-k similar tickets, runbook excerpts, model confidence and explanation.
   - Record reviewer identity, rationale, and time-to-decision; require secondary review for high-impact actions.

10. Explainability & Model Reasoning
   - Request model rationales alongside decisions and persist them; provide simple extraction (why X was chosen).
   - Implement a lightweight explanation verifier: check rationale for consistency with recovered evidence (RAG docs, similarity scores).

11. Privacy, PII, and Legal Compliance
   - Apply PII detection and redaction pre-send; use a high-confidence detector and log redaction decisions.
   - Maintain configurable retention policies and an erasure workflow tied to the DB and RAG index.
   - Document legal justifications for automated decisions and ensure consent/notice where required.

12. CI/CD, Change Management, & Policy-as-Code
   - Treat prompts, prompt templates, and safety rules as code with PR review, tests, and staged rollouts.
   - Add unit/regression tests that exercise contract validation and known adversarial examples.
   - Require security review and dependency scanning for any new third-party SDKs or agents.

13. Incident Response & Playbooks
   - Maintain a playbook for model-related incidents (unexpected behaviour, data leak, excessive cost).
   - Include steps: rotate credentials, disable model access (feature flag), preserve forensic snapshots, notify stakeholders.

## Implementation notes (quick wins)
- Persist full model request/response pairs to a secure audit store with access controls.
- Version `agents/llm/contracts.py` and require backwards-compatible changes (or migration scripts) before deployment.
- Add unit tests that feed adversarial or malformed inputs to `guardrails.check_guardrails()` and `llm._chat_json()`.
- Add a `policy` table to the DB that stores active safety thresholds (confidence cutoff, hitl_threshold, cost cap) so operators can tune without code deploy.

## Suggested next steps
- Review which controls you want to prioritize (Auditability, Prompt Guards, Privacy, Monitoring).
- I can: implement hardened contract validation tests, add PII-redaction + detector wiring, or add a simple policy table and runtime checks — pick one and I'll implement it.
