"""Device MCP Server (Python / FastMCP).

A FastMCP server for managing network devices (Cisco IOS, BDCOM, and any other
netmiko-supported platform) over SSH or Telnet. Exposes twelve tools:

    * connect_device
    * execute_command
    * configure_device
    * disconnect_device
    * list_connections
    * get_console_history
    * read_console_stream
    * get_help
    * transfer_file        - copy files over TFTP/FTP (config backup / image fetch)
    * upgrade_firmware      - download an image to flash and reboot into it
    * enter_monitor_mode    - drop into the bootloader 'monitor#' shell
    * recover_firmware      - end-to-end monitor-mode firmware recovery

Connections are addressed by host, plus a ``port`` that is required only when
several devices share an IP (a console/terminal server).
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from pydantic import Field

from .connection import DeviceConnectionManager

mcp = FastMCP("device-mcp")
_manager = DeviceConnectionManager()

# Reused field description: identifies which connection a tool acts on.
_PORT_DESC = (
    "Port of the target connection. Required only when several devices share an "
    "IP (a console/terminal server); omit it when the host has a single connection."
)


def _json(payload: object) -> str:
    return json.dumps(payload, indent=2, default=str)


@mcp.tool
def connect_device(
    host: Annotated[str, Field(description="IP address or hostname of the device")],
    username: Annotated[
        Optional[str],
        Field(description="Username for authentication (optional for unsecured consoles)"),
    ] = None,
    password: Annotated[
        Optional[str],
        Field(description="Password for authentication (optional for unsecured consoles)"),
    ] = None,
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
        Field(description="Enable password for privileged mode (optional; many "
              "devices need none)"),
    ] = None,
) -> str:
    """Connect to a network device via SSH or Telnet.

    Establishes a persistent connection for command execution. The connection is
    keyed by ``host:port``, so multiple devices behind one IP stay independent.
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
        Literal["auto", "user", "enable", "config"],
        Field(
            description="Execution mode: auto (default) runs at the current "
            "privilege level without downgrading; user/enable/config force that "
            "exact level. Use config for a single config command (prefer "
            "configure_device for multi-step config)."
        ),
    ] = "auto",
    expect_string: Annotated[
        Optional[str],
        Field(description="Regex pattern to expect for an interactive command "
              "confirmation, e.g. '[y/n]' or '\\(y/n\\)' (optional)"),
    ] = None,
    answer: Annotated[
        Optional[str],
        Field(description="Answer to send in response to the confirmation prompt "
              "(optional; pair with expect_string)"),
    ] = None,
    port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
    raw: Annotated[
        bool,
        Field(description="Bypass mode switching and prompt detection, driving the "
              "channel directly. Needed at prompts netmiko doesn't know, e.g. the "
              "bootloader 'monitor#' shell. With raw, 'mode' is ignored. Default false."),
    ] = False,
) -> str:
    """Execute a command on a connected network device.

    The device must be connected first using ``connect_device``. Returns the raw
    command output followed by a ``[device-mcp]`` footer line reporting any device
    error and where the CLI ended up (prompt + mode).
    """
    try:
        return _manager.execute_command(
            host, command, mode, expect_string, answer, port, raw
        )
    except Exception as exc:  # noqa: BLE001 - return AI-friendly error text
        return f"Error: {exc}"


@mcp.tool
def configure_device(
    host: Annotated[
        str, Field(description="IP address or hostname of the connected device")
    ],
    commands: Annotated[
        list[str],
        Field(
            description="Ordered configuration commands. They are sent as one block "
            "(config mode is entered/exited automatically). Send a sub-mode 'exit' "
            "as its own list item when moving between contexts, e.g. "
            '["vlan 30", "exit", "interface GigaEthernet0/1", "switchport mode '
            'access", "exit"]. A trailing ";" is not a command separator.'
        ),
    ],
    port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
) -> str:
    """Apply a sequence of configuration commands to a connected device.

    Enters config mode, sends each command in order (handling sub-mode prompt
    changes), then exits config mode. Returns the combined output plus a
    ``[device-mcp]`` footer; if a command is rejected the footer reports which one
    failed and where the session was left, instead of applying a partial config.
    """
    try:
        return _manager.configure(host, commands, port)
    except Exception as exc:  # noqa: BLE001 - return AI-friendly error text
        return f"Error: {exc}"


@mcp.tool
def disconnect_device(
    host: Annotated[
        str,
        Field(description="IP address or hostname of the device to disconnect"),
    ],
    port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
) -> str:
    """Disconnect from a network device and clean up the connection."""
    return _json(_manager.disconnect(host, port))


@mcp.tool
def list_connections() -> str:
    """List all active network device connections (with their host:port targets)."""
    return _json(_manager.list_connections())


@mcp.tool
def get_console_history(
    host: Annotated[
        str, Field(description="IP address or hostname of the connected device")
    ],
    limit: Annotated[
        int,
        Field(description="Number of most recent console lines to return (default 100)"),
    ] = 100,
    port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
) -> str:
    """Return the last N lines of raw console I/O captured for a connection.

    Useful for auditing logins, prompt-matching failures, and reboots. Note the
    history may contain sensitive output (e.g. plaintext credentials in a config).
    """
    try:
        return _manager.get_console_history(host, limit, port)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@mcp.tool
def read_console_stream(
    host: Annotated[
        str, Field(description="IP address or hostname of the connected device")
    ],
    expect_pattern: Annotated[
        Optional[str],
        Field(description="Regex to stop reading on (e.g. a login prompt). If "
              "omitted, reads until the timeout."),
    ] = None,
    timeout: Annotated[
        float,
        Field(description="Max seconds to read (capped at 120). Default 10."),
    ] = 10.0,
    port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
) -> str:
    """Read live console output without sending a command.

    Accumulates whatever the device emits until ``expect_pattern`` matches or the
    timeout elapses - handy for watching a reboot back to the login prompt.
    """
    try:
        return _manager.read_console_stream(host, expect_pattern, timeout, port)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@mcp.tool
def get_help(
    host: Annotated[
        str, Field(description="IP address or hostname of the connected device")
    ],
    command_prefix: Annotated[
        str,
        Field(description='Text to request help for, e.g. "sh" for possible commands, "show " for possible subcommands or "" for '
              "top-level help"),
    ] = "",
    port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
) -> str:
    """Send '?' inline help for a command prefix and return the device's options.

    Writes ``command_prefix + '?'`` without a newline (as the CLI expects), then
    clears the input line so the next command runs cleanly. Best effort.
    """
    try:
        return _manager.get_help(host, command_prefix, port)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


# @mcp.tool
# def transfer_file(
#     host: Annotated[
#         str, Field(description="IP address or hostname of the connected device")
#     ],
#     source: Annotated[
#         str,
#         Field(description='Copy source, e.g. "flash:startup-config", '
#               '"tftp:Rajibul/img.bin", or "ftp://user:pass@host/dir/file"'),
#     ],
#     destination: Annotated[
#         str,
#         Field(description='Copy destination, e.g. "flash:switch.bin" or '
#               '"tftp:backup/cfg"'),
#     ],
#     server: Annotated[
#         Optional[str],
#         Field(description="Trailing TFTP/FTP server IP that some BDCOM 'copy' forms "
#               "require, e.g. 170.170.170.170 (optional)"),
#     ] = None,
#     timeout: Annotated[
#         float,
#         Field(description="Max seconds to wait for the transfer (default 120)"),
#     ] = 120.0,
#     port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
# ) -> str:
#     """Copy a file to/from the device over TFTP/FTP (config backup or image fetch).

#     Runs BDCOM ``copy <source> <destination> [server]`` and returns the transfer
#     output plus a ``[device-mcp]`` footer reporting success/failure. Works in normal
#     CLI/enable mode and in the bootloader ``monitor`` shell. Note: a ``copy`` URL may
#     embed plaintext FTP credentials — treat the output as sensitive.
#     """
#     try:
#         return _manager.transfer_file(host, source, destination, server, timeout, port)
#     except Exception as exc:  # noqa: BLE001
#         return f"Error: {exc}"


# @mcp.tool
# def upgrade_firmware(
#     host: Annotated[
#         str, Field(description="IP address or hostname of the connected device")
#     ],
#     image_url: Annotated[
#         str,
#         Field(description='Firmware image source, e.g. "tftp:Rajibul/switch.bin" or '
#               '"ftp://user:pass@host/dir/img.bin"'),
#     ],
#     server: Annotated[
#         str,
#         Field(description="TFTP/FTP server IP appended to the 'copy' command, e.g. "
#               "170.170.170.170"),
#     ],
#     flash_name: Annotated[
#         str,
#         Field(description='Destination filename in flash (default "switch.bin")'),
#     ] = "switch.bin",
#     reboot: Annotated[
#         bool,
#         Field(description="Reboot into the new image after a successful transfer "
#               "(default true)"),
#     ] = True,
#     port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
# ) -> str:
#     """Download a firmware image to flash and (optionally) reboot into it.

#     Normal/enable-mode path: transfers the image to ``flash:<flash_name>``, requires a
#     ``successfully`` confirmation, then reboots answering the ``(y/n)`` prompt. Aborts
#     before rebooting if the transfer did not confirm. For a unit that can't boot far
#     enough to run this, use ``recover_firmware`` (monitor mode) instead.
#     """
#     try:
#         return _manager.upgrade_firmware(host, image_url, server, flash_name, reboot, port)
#     except Exception as exc:  # noqa: BLE001
#         return f"Error: {exc}"


@mcp.tool
def enter_monitor_mode(
    host: Annotated[
        str, Field(description="IP address or hostname of the connected device")
    ],
    timeout: Annotated[
        float,
        Field(description="Max seconds to reach the monitor prompt (default 180)"),
    ] = 180.0,
    port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
) -> str:
    """Drop the device into the bootloader ``monitor#`` shell.

    Two stages: (1) initiate a reboot — tries the hidden ``menu:`` reboot option
    first (works even at a login prompt), falling back to ``reboot``+``y``; (2) after
    ``RTC Test``, sends a short Ctrl-P burst to interrupt the boot into monitor mode.
    Returns the boot transcript plus a footer (``now: monitor#`` on success).
    """
    try:
        return _manager.enter_monitor_mode(host, timeout=timeout, port=port)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@mcp.tool
def recover_firmware(
    host: Annotated[
        str, Field(description="IP address or hostname of the connected device")
    ],
    image_url: Annotated[
        str,
        Field(description="Firmware image as a tftp: source — the bootloader monitor "
              "'copy' only accepts tftp: (an ftp:// URL is rejected) and caps the name "
              "at 60 chars. This fleet's relay shorthand is "
              "'tftp:f::<last-chars-of-ftp-dir>/<file>', e.g. "
              "'tftp:f::53/BD_3954_interAptiv_2.2.0F_154634.bin' for FTP dir "
              "/BDCOM0053/. Using 'f::' will relay the file from the FTP server and serve over TFTP."),
    ],
    server: Annotated[
        str, Field(description="TFTP (also FTP-relay) server IP appended to the 'copy' "
              "command")
    ],
    monitor_ip: Annotated[
        str,
        Field(description="IP to assign the unit in monitor mode so it can reach the "
              "TFTP/FTP-relay server, e.g. 170.170.170.183"),
    ],
    mask: Annotated[
        str, Field(description='Subnet mask for monitor_ip (default "255.255.255.0")'),
    ] = "255.255.255.0",
    flash_name: Annotated[
        str, Field(description='Destination filename in flash (default "switch.bin")'),
    ] = "switch.bin",
    port: Annotated[Optional[int], Field(description=_PORT_DESC)] = None,
) -> str:
    """Firmware recovery/upgrade via the bootloader ``monitor#`` shell.

    For a unit too broken to upgrade normally: enters monitor mode, assigns
    ``monitor_ip``, recovers the image to flash, and reboots into it. Aborts if
    monitor mode isn't reached or the flash transfer doesn't confirm.
    """
    try:
        return _manager.recover_firmware(
            host, image_url, server, monitor_ip, mask, flash_name, port
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


def main() -> None:
    """Entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
