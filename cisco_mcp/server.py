"""Cisco MCP Server (Python / FastMCP).

A FastMCP port of the original TypeScript Cisco MCP server. Exposes four tools
for managing Cisco routers, switches, and firewalls over SSH or Telnet:

    * connect_cisco_device
    * execute_cisco_command
    * disconnect_cisco_device
    * list_connections
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from pydantic import Field

from .connection import CiscoConnectionManager

mcp = FastMCP("cisco-mcp")
_manager = CiscoConnectionManager()


def _json(payload: object) -> str:
    return json.dumps(payload, indent=2, default=str)


@mcp.tool
def connect_cisco_device(
    host: Annotated[str, Field(description="IP address or hostname of the Cisco device")],
    username: Annotated[str, Field(description="Username for authentication")],
    password: Annotated[str, Field(description="Password for authentication")],
    protocol: Annotated[
        Literal["ssh", "telnet"],
        Field(description="Connection protocol (ssh or telnet)"),
    ] = "ssh",
    port: Annotated[
        Optional[int],
        Field(description="Port number (default: 22 for SSH, 23 for Telnet)"),
    ] = None,
    enable_password: Annotated[
        Optional[str],
        Field(description="Enable password for privileged mode (optional)"),
    ] = None,
) -> str:
    """Connect to a Cisco device via SSH or Telnet.

    Establishes a persistent connection for command execution.
    """
    result = _manager.connect(
        host=host,
        username=username,
        password=password,
        protocol=protocol,
        port=port,
        enable_password=enable_password,
    )
    return _json(result)


@mcp.tool
def execute_cisco_command(
    host: Annotated[
        str, Field(description="IP address or hostname of the connected Cisco device")
    ],
    command: Annotated[
        str,
        Field(
            description='Cisco command to execute (e.g., "show version", '
            '"show ip interface brief")'
        ),
    ],
    mode: Annotated[
        Literal["user", "enable", "config"],
        Field(
            description="Execution mode: user (default), enable (privileged), "
            "or config (configuration)"
        ),
    ] = "user",
) -> str:
    """Execute a command on a connected Cisco device.

    The device must be connected first using ``connect_cisco_device``.
    """
    try:
        return _manager.execute_command(host, command, mode)
    except Exception as exc:  # noqa: BLE001 - return AI-friendly error text
        return f"Error: {exc}"


@mcp.tool
def disconnect_cisco_device(
    host: Annotated[
        str,
        Field(description="IP address or hostname of the Cisco device to disconnect"),
    ],
) -> str:
    """Disconnect from a Cisco device and clean up the connection."""
    return _json(_manager.disconnect(host))


@mcp.tool
def list_connections() -> str:
    """List all active Cisco device connections."""
    return _json(_manager.list_connections())


def main() -> None:
    """Entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
