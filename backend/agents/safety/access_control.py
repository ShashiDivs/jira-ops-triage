"""Access control for human approve/reject decisions (Governance pattern — Access Control /
RBAC, least-privilege). Deliberately a simple, static role map rather than a full login/session
system — enough to demonstrate the pattern for teaching, and to make `decided_by` (previously
never written — see reference/agentic-teaching-notes.md) actually mean something.

Two roles:
- "approver": can approve/reject ordinary tickets.
- "security_approver": required for tickets flagged security-related or payment/billing/PII-
  sensitive — least-privilege in action: not every approver should be able to sign off on those.
"""
from config import get_settings

settings = get_settings()

# Must match the exact strings agents/graph.py's _hitl_reasons() appends for security/sensitive
# tickets — kept as a set here rather than re-deriving the logic, since the two modules already
# agree on the two specific reason strings.
_SECURITY_REASONS = {"Ticket is security-related", "Ticket involves payment, billing, or PII"}


class AuthorizationError(PermissionError):
    """Raised when the named approver is unknown, or lacks the role required for this ticket."""


def authorize_approver(approver: str, hitl_reasons: list[str]) -> None:
    """Raises AuthorizationError if `approver` may not approve/reject a ticket with the given
    hitl_reasons. Called before the decision is applied — never after."""
    if not approver or not approver.strip():
        raise AuthorizationError("an approver name is required")

    role = settings.approver_role_map.get(approver)
    if role is None:
        raise AuthorizationError(f"'{approver}' is not a recognized approver")

    triggered_security_reasons = [r for r in hitl_reasons if r in _SECURITY_REASONS]
    if triggered_security_reasons and role != "security_approver":
        raise AuthorizationError(
            f"'{approver}' has role '{role}', but this ticket requires a security_approver "
            f"(reasons: {', '.join(triggered_security_reasons)})"
        )
