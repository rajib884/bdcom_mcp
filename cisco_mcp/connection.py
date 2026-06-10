"""Persistent SSH/Telnet connection management for Cisco devices.

This is the Python port of the original TypeScript ``CiscoConnectionManager``.
It uses `netmiko <https://github.com/ktbyers/netmiko>`_ as the transport, which
is purpose-built for network devices and handles prompt detection, paging
("--More--"), and user/enable/config mode transitions reliably.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

Protocol = Literal["ssh", "telnet"]
Mode = Literal["user", "enable", "config"]

# Read timeouts (seconds) for command execution. Generous defaults so large
# outputs such as ``show running-config`` / ``show tech-support`` complete.
_EXEC_READ_TIMEOUT = 60.0
_CONFIG_READ_TIMEOUT = 60.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CiscoConnection:
    """A single live connection plus its metadata."""

    config: dict[str, Any]
    connection: Any  # netmiko BaseConnection
    connected: bool = True
    connected_at: datetime = field(default_factory=_now)
    last_activity: datetime = field(default_factory=_now)
    current_mode: Mode = "user"


class CiscoConnectionManager:
    """Manage long-lived connections to multiple Cisco devices, keyed by host."""

    def __init__(self) -> None:
        self._connections: dict[str, CiscoConnection] = {}
        # FastMCP may dispatch tool calls from multiple worker threads, so guard
        # the shared connection map and the (non-reentrant) netmiko channels.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ connect
    def connect(
        self,
        host: str,
        username: str,
        password: str,
        protocol: Protocol = "ssh",
        port: int | None = None,
        enable_password: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            existing = self._connections.get(host)
            if existing is not None and existing.connected:
                return {
                    "success": True,
                    "message": f"Already connected to {host} via {protocol.upper()}",
                    "host": host,
                }

            device_type = "cisco_ios" if protocol == "ssh" else "cisco_ios_telnet"
            params: dict[str, Any] = {
                "device_type": device_type,
                "host": host,
                "username": username,
                "password": password,
                "conn_timeout": 30,
            }
            if port is not None:
                params["port"] = port
            if enable_password:
                params["secret"] = enable_password

            try:
                connection = ConnectHandler(**params)
            except NetmikoAuthenticationException as exc:
                return self._failure(host, f"Authentication failed: {exc}")
            except NetmikoTimeoutException as exc:
                return self._failure(host, f"Connection timed out: {exc}")
            except Exception as exc:  # noqa: BLE001 - surface any transport error
                return self._failure(host, f"{type(exc).__name__}: {exc}")

            self._connections[host] = CiscoConnection(
                config={
                    "host": host,
                    "protocol": protocol,
                    "enable_password": enable_password,
                },
                connection=connection,
            )
            return {
                "success": True,
                "message": f"Successfully connected to {host} via {protocol.upper()}",
                "host": host,
            }

    # ------------------------------------------------------------------ execute
    def execute_command(self, host: str, command: str, mode: Mode = "user") -> str:
        with self._lock:
            conn = self._connections.get(host)
            if conn is None or not conn.connected:
                raise RuntimeError(
                    f"No active connection to {host}. Please connect first."
                )

            conn.last_activity = _now()
            try:
                self._switch_mode(conn, mode)
                net = conn.connection
                if mode == "config":
                    # Prompt changes in config mode, so use timing-based reads.
                    output = net.send_command_timing(
                        command, read_timeout=_CONFIG_READ_TIMEOUT
                    )
                else:
                    output = net.send_command(
                        command, read_timeout=_EXEC_READ_TIMEOUT
                    )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"Command execution failed: {exc}") from exc
            return output

    # ------------------------------------------------------------- mode switching
    def _switch_mode(self, conn: CiscoConnection, target: Mode) -> None:
        net = conn.connection
        enable_password = conn.config.get("enable_password")

        if target == "enable":
            if not enable_password:
                raise RuntimeError("Enable password required for privileged mode")
            if net.check_config_mode():
                net.exit_config_mode()
            if not net.check_enable_mode():
                net.enable()
            conn.current_mode = "enable"

        elif target == "config":
            if not net.check_enable_mode():
                if not enable_password:
                    raise RuntimeError("Enable password required for privileged mode")
                net.enable()
            if not net.check_config_mode():
                net.config_mode()
            conn.current_mode = "config"

        else:  # user
            if net.check_config_mode():
                net.exit_config_mode()
            if net.check_enable_mode():
                net.exit_enable_mode()
            conn.current_mode = "user"

    # --------------------------------------------------------------- disconnect
    def disconnect(self, host: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connections.get(host)
            if conn is None:
                return self._failure(host, f"No connection found for {host}")
            try:
                conn.connection.disconnect()
            except Exception as exc:  # noqa: BLE001
                return self._failure(host, f"Error disconnecting from {host}: {exc}")
            finally:
                self._connections.pop(host, None)
            return {
                "success": True,
                "message": f"Successfully disconnected from {host}",
                "host": host,
            }

    # -------------------------------------------------------------------- listing
    def list_connections(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "host": host,
                    "protocol": conn.config.get("protocol"),
                    "connected": conn.connected,
                    "current_mode": conn.current_mode,
                    "connected_at": conn.connected_at.isoformat(),
                    "last_activity": conn.last_activity.isoformat(),
                }
                for host, conn in self._connections.items()
            ]

    def cleanup(self) -> None:
        """Close every active connection (best effort)."""
        with self._lock:
            for host in list(self._connections):
                self.disconnect(host)

    # --------------------------------------------------------------------- helpers
    @staticmethod
    def _failure(host: str, message: str) -> dict[str, Any]:
        return {"success": False, "message": message, "host": host}
