"""Persistent SSH/Telnet connection management for network devices.

Uses `netmiko <https://github.com/ktbyers/netmiko>`_ as the transport, which is
purpose-built for network devices and handles prompt detection, paging
("--More--"), and user/enable/config mode transitions. BDCOM devices are driven
by a small custom driver (see :mod:`device_mcp.bdcom`); Cisco IOS and any other
netmiko platform go through the standard dispatcher.

Connections are keyed by ``host:port`` (a "target"), not by host alone, so that
several devices reachable behind a single console/terminal-server IP on different
ports stay independent.
"""

from __future__ import annotations

import io
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import netmiko
from netmiko import ConnectHandler
from netmiko.exceptions import (
    ConfigInvalidException,
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

from .bdcom import BdcomSSH, BdcomTelnet

Protocol = Literal["ssh", "telnet"]
# "auto" (the default) runs at the current privilege level without downgrading;
# user/enable/config force that exact level.
Mode = Literal["auto", "user", "enable", "config"]

# Read timeouts (seconds) for command execution. Generous defaults so large
# outputs such as ``show running-config`` / ``show tech-support`` complete.
_EXEC_READ_TIMEOUT = 60.0
_CONFIG_READ_TIMEOUT = 60.0

# Hard ceiling for read_console_stream so a tool call cannot block forever.
_MAX_STREAM_TIMEOUT = 120.0

# Default transport ports, used to resolve a target when no port is supplied.
_DEFAULT_PORTS: dict[str, int] = {"ssh": 22, "telnet": 23}

# device_type values served by a custom driver class instead of netmiko's
# dispatcher. Maps key -> (ssh_class, telnet_class).
_CUSTOM_DRIVERS: dict[str, tuple[type, type]] = {
    "bdcom": (BdcomSSH, BdcomTelnet),
}

# Prompts emitted by an initial setup wizard that we auto-decline (best effort).
_WIZARD_RE = re.compile(
    r"initial (?:configuration|config) dialog|\[yes/no\]|\[yes\]:", re.IGNORECASE
)

# Markers that mean the device *rejected* a command (vs a transport failure).
# Matched per line so a description containing one of these words is less likely
# to false-positive. Covers BDCOM and Cisco IOS phrasing.
_DEVICE_ERROR_RE = re.compile(
    r"(?im)^\s*(?:"
    r"%?\s*Unknown command"
    r"|%?\s*Incomplete command"
    r"|%?\s*Ambiguous command"
    r"|%?\s*Too many parameters"
    r"|%?\s*Parameter out of range"
    r"|%\s*Invalid input"
    r"|%\s*Bad "
    r"|%Err"
    r"|.*does not exist or does not have"
    r")"
)
# error_pattern handed to netmiko send_config_set so a bad config line raises.
_CONFIG_ERROR_RE = (
    r"(?i)(?:Unknown command|Incomplete command|Ambiguous command"
    r"|Too many parameters|Parameter out of range|Invalid input|%Err"
    r"|does not exist or does not have)"
)
_CARET_LINE_RE = re.compile(r"^(\s*)\^\s*$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _detect_device_error(output: str) -> Optional[dict[str, Any]]:
    """Find a device command-rejection in ``output``.

    Returns ``{"message": <error line>, "near": <token>|None}`` or ``None``. When
    the device prints a caret (``^``) line marking the bad token, the word above
    the caret column is reported as ``near``. Pure / offline-testable.
    """
    match = _DEVICE_ERROR_RE.search(output)
    if not match:
        return None
    message = match.group(0).strip()
    near = None
    lines = output.splitlines()
    for i, line in enumerate(lines):
        caret = _CARET_LINE_RE.match(line)
        if caret and i > 0:
            col = len(caret.group(1))
            prev = lines[i - 1]
            # Take the whitespace-delimited token spanning the caret column.
            start = prev.rfind(" ", 0, col + 1) + 1
            end = prev.find(" ", col)
            near = prev[start : end if end != -1 else len(prev)].strip() or None
            break
    return {"message": message, "near": near}


def _classify_prompt(prompt: str) -> str:
    """Classify a device prompt into user / enable / config. Pure."""
    p = prompt.strip()
    if p.endswith(">"):
        return "user"
    if "_config" in p or "(config" in p:
        return "config"
    if p.endswith("#"):
        return "enable"
    return "unknown"


def _describe_prompt(net: Any) -> tuple[Optional[str], str]:
    """Read the current prompt and classify it; best effort (does channel I/O)."""
    try:
        prompt = net.find_prompt()
    except Exception:  # noqa: BLE001
        return None, "unknown"
    return prompt, _classify_prompt(prompt)


def _parse_help_tokens(output: str) -> list[str]:
    """Parse next-token names from CLI '?' help lines (``token  -- description``)."""
    tokens = []
    for line in output.splitlines():
        m = re.match(r"\s*(\S+)\s+--\s+", line)
        if m:
            tokens.append(m.group(1))
    return tokens


def _footer(
    *,
    device_error: Optional[dict[str, Any]] = None,
    failure: Optional[str] = None,
    prompt: Optional[str] = None,
    mode: str = "unknown",
    options: Optional[list[str]] = None,
    note: Optional[str] = None,
) -> str:
    """Build the single-line ``[device-mcp]`` status footer appended to output."""
    where = f"now: {prompt} ({mode})" if prompt else f"now: unknown ({mode})"
    if failure:
        head = f"FAILED: {failure}"
    elif device_error:
        near = device_error.get("near")
        head = "device error: " + device_error["message"]
        if near:
            head += f" near '{near}'"
    elif note:
        head = note
    elif options is not None:
        head = "options: " + (", ".join(options) if options else "(none)")
    else:
        head = "ok"
    return f"[device-mcp] {head} | {where}"


def _target(host: str, port: int | None, protocol: Protocol = "ssh") -> str:
    """Resolve ``(host, port, protocol)`` to the canonical ``host:port`` key.

    When ``port`` is omitted, the protocol's default port (22 SSH / 23 telnet) is
    filled in. Pure and side-effect-free so it can be unit-tested offline.
    """
    if port is None:
        port = _DEFAULT_PORTS.get(protocol, 22)
    return f"{host}:{port}"


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


class _ConsoleRingLog(io.BufferedIOBase):
    """A bounded, in-memory session log for netmiko.

    netmiko's ``session_log`` accepts any :class:`io.BufferedIOBase`; it writes raw
    channel traffic (and, with ``session_log_record_writes``, our writes too) here
    as UTF-8 bytes. We keep only the last ``maxlen`` lines so the buffer can't grow
    without bound, then hand them back via :meth:`text` for console auditing.
    """

    def __init__(self, maxlen: int = 2000) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._partial = ""

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:  # type: ignore[override]
        text = (
            b.decode("utf-8", "replace")
            if isinstance(b, (bytes, bytearray))
            else str(b)
        )
        parts = (self._partial + text).split("\n")
        self._partial = parts.pop()
        self._lines.extend(parts)
        return len(b)

    def text(self, limit: int | None = 100) -> str:
        lines = list(self._lines)
        if self._partial:
            lines.append(self._partial)
        if limit is not None and limit >= 0:
            lines = lines[-limit:]
        return "\n".join(lines)


@dataclass
class DeviceConnection:
    """A single live connection plus its metadata."""

    config: dict[str, Any]
    connection: Any  # netmiko BaseConnection (or a custom subclass)
    console: Optional[_ConsoleRingLog] = None
    connected: bool = True
    connected_at: datetime = field(default_factory=_now)
    last_activity: datetime = field(default_factory=_now)
    current_mode: Mode = "user"


class DeviceConnectionManager:
    """Manage long-lived connections to multiple network devices, keyed by target.

    The target is ``host:port``; several devices behind one IP (a console server)
    on different ports are therefore independent connections.
    """

    def __init__(self) -> None:
        self._connections: dict[str, DeviceConnection] = {}
        # FastMCP may dispatch tool calls from multiple worker threads, so guard
        # the shared connection map and the (non-reentrant) netmiko channels.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ connect
    def connect(
        self,
        host: str,
        username: str | None = None,
        password: str | None = None,
        device_type: str = "cisco_ios",
        protocol: Protocol = "ssh",
        port: int | None = None,
        enable_password: str | None = None,
        auto_bypass_wizard: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            resolved_port = port if port is not None else _DEFAULT_PORTS.get(protocol, 22)
            target = f"{host}:{resolved_port}"

            existing = self._connections.get(target)
            if existing is not None and existing.connected:
                return {
                    "success": True,
                    "message": f"Already connected to {target} via {protocol.upper()}",
                    "host": host,
                    "port": resolved_port,
                    "target": target,
                }

            try:
                strategy, dispatch = resolve_platform(device_type, protocol)
            except ValueError as exc:
                return self._failure(host, str(exc), port=resolved_port)

            console = _ConsoleRingLog()
            params: dict[str, Any] = {
                "host": host,
                "port": resolved_port,
                "conn_timeout": 30,
                "session_log": console,
                "session_log_record_writes": True,
            }
            # username/password are optional: unsecured console/terminal-server
            # ports may need neither. Only pass what was supplied.
            if username is not None:
                params["username"] = username
            if password is not None:
                params["password"] = password
            if enable_password:
                params["secret"] = enable_password

            try:
                if strategy == "class":
                    # BaseConnection picks telnet vs SSH (and the default port)
                    # by checking for "_telnet" in the device_type string, not
                    # by which class is instantiated - it must be passed even
                    # when bypassing ConnectHandler's dispatcher.
                    class_device_type = device_type.lower()
                    if protocol == "telnet":
                        class_device_type = f"{class_device_type}_telnet"
                    connection = dispatch(device_type=class_device_type, **params)
                else:
                    connection = ConnectHandler(device_type=dispatch, **params)
            except NetmikoAuthenticationException as exc:
                return self._failure(host, f"Authentication failed: {exc}", port=resolved_port)
            except NetmikoTimeoutException as exc:
                return self._failure(host, f"Connection timed out: {exc}", port=resolved_port)
            except Exception as exc:  # noqa: BLE001 - surface any transport error
                return self._failure(host, f"{type(exc).__name__}: {exc}", port=resolved_port)

            if auto_bypass_wizard:
                self._maybe_bypass_wizard(connection)

            self._connections[target] = DeviceConnection(
                config={
                    "host": host,
                    "port": resolved_port,
                    "device_type": device_type,
                    "protocol": protocol,
                    "enable_password": enable_password,
                },
                connection=connection,
                console=console,
            )
            return {
                "success": True,
                "message": (
                    f"Successfully connected to {target} ({device_type}) "
                    f"via {protocol.upper()}"
                ),
                "host": host,
                "port": resolved_port,
                "target": target,
            }

    # ------------------------------------------------------------------ execute
    def execute_command(
        self,
        host: str,
        command: str,
        mode: Mode = "auto",
        expect_string: str | None = None,
        answer: str | None = None,
        port: int | None = None,
    ) -> str:
        """Run a command and return its raw output plus a ``[device-mcp]`` footer.

        The footer reports whether the device rejected the command (a *device
        error*, distinct from a transport failure) and where the CLI ended up
        (prompt + mode), so the caller can react without re-deriving session state.
        """
        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            if not conn.connected:
                raise RuntimeError(
                    f"No active connection to {host}. Please connect first."
                )

            net = conn.connection
            conn.last_activity = _now()
            try:
                self._switch_mode(conn, mode)
                if mode == "config":
                    # Prompt changes in config mode, so use timing-based reads.
                    output = net.send_command_timing(
                        command, read_timeout=_CONFIG_READ_TIMEOUT
                    )
                elif expect_string and answer is not None:
                    # Interactive confirmation (e.g. BDCOM "(y/n)"): send the
                    # command, wait for the prompt, then answer it.
                    output = net.send_command(
                        command,
                        expect_string=expect_string,
                        read_timeout=_EXEC_READ_TIMEOUT,
                        auto_find_prompt=False,
                    )
                    try:
                        output += "\n" + net.send_command(
                            answer, read_timeout=_EXEC_READ_TIMEOUT
                        )
                    except Exception as confirm_exc:  # noqa: BLE001
                        if "reboot" in command.lower() or "reload" in command.lower():
                            output += f"\n[Connection closed during reboot: {confirm_exc}]"
                        else:
                            raise
                else:
                    output = net.send_command(command, read_timeout=_EXEC_READ_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - report what broke + where we are
                prompt, where = _describe_prompt(net)
                return _footer(failure=f"{type(exc).__name__}: {exc}",
                               prompt=prompt, mode=where)

            prompt, where = _describe_prompt(net)
            conn.current_mode = where if where in ("user", "enable", "config") else conn.current_mode
            device_error = _detect_device_error(output)
            return output.rstrip("\n") + "\n\n" + _footer(
                device_error=device_error, prompt=prompt, mode=where
            )

    # ------------------------------------------------------------------ configure
    def configure(
        self, host: str, commands: list[str], port: int | None = None
    ) -> str:
        """Apply a sequence of config commands as one block; return output+footer.

        Uses netmiko ``send_config_set``: enters config mode, sends each command in
        order (tolerating submode prompt changes), then exits config mode. If a
        command is rejected the footer reports which one and where the session was
        left, instead of silently applying a partial/wrong config.
        """
        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            if not conn.connected:
                raise RuntimeError(
                    f"No active connection to {host}. Please connect first."
                )
            net = conn.connection
            conn.last_activity = _now()
            try:
                # BDCOM (and Cisco) only accept config from privileged mode;
                # send_config_set enters config but assumes enable first.
                if not net.check_enable_mode():
                    self._enter_enable(net)
                output = net.send_config_set(
                    commands,
                    error_pattern=_CONFIG_ERROR_RE,
                    read_timeout=_CONFIG_READ_TIMEOUT,
                )
            except ConfigInvalidException as exc:
                # send_config_set may strand the session in a submode on error.
                prompt, where = _describe_prompt(net)
                conn.current_mode = where if where in ("user", "enable", "config") else conn.current_mode
                return _footer(failure=str(exc), prompt=prompt, mode=where)
            except Exception as exc:  # noqa: BLE001
                prompt, where = _describe_prompt(net)
                return _footer(failure=f"{type(exc).__name__}: {exc}",
                               prompt=prompt, mode=where)

            conn.current_mode = "enable"  # send_config_set exits config mode
            prompt, where = _describe_prompt(net)
            conn.current_mode = where if where in ("user", "enable", "config") else conn.current_mode
            return output.rstrip("\n") + "\n\n" + _footer(
                note=f"applied {len(commands)} command(s)", prompt=prompt, mode=where
            )

    # ------------------------------------------------------------- diagnostics
    def get_console_history(
        self, host: str, limit: int = 100, port: int | None = None
    ) -> str:
        """Return the last ``limit`` lines of raw console I/O for a connection."""
        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            if conn.console is None:
                return ""
            return conn.console.text(limit)

    def read_console_stream(
        self,
        host: str,
        expect_pattern: str | None = None,
        timeout: float = 10.0,
        port: int | None = None,
    ) -> str:
        """Read live console output until ``expect_pattern`` matches or ``timeout``.

        Accumulates whatever the device emits without sending a command - handy for
        watching a reboot back to the login prompt. Partial output is always
        returned (even on timeout). Holds the manager lock for its duration.
        """
        timeout = max(0.0, min(float(timeout), _MAX_STREAM_TIMEOUT))
        pattern = re.compile(expect_pattern) if expect_pattern else None
        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            net = conn.connection
            conn.last_activity = _now()
            deadline = time.time() + timeout
            acc = ""
            while time.time() < deadline:
                chunk = net.read_channel()
                if chunk:
                    acc += chunk
                    if pattern and pattern.search(acc):
                        break
                else:
                    time.sleep(0.2)
            return acc

    def get_help(
        self, host: str, command_prefix: str = "", port: int | None = None
    ) -> str:
        """Send ``command_prefix + '?'`` and return the device's inline help.

        ``?`` triggers help without a newline and leaves the typed prefix sitting on
        the input line. If it is not removed it gets prepended to the next command
        (e.g. ``show int`` + ``show interface ?`` -> ``show intshow interface ?``).
        So after reading the help we backspace the prefix off the line. Ctrl-C /
        Ctrl-U are not reliable on BDCOM, so we use backspaces and stop at the bell
        (``\\x07``) the CLI emits once the line is empty.

        Returns the raw help text plus a ``[device-mcp]`` footer listing the parsed
        next-token options (or flagging an invalid prefix).
        """
        # Normalize: a manually appended "?" would double up, and multiple trailing
        # spaces return an empty result on BDCOM. Strip one trailing "?" and
        # collapse a trailing whitespace run to a single space (which preserves the
        # "list the next token" intent).
        prefix = command_prefix
        if prefix.endswith("?"):
            prefix = prefix[:-1]
        stripped = prefix.rstrip()
        if stripped != prefix:
            prefix = stripped + " "

        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            net = conn.connection
            conn.last_activity = _now()

            net.write_channel(prefix + "?")
            out = ""
            # Long help lists (e.g. "ip ?") can take a while; cap generously and
            # rely on a quiet period to detect the end.
            deadline = time.time() + 8.0
            empties = 0
            while time.time() < deadline and empties < 3:
                time.sleep(0.3)
                chunk = net.read_channel()
                if chunk:
                    out += chunk
                    empties = 0
                else:
                    empties += 1

            # Erase the prefix the device redrew on the line so it is not prepended
            # to whatever command runs next. Bound the loop to the prefix length
            # (plus margin) so a device that never bells can't spin.
            self._clear_input_line(net, len(prefix) + 8)
            prompt, where = _describe_prompt(net)

        device_error = _detect_device_error(out)
        if device_error:
            footer = _footer(device_error=device_error, prompt=prompt, mode=where)
        else:
            footer = _footer(options=_parse_help_tokens(out), prompt=prompt, mode=where)
        return out.rstrip("\n") + "\n\n" + footer

    @staticmethod
    def _clear_input_line(net: Any, max_chars: int) -> None:
        """Backspace the device's input line until it is empty.

        A Cisco-style CLI (including BDCOM) echoes a bell (``\\x07``) when backspace
        is pressed on an already-empty line. Starting from a line that holds the
        typed prefix, sending one backspace at a time erases exactly that text and
        stops at the single terminating bell - so there is no storm of bells on an
        empty line, and nothing is left to leak into the next command. ``max_chars``
        bounds the loop for a device that never bells.
        """
        try:
            for _ in range(max(0, max_chars)):
                net.write_channel("\x08")
                time.sleep(0.08)
                if "\x07" in net.read_channel():  # BEL: line is now empty
                    break
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass

    # ------------------------------------------------------------- mode switching
    def _switch_mode(self, conn: DeviceConnection, target: Mode) -> None:
        net = conn.connection

        if target == "auto":
            # Run at the current privilege level: de-nest config submodes but do
            # not downgrade. BDCOM lands in enable at connect (our driver), so
            # 'show' works here; a Cisco user-exec session stays user-exec.
            if net.check_config_mode():
                net.exit_config_mode()
            conn.current_mode = "enable" if net.check_enable_mode() else "user"

        elif target == "enable":
            if net.check_config_mode():
                net.exit_config_mode()
            if not net.check_enable_mode():
                self._enter_enable(net)
            conn.current_mode = "enable"

        elif target == "config":
            if not net.check_enable_mode():
                self._enter_enable(net)
            if not net.check_config_mode():
                net.config_mode()
            conn.current_mode = "config"

        else:  # user
            if net.check_config_mode():
                net.exit_config_mode()
            if net.check_enable_mode():
                net.exit_enable_mode()
            conn.current_mode = "user"

    @staticmethod
    def _enter_enable(net: Any) -> None:
        # netmiko sends the enable secret only when the device prints a password
        # prompt, so this works on devices with no enable password (e.g. BDCOM
        # 'aaa authentication enable default none'). Only a genuine failure
        # (wrong/absent password when one is required) raises.
        try:
            net.enable()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Could not enter privileged (enable) mode: {exc}. If this device "
                "requires an enable password, pass enable_password on connect."
            ) from exc

    # --------------------------------------------------------------- disconnect
    def disconnect(self, host: str, port: int | None = None) -> dict[str, Any]:
        with self._lock:
            try:
                key = self._resolve(host, port)
            except RuntimeError as exc:
                return self._failure(host, str(exc), port=port)
            conn = self._connections[key]
            try:
                conn.connection.disconnect()
            except Exception as exc:  # noqa: BLE001
                return self._failure(host, f"Error disconnecting from {key}: {exc}", port=port)
            finally:
                self._connections.pop(key, None)
            return {
                "success": True,
                "message": f"Successfully disconnected from {key}",
                "host": host,
                "port": conn.config.get("port"),
                "target": key,
            }

    # -------------------------------------------------------------------- listing
    def list_connections(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "target": target,
                    "host": conn.config.get("host"),
                    "port": conn.config.get("port"),
                    "device_type": conn.config.get("device_type"),
                    "protocol": conn.config.get("protocol"),
                    "connected": conn.connected,
                    "current_mode": conn.current_mode,
                    "connected_at": conn.connected_at.isoformat(),
                    "last_activity": conn.last_activity.isoformat(),
                }
                for target, conn in self._connections.items()
            ]

    def cleanup(self) -> None:
        """Close every active connection (best effort)."""
        with self._lock:
            for key, conn in list(self._connections.items()):
                try:
                    if conn.connection is not None:
                        conn.connection.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._connections.pop(key, None)

    # --------------------------------------------------------------------- helpers
    def _resolve(self, host: str, port: int | None) -> str:
        """Return the connection key for ``host`` (disambiguated by ``port``).

        Caller must hold ``self._lock``. With ``port`` given, the exact
        ``host:port`` key is required. Without it, a lone connection on ``host``
        is used; zero or several raise a helpful :class:`RuntimeError`.
        """
        if port is not None:
            key = f"{host}:{port}"
            if key in self._connections:
                return key
            raise RuntimeError(f"No active connection to {key}. Please connect first.")

        matches = [
            k for k, c in self._connections.items() if c.config.get("host") == host
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise RuntimeError(
                f"No active connection to {host}. Please connect first."
            )
        ports = ", ".join(str(self._connections[k].config.get("port")) for k in matches)
        raise RuntimeError(
            f"Multiple connections on {host} (ports: {ports}); specify port."
        )

    def _maybe_bypass_wizard(self, net: Any) -> None:
        """Best-effort: decline an initial setup dialog if one is waiting."""
        try:
            peek = net.read_channel()
        except Exception:  # noqa: BLE001
            return
        if peek and _WIZARD_RE.search(peek):
            try:
                net.write_channel("no" + getattr(net, "RETURN", "\n"))
                time.sleep(0.3)
                net.read_channel()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _failure(host: str, message: str, port: int | None = None) -> dict[str, Any]:
        return {"success": False, "message": message, "host": host, "port": port}
