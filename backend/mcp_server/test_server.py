"""Standalone MCP smoke test: verifies the mock-Jira + RAG tools work over stdio."""
import asyncio

from agents.llm.client import llm_configured
from agents.integrations.mcp_client import MCPClient


async def main() -> None:
    client = MCPClient()
    await client.connect()
    try:
        ticket = await client.call_tool(
            "create_ticket",
            {"summary": "test ticket", "description": "smoke test", "component": "test-component"},
        )
        print("create_ticket:", ticket)
        ticket_id = ticket["id"]

        print("get_ticket:", await client.call_tool("get_ticket", {"ticket_id": ticket_id}))

        print(
            "update_ticket:",
            await client.call_tool("update_ticket", {"ticket_id": ticket_id, "status": "auto_approved"}),
        )

        print(
            "add_comment:",
            await client.call_tool("add_comment", {"ticket_id": ticket_id, "author": "smoke-test", "body": "hello"}),
        )

        listed = await client.call_tool("list_tickets", {})
        print(f"list_tickets: {len(listed)} ticket(s)")

        if llm_configured():
            print("search_similar_tickets:", await client.call_tool("search_similar_tickets", {"query": "outage", "k": 2}))
            print("search_runbooks:", await client.call_tool("search_runbooks", {"query": "severity policy", "k": 1}))
        else:
            print(
                "Azure AI Foundry not configured — skipping RAG search tool calls "
                "(embeddings require a real deployment, there is no offline fallback for those)."
            )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
