"""Unit tests for the Access Control / RBAC governance pattern (agents/access_control.py). No
DB/LLM needed — this module is pure policy logic over a configured name->role map."""
from agents.safety.access_control import AuthorizationError, authorize_approver, settings


def _with_roles(roles: str):
    """Temporarily overrides the configured approver role map, same save/restore pattern used
    by rag/test_retriever.py for settings.rag_min_similarity."""
    original = settings.approver_roles
    settings.approver_roles = roles
    return original


def main() -> None:
    original = _with_roles("alice:approver,bob:security_approver")
    try:
        print("--- Case 1: unknown approver is refused ---")
        try:
            authorize_approver("mallory", [])
            raise AssertionError("expected an unrecognized approver to be refused")
        except AuthorizationError as e:
            assert "not a recognized approver" in str(e)
        print("test_unknown_approver_is_refused: PASS")

        print("--- Case 2: empty approver name is refused ---")
        try:
            authorize_approver("", [])
            raise AssertionError("expected an empty approver name to be refused")
        except AuthorizationError:
            pass
        print("test_empty_approver_is_refused: PASS")

        print("--- Case 3: 'approver' role may approve an ordinary ticket ---")
        authorize_approver("alice", ["Triage confidence is below 0.7"])
        print("test_approver_role_allows_ordinary_ticket: PASS")

        print("--- Case 4: 'approver' role may NOT approve a security-flagged ticket ---")
        try:
            authorize_approver("alice", ["Ticket is security-related"])
            raise AssertionError("expected a plain approver to be refused on a security ticket")
        except AuthorizationError as e:
            assert "security_approver" in str(e)
        print("test_approver_role_refused_on_security_ticket: PASS")

        print("--- Case 5: 'security_approver' role may approve a security-flagged ticket ---")
        authorize_approver("bob", ["Ticket is security-related"])
        print("test_security_approver_role_allows_security_ticket: PASS")

        print("--- Case 6: 'security_approver' role may also approve an ordinary ticket ---")
        authorize_approver("bob", ["Severity is S1"])
        print("test_security_approver_role_allows_ordinary_ticket: PASS")

        print("--- Case 7: PII/payment-sensitive reason also requires security_approver ---")
        try:
            authorize_approver("alice", ["Ticket involves payment, billing, or PII"])
            raise AssertionError("expected a plain approver to be refused on a PII-sensitive ticket")
        except AuthorizationError:
            pass
        print("test_approver_role_refused_on_pii_ticket: PASS")
    finally:
        settings.approver_roles = original


if __name__ == "__main__":
    main()
