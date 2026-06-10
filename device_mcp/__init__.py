"""Device MCP Server - Python / FastMCP implementation."""

from .connection import DeviceConnectionManager
from .server import main, mcp

__all__ = ["DeviceConnectionManager", "main", "mcp"]
__version__ = "1.0.0"
