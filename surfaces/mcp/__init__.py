"""Hippo as an MCP server."""

from surfaces.mcp.client import HippoClient, HippoError
from surfaces.mcp.server import TOOLS, build_server, main

__all__ = ["TOOLS", "HippoClient", "HippoError", "build_server", "main"]
