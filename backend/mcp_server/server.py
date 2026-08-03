"""MCP server exposing the mock Jira store and pgvector RAG search as tools over stdio.
Same tool surface as the reference project so the agent graph doesn't need to change:
create_ticket, update_ticket, add_comment, get_ticket, list_tickets,
search_similar_tickets, search_runbooks.
"""
from mcp.server.fastmcp import FastMCP

from mcp_server import jira_store
from rag import retriever

mcp = FastMCP("ops-triage-jira")


@mcp.tool()
def create_ticket(
    summary: str,
    description: str,
    component: str = "",
    reporter: str = "",
    severity: str | None = None,
    confidence: float | None = None,
    owning_team: str | None = None,
    status: str = "pending",
    reasoning: dict | None = None,
) -> dict:
    """Create a new ticket in the mock Jira store."""
    return jira_store.create_ticket(
        summary=summary,
        description=description,
        component=component,
        reporter=reporter,
        severity=severity,
        confidence=confidence,
        owning_team=owning_team,
        status=status,
        reasoning=reasoning,
    )


@mcp.tool()
def update_ticket(
    ticket_id: str,
    severity: str | None = None,
    confidence: float | None = None,
    owning_team: str | None = None,
    status: str | None = None,
    reasoning: dict | None = None,
) -> dict:
    """Update fields on an existing ticket. Fields left as None are unchanged."""
    fields = {
        k: v
        for k, v in {
            "severity": severity,
            "confidence": confidence,
            "owning_team": owning_team,
            "status": status,
            "reasoning": reasoning,
        }.items()
        if v is not None
    }
    return jira_store.update_ticket(ticket_id, **fields)


@mcp.tool()
def add_comment(ticket_id: str, author: str, body: str) -> dict:
    """Add an audit comment to a ticket."""
    return jira_store.add_comment(ticket_id, author, body)


@mcp.tool()
def get_ticket(ticket_id: str) -> dict | None:
    """Fetch a single ticket by id."""
    return jira_store.get_ticket(ticket_id)


@mcp.tool()
def list_tickets(status: str | None = None) -> list[dict]:
    """List tickets, optionally filtered by status."""
    return jira_store.list_tickets(status=status)


@mcp.tool()
def search_similar_tickets(
    query: str, k: int = 3, ticket_id: str | None = None, node_name: str | None = None
) -> list[dict]:
    """Search historical resolved tickets by pgvector similarity. ticket_id/node_name are
    optional — pass them from a graph node to get this call logged in retrieval_log."""
    return retriever.search_similar_tickets(query, k=k, ticket_id=ticket_id, node_name=node_name)


@mcp.tool()
def search_runbooks(
    query: str,
    k: int = 3,
    doc_type: str | None = None,
    ticket_id: str | None = None,
    node_name: str | None = None,
) -> list[dict]:
    """Search runbook/policy docs by pgvector similarity. ticket_id/node_name are optional —
    pass them from a graph node to get this call logged in retrieval_log."""
    return retriever.search_runbooks(query, k=k, doc_type=doc_type, ticket_id=ticket_id, node_name=node_name)


if __name__ == "__main__":
    mcp.run(transport="stdio")
