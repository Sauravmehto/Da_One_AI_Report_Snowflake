"""Agent loop: the layer that decides which engine capability to invoke.

In the MCP deployment this job belonged to Claude Desktop — it read the tool
descriptions and chose. Owning the frontend means owning that loop, so it
lives here: tool schemas, dispatch onto the (blocking) engine, and the
streaming conversation itself.
"""

from app.agent.tools import TOOL_DEFINITIONS, TOOL_NAMES, ToolContext, dispatch

__all__ = ["TOOL_DEFINITIONS", "TOOL_NAMES", "ToolContext", "dispatch"]
