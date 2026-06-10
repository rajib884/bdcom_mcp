"""Device MCP Server (Python / FastMCP).

A FastMCP server for managing network devices (Cisco IOS, BDCOM, and any other
netmiko-supported platform) over SSH or Telnet. Exposes four tools:

    * connect_device
    * execute_command
    * disconnect_device
    * list_connections
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from pydantic import Field

from .connection import DeviceConnectionManager

mcp = FastMCP("device-mcp")
_manager = DeviceConnectionManager()


def _json(payload: object) -> str:
    return json.dumps(payload, indent=2, default=str)


@mcp.tool
def connect_device(
    host: Annotated[str, Field(description="IP address or hostname of the device")],
    username: Annotated[str, Field(description="Username for authentication")],
    password: Annotated[str, Field(description="Password for authentication")],
    device_type: Annotated[
        str,
        Field(
            description="Platform driver: 'cisco_ios' (default), 'bdcom', or any "
            "netmiko device_type (e.g. cisco_xe, cisco_nxos, arista_eos). BDCOM "
            "switches must use 'bdcom'."
        ),
    ] = "cisco_ios",
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
    """Connect to a network device via SSH or Telnet.

    Establishes a persistent connection for command execution.
    """
    result = _manager.connect(
        host=host,
        username=username,
        password=password,
        device_type=device_type,
        protocol=protocol,
        port=port,
        enable_password=enable_password,
    )
    return _json(result)


@mcp.tool
def execute_command(
    host: Annotated[
        str, Field(description="IP address or hostname of the connected device")
    ],
    command: Annotated[
        str,
        Field(
            description='Command to execute (e.g., "show version", '
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
    """Execute a command on a connected network device.

    The device must be connected first using ``connect_device``.
    """
    try:
        return _manager.execute_command(host, command, mode)
    except Exception as exc:  # noqa: BLE001 - return AI-friendly error text
        return f"Error: {exc}"


@mcp.tool
def disconnect_device(
    host: Annotated[
        str,
        Field(description="IP address or hostname of the device to disconnect"),
    ],
) -> str:
    """Disconnect from a network device and clean up the connection."""
    return _json(_manager.disconnect(host))


@mcp.tool
def list_connections() -> str:
    """List all active network device connections."""
    return _json(_manager.list_connections())


def main() -> None:
    """Entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
