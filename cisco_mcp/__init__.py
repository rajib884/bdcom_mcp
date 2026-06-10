"""Cisco MCP Server — Python / FastMCP implementation."""

from .connection import CiscoConnectionManager
from .server import main, mcp

__all__ = ["CiscoConnectionManager", "main", "mcp"]
__version__ = "1.0.0"
