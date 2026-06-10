"""Persistent SSH/Telnet connection management for network devices.

Uses `netmiko <https://github.com/ktbyers/netmiko>`_ as the transport, which is
purpose-built for network devices and handles prompt detection, paging
("--More--"), and user/enable/config mode transitions. BDCOM devices are driven
by a small custom driver (see :mod:`device_mcp.bdcom`); Cisco IOS and any other
netmiko platform go through the standard dispatcher.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import netmiko
from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

from .bdcom import BdcomSSH, BdcomTelnet

Protocol = Literal["ssh", "telnet"]
Mode = Literal["user", "enable", "config"]

# Read timeouts (seconds) for command execution. Generous defaults so large
# outputs such as ``show running-config`` / ``show tech-support`` complete.
_EXEC_READ_TIMEOUT = 60.0
_CONFIG_READ_TIMEOUT = 60.0

# device_type values served by a custom driver class instead of netmiko's
# dispatcher. Maps key -> (ssh_class, telnet_class).
_CUSTOM_DRIVERS: dict[str, tuple[type, type]] = {
    "bdcom": (BdcomSSH, BdcomTelnet),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_platform(device_type: str, protocol: Protocol) -> tuple[str, Any]:
    """Resolve ``(device_type, protocol)`` to a connection strategy.

    Returns one of:
      * ``("class", <netmiko subclass>)``  - instantiate the class directly
      * ``("netmiko", <device_type str>)`` - pass to ``ConnectHandler``

    Raises :class:`ValueError` for an unknown netmiko ``device_type``. Pure and
    side-effect-free so it can be unit-tested without any network I/O.
    """
    key = device_type.lower()
    if key in _CUSTOM_DRIVERS:
        ssh_cls, telnet_cls = _CUSTOM_DRIVERS[key]
        return ("class", telnet_cls if protocol == "telnet" else ssh_cls)

    effective = device_type
    if protocol == "telnet" and not effective.endswith("_telnet"):
        effective = f"{effective}_telnet"
    if effective not in netmiko.platforms:
        raise ValueError(f"Unsupported device_type '{effective}'")
    return ("netmiko", effective)


@dataclass
class DeviceConnection:
    """A single live connection plus its metadata."""

    config: dict[str, Any]
    connection: Any  # netmiko BaseConnection (or a custom subclass)
    connected: bool = True
    connected_at: datetime = field(default_factory=_now)
    last_activity: datetime = field(default_factory=_now)
    current_mode: Mode = "user"


class DeviceConnectionManager:
    """Manage long-lived connections to multiple network devices, keyed by host."""

    def __init__(self) -> None:
        self._connections: dict[str, DeviceConnection] = {}
        # FastMCP may dispatch tool calls from multiple worker threads, so guard
        # the shared connection map and the (non-reentrant) netmiko channels.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ connect
    def connect(
        self,
        host: str,
        username: str,
        password: str,
        device_type: str = "cisco_ios",
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

            try:
                strategy, target = resolve_platform(device_type, protocol)
            except ValueError as exc:
                return self._failure(host, str(exc))

            params: dict[str, Any] = {
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
                if strategy == "class":
                    connection = target(**params)
                else:
                    connection = ConnectHandler(device_type=target, **params)
            except NetmikoAuthenticationException as exc:
                return self._failure(host, f"Authentication failed: {exc}")
            except NetmikoTimeoutException as exc:
                return self._failure(host, f"Connection timed out: {exc}")
            except Exception as exc:  # noqa: BLE001 - surface any transport error
                return self._failure(host, f"{type(exc).__name__}: {exc}")

            self._connections[host] = DeviceConnection(
                config={
                    "host": host,
                    "device_type": device_type,
                    "protocol": protocol,
                    "enable_password": enable_password,
                },
                connection=connection,
            )
            return {
                "success": True,
                "message": (
                    f"Successfully connected to {host} ({device_type}) "
                    f"via {protocol.upper()}"
                ),
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
    def _switch_mode(self, conn: DeviceConnection, target: Mode) -> None:
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
                    "device_type": conn.config.get("device_type"),
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
