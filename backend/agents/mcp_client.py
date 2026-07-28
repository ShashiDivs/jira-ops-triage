"""Async MCP client used inside LangGraph nodes to call the mock-Jira + RAG tools
exposed by mcp_server/server.py over stdio.

Each call_tool() opens its own stdio subprocess + session rather than sharing one across
calls: LangGraph runs each node as a separate asyncio task, and anyio's cancel scopes (used
internally by the stdio transport) can't be entered in one task and exited in another — a
persistent shared connection intermittently breaks with "Attempted to exit cancel scope in a
different task than it was entered in" once a ticket reaches its second or third tool call.
"""
import json
import os
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _server_params() -> StdioServerParameters:
    # mcp's stdio_client only inherits a small safe-list of env vars by default (not
    # OPENAI_API_KEY, DATABASE_URL, etc.) unless env= is passed explicitly. Pass the parent
    # process's full environment through so the subprocess's own Settings() sees the same
    # config the parent does — this matters in Docker, where secrets arrive via injected env
    # vars rather than a .env file (which is deliberately excluded from the image).
    return StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], env=os.environ.copy())


def _extract_result(result: Any) -> Any:
    # Prefer structuredContent: it's built from the tool's actual return-type annotation, so
    # it's unambiguous for any list length. The unstructured `content` list is NOT safe to
    # parse generically for list-returning tools — FastMCP emits one text block per list item
    # rather than one block containing a JSON array, so a single-item list is indistinguishable
    # from a bare dict, and a multi-item list silently truncates to whichever block is read.
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]
    if structured is not None:
        return structured

    # Bare `dict` return types (e.g. create_ticket, update_ticket, add_comment) get no output
    # model in FastMCP, so structuredContent is None — fall back to the single content block.
    for block in getattr(result, "content", []):
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return None


class MCPToolError(RuntimeError):
    pass


class MCPClient:
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments or {})
                if result.isError:
                    raise MCPToolError(f"MCP tool '{name}' failed: {_extract_result(result)}")
                return _extract_result(result)


async def get_mcp_client() -> MCPClient:
    return MCPClient()


async def shutdown_mcp_client() -> None:
    pass
