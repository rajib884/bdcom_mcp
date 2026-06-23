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
import os
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
# user/enable/config force that exact level. "raw" drives the channel directly
# (no mode switching, no netmiko prompt detection) for prompts netmiko doesn't
# know - notably the bootloader "monitor#" shell.
Mode = Literal["auto", "user", "enable", "config", "raw"]

# Closed set of reportable session states for the footer's ``now:`` field. The
# first three are normal CLI modes; ``monitor`` is the bootloader recovery shell;
# the rest are abnormal/transition states. Reporting one of these instead of raw
# read-buffer text is what makes a desync actionable (see _classify_session).
SessionState = Literal[
    "user",
    "enable",
    "config",
    "monitor",
    "awaiting_login",
    "session_terminated",
    "disconnected",
    "unknown",
]

# Read timeouts (seconds) for command execution. Generous defaults so large
# outputs such as ``show running-config`` / ``show tech-support`` complete.
_EXEC_READ_TIMEOUT = 60.0
_CONFIG_READ_TIMEOUT = 60.0

# Hard ceiling for read_console_stream so a tool call cannot block forever.
_MAX_STREAM_TIMEOUT = 120.0
# How often an otherwise-idle connection is polled for unsolicited console output.
# The poller takes the manager lock non-blockingly, so active tool calls keep
# exclusive channel ownership.
_IDLE_LOG_POLL_INTERVAL = 0.25

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

# Patterns that mean the session is no longer at a usable CLI prompt. A trailing
# login prompt or an auth failure means the device dropped us back to the login
# (so the next command would be typed as a username); a "logged out" line is the
# event that caused it. Both are reported as a terminated session needing a
# reconnect, classified from the console buffer rather than guessed from a raw
# netmiko timeout (the desyncs seen throughout conn2.log).
_LOGIN_PROMPT_RE = re.compile(r"(?im)^\s*(?:Username|Login|Password)\s*:\s*$")
_AUTH_FAILED_RE = re.compile(r"(?i)Authentication failed")
_LOGGED_OUT_RE = re.compile(r"(?i)logged out|User .*? logged out")
# The BDCOM bootloader recovery shell prompt (e.g. "monitor#").
_MONITOR_PROMPT_RE = re.compile(r"(?im)^\s*monitor\s*#")

# Known Cisco-isms that BDCOM rejects (or, worse, mishandles). When a command is
# rejected or kills the session, the matching hint is surfaced in the footer so the
# caller doesn't have to know BDCOM's dialect. Each is justified by conn2.log, the
# README cheat-sheet, or the documented save-config/reboot issues.
_DIALECT_HINTS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)\bwrite\s+memory\b"),
        "BDCOM saves config with bare 'write' (or 'write all'); 'write memory' "
        "logs the session out.",
    ),
    (
        re.compile(r"(?i)\bswitchport\s+access\s+vlan\b"),
        "BDCOM has no 'switchport access vlan'; use 'switchport mode access' then "
        "'switchport pvid <id>'.",
    ),
    (
        re.compile(r"(?i)\bconfigure\s+terminal\b"),
        "BDCOM enters global config with 'config', not 'configure terminal'.",
    ),
    (
        re.compile(r"(?i)\bshow\s+vlan\s+brief\b"),
        "BDCOM uses 'show vlan' (no 'brief').",
    ),
    (
        re.compile(r"(?i)\bshow\s+interface\s+status\b"),
        "BDCOM has no 'show interface status'; use 'show ip interface brief'.",
    ),
    (
        re.compile(r"(?i)^\s*end\s*$"),
        "BDCOM leaves config with Ctrl-Z or 'exit', not 'end'.",
    ),
    (
        re.compile(r"(?i)^\s*disable\s*$"),
        "BDCOM leaves privileged mode with 'exit', not 'disable'.",
    ),
]

# Markers for the file-transfer and monitor-mode recovery flows (Workstream B).
# A redrawn CLI/monitor prompt (line ending in '#'/'>') marks "command finished";
# the device echoes the prompt mid-line on the command itself, so the trailing
# whitespace anchor only matches the *redrawn* prompt after the output.
_CLI_PROMPT_RE = re.compile(r"[>#]\s*$")
_CONFIRM_YN_RE = re.compile(r"\(y/n\)")
_TRANSFER_OK_RE = re.compile(r"(?i)\bsuccessfully\b")
# A copy streams a long run of '#' progress markers (so the redrawn prompt can't be
# the stop condition - a progress '#' would match it). Stop only on a real outcome:
# the success line, or a known failure phrase the device prints.
_TRANSFER_DONE_RE = re.compile(
    r"(?i)\bsuccessfully\b"
    r"|Parameter invalid"
    r"|file name is too long"
    r"|%Err"
    r"|Unknown command"
    r"|^\s*error,"
    r"|does not exist"
    r"|No such file"
    r"|operation failed",
    re.MULTILINE,
)
# BDCOM bootloader 'copy' rejects a tftp: source filename longer than this.
_TFTP_NAME_LIMIT = 60
_RTC_TEST_RE = re.compile(r"RTC Test")
# A hidden-menu line offering a reboot, e.g. "4   reboot" or
# "9   core dump and reboot"; group 1 is the single-keystroke option to send.
_MENU_REBOOT_RE = re.compile(r"(?im)^\s*([0-9A-Za-z]{1,2})\s+.*\breboot\b.*$")
# Ctrl-] opens the hidden boot/console menu: the device prints "menu:" then a list
# of single-keystroke options (so the option key is sent with no newline). Ctrl-P
# drops a booting BDCOM unit into the monitor shell.
_MENU_TRIGGER = "\x1d"
_MONITOR_INTERRUPT = "\x10"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dialect_hint(command: str) -> Optional[str]:
    """Return a Cisco->BDCOM hint for ``command`` if it is a known gotcha. Pure."""
    for pattern, hint in _DIALECT_HINTS:
        if pattern.search(command):
            return hint
    return None


def _open_audit_log(
    host: str, port: int, device_type: str, protocol: str, when: datetime
) -> tuple[Optional[Any], Optional[str]]:
    """Open a per-connection audit log file and return ``(handle, path)``.

    The full device I/O is teed here for later review. The directory is
    ``$DEVICE_MCP_LOG_DIR`` (default ``./logs``); the file is
    ``<host>_<port>_<YYYYmmddTHHMMSSZ>.log`` (Windows-safe). Best effort - returns
    ``(None, None)`` on any error so audit logging can never block a connection.
    """
    try:
        log_dir = os.environ.get("DEVICE_MCP_LOG_DIR", "logs")
        os.makedirs(log_dir, exist_ok=True)
        safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
        ts = when.strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(log_dir, f"{safe_host}_{port}_{ts}.log")
        handle = open(path, "a", encoding="utf-8")
        handle.write(
            f"# device-mcp session log - {host}:{port} ({device_type}/{protocol}) "
            f"opened {when.isoformat()}\n"
        )
        handle.flush()
        return handle, path
    except Exception:  # noqa: BLE001 - logging must never block connecting
        return None, None


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


def _friendly_subcontext(sub: str) -> str:
    """Render a config sub-mode token as a readable phrase (unknown tokens as-is).

    e.g. ``g0/1`` -> ``interface g0/1``, ``vlan10``/``v10`` -> ``vlan 10``,
    ``line`` -> ``line``, ``ospf 1`` -> ``ospf 1``.
    """
    s = sub.strip().rstrip("#> ")
    low = s.lower()
    if low == "line":
        return "line"
    if low.startswith("vlan") and low[4:].isdigit():
        return f"vlan {low[4:]}"
    if re.fullmatch(r"v\d+", low):
        return f"vlan {low[1:]}"
    # Interface tokens look like a short letter prefix then a number/slash
    # (g0/1, gi0/1, tg0/1, f0/1, te0/1, ...). Pure name+number -> interface.
    if re.fullmatch(r"[a-z]{1,3}\d[\d/.:]*", low):
        return f"interface {s}"
    return s


def _describe_mode(prompt: str) -> str:
    """Concise human label for a device prompt's mode/sub-mode. Pure.

    The device name is arbitrary (set by ``hostname``); the mode lives in the prompt
    suffix. Examples (not exhaustive)::

        <name>>                -> user                    (User EXEC)
        <name>#                -> enable                  (Privileged EXEC)
        <name>_config#         -> config                  (global config)
        <name>_config_g0/1#    -> config: interface g0/1
        <name>_config_vlan10#  -> config: vlan 10
        <name>_config_line#    -> config: line
        monitor#               -> monitor                 (BootROM image recovery)

    Any other ``_config_<x>`` (BDCOM) or ``(config-<x>)`` (Cisco) sub-mode is reported
    as ``config: <x>``, so an unlisted feature sub-mode is still described, not lost.
    """
    p = prompt.strip()
    if _MONITOR_PROMPT_RE.search(p):
        return "monitor"
    # Cisco style: "name(config)#" / "name(config-if)#".
    m = re.search(r"\(config(?:-(?P<sub>[^)]+))?\)", p)
    if m:
        sub = m.group("sub")
        return f"config: {_friendly_subcontext(sub)}" if sub else "config"
    # BDCOM style: "name_config#" / "name_config_g0/1#".
    m = re.search(r"_config(?:_(?P<sub>.+?))?[#>]?\s*$", p)
    if m:
        sub = m.group("sub")
        return f"config: {_friendly_subcontext(sub)}" if sub else "config"
    if p.endswith(">"):
        return "user"
    if p.endswith("#"):
        return "enable"
    return "unknown"


def _classify_session(text: str) -> SessionState:
    """Classify session state from a prompt string or recent console text. Pure.

    Recognizes the abnormal states (login prompt / auth failure / logout / monitor
    shell) that a bare prompt classifier would mislabel as ``unknown`` and leak as
    raw text, then falls back to :func:`_classify_prompt` on the last line for the
    normal user/enable/config modes.
    """
    if not text or not text.strip():
        return "unknown"
    if _LOGGED_OUT_RE.search(text):
        return "session_terminated"
    if _LOGIN_PROMPT_RE.search(text) or _AUTH_FAILED_RE.search(text):
        return "awaiting_login"
    if _MONITOR_PROMPT_RE.search(text):
        return "monitor"
    last = text.strip().splitlines()[-1]
    classified = _classify_prompt(last)
    return classified  # type: ignore[return-value]  # one of user/enable/config/unknown


def _describe_session(
    net: Any, console: Optional["_ConsoleRingLog"] = None
) -> tuple[Optional[str], SessionState]:
    """Return ``(prompt, state)`` for the footer, never leaking raw buffer text.

    Reads the current prompt straight from the console ring - the device already
    echoed it as the command's read completed - instead of issuing a fresh
    ``find_prompt()``. That probe writes a bare carriage return the device echoes
    back, and netmiko retries it up to a dozen times when a prompt is slow, which
    is the dominant source of blank-line/duplicate-prompt noise in the log. Only
    when the ring is unavailable or its tail is inconclusive do we actively probe.
    """
    if console is not None:
        tail = console.text(20)
        stripped = tail.strip()
        last = stripped.splitlines()[-1].strip() if stripped else ""
        if last:
            # A clean CLI/monitor prompt on the last line means we are sitting at a
            # working prompt now; any login banner higher up the tail is history.
            if _MONITOR_PROMPT_RE.search(last):
                return last, "monitor"
            mode = _classify_prompt(last)
            if mode in ("user", "enable", "config"):
                return last, mode  # type: ignore[return-value]
        # Last line is not a normal prompt: scan the recent tail for the abnormal
        # states (login / logout / monitor) a bare prompt classifier would miss.
        scanned = _classify_session(tail)
        if scanned in ("awaiting_login", "session_terminated"):
            return None, scanned
        if scanned == "monitor":
            return last or None, scanned
        # else: inconclusive - fall through to an active probe as a last resort.
    try:
        prompt: Optional[str] = net.find_prompt()
    except Exception:  # noqa: BLE001
        prompt = None
    if prompt:
        state = _classify_session(prompt)
        if state in ("user", "enable", "config", "monitor"):
            return prompt, state
        if state != "unknown":
            # A login/auth/logout prompt: report the state, never the raw text.
            return None, state
    return None, "unknown"


def _read_until(
    net: Any,
    pattern: Optional[re.Pattern[str]] = None,
    *,
    timeout: float,
    idle: float = 0.5,
) -> str:
    """Accumulate channel output until ``pattern`` matches or ``timeout`` elapses.

    With no ``pattern`` it returns once the channel has been quiet for ``idle``
    seconds (after at least some data) or the timeout is hit. Always returns whatever
    was read (partial output on timeout). Does raw channel I/O; the caller holds the
    manager lock. Read exceptions propagate so the caller can distinguish a dropped
    session (e.g. a reboot) from a clean quiet period.
    """
    acc = ""
    start = time.time()
    last_data = start
    while time.time() - start < timeout:
        chunk = net.read_channel()
        if chunk:
            acc += chunk
            last_data = time.time()
            if pattern is not None and pattern.search(acc):
                break
        else:
            if pattern is None and acc and (time.time() - last_data) >= idle:
                break
            time.sleep(0.1)
    return acc


def _send_and_read(
    net: Any,
    text: str,
    pattern: Optional[re.Pattern[str]] = None,
    *,
    timeout: float,
    idle: float = 0.5,
    newline: bool = True,
) -> str:
    """Write ``text`` (with the channel's RETURN unless ``newline`` is False) and
    read the response via :func:`_read_until`."""
    data = text + (getattr(net, "RETURN", "\n") if newline else "")
    net.write_channel(data)
    return _read_until(net, pattern, timeout=timeout, idle=idle)


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
    terminated: bool = False,
    prompt: Optional[str] = None,
    state: str = "unknown",
    options: Optional[list[str]] = None,
    note: Optional[str] = None,
    hint: Optional[str] = None,
) -> str:
    """Build the single-line ``[device-mcp]`` status footer appended to output.

    The ``now:`` field shows the live prompt and a parsed mode label for a recognized
    CLI/monitor mode - including the config *sub-mode* (e.g. ``(config: interface
    g0/1)``) so the caller knows exactly where the CLI is. Any abnormal state is
    reported by name (e.g. ``now: awaiting_login``) so leftover read-buffer text can
    never leak in as a bogus "mode". An optional ``hint`` (a Cisco->BDCOM gotcha) is
    appended after the head.
    """
    if prompt and state in ("user", "enable", "config", "monitor"):
        where = f"now: {prompt} ({_describe_mode(prompt)})"
    else:
        where = f"now: {state}"
    if terminated:
        head = (
            "SESSION_TERMINATED: device returned to login prompt — reconnect required"
        )
    elif failure:
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
    if hint:
        head += f" | hint: {hint}"
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
    """A bounded, in-memory session log for netmiko, optionally teed to a file.

    netmiko's ``session_log`` accepts any :class:`io.BufferedIOBase`; it writes raw
    channel reads here as UTF-8 bytes (we leave ``session_log_record_writes`` off so
    echoed input isn't logged twice). We keep only the last ``maxlen`` lines so the
    buffer can't grow
    without bound, then hand them back via :meth:`text` for console auditing. When a
    ``file`` is given, the same text is also appended to it (unbounded) as a durable
    per-connection audit log; :meth:`close` flushes and closes that file.
    """

    def __init__(self, maxlen: int = 2000, file: Optional[Any] = None) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._partial = ""
        self._file = file

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
        if self._file is not None:
            try:
                self._file.write(text)
                self._file.flush()
            except Exception:  # noqa: BLE001 - never let audit logging break I/O
                pass
        return len(b)

    def text(self, limit: int | None = 100) -> str:
        lines = list(self._lines)
        if self._partial:
            lines.append(self._partial)
        if limit is not None and limit >= 0:
            lines = lines[-limit:]
        return "\n".join(lines)

    def close(self) -> None:  # type: ignore[override]
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except Exception:  # noqa: BLE001
                pass
            self._file = None
        super().close()


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
    log_path: Optional[str] = None
    idle_log_stop: threading.Event | None = None
    idle_log_thread: threading.Thread | None = None


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

            opened = _now()
            log_file, log_path = _open_audit_log(
                host, resolved_port, device_type, protocol, opened
            )
            console = _ConsoleRingLog(file=log_file)
            params: dict[str, Any] = {
                "host": host,
                "port": resolved_port,
                "conn_timeout": 30,
                "session_log": console,
                # Don't record our channel writes: these CLIs echo typed input, so
                # netmiko's read capture already contains every command (prefixed by
                # the live prompt, e.g. "Switch#show version"). Recording writes too
                # logged each command a second time as a bare, prompt-less line. The
                # only writes this drops from the audit log are non-echoed ones -
                # credentials (already masked) and raw control bytes (Ctrl-Z/Ctrl-P).
                "session_log_record_writes": False,
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
                # Surface what the device actually sent (the login banner/prompts) so
                # the caller can tell a bad password from a banner-only or
                # specific-account login, instead of a bare "Login failed".
                transcript = console.text(20).strip()
                console.close()  # flush+close the audit log (keeps the transcript)
                msg = f"Authentication failed: {exc}"
                if transcript:
                    msg += f"\n--- last console output ---\n{transcript}"
                return self._failure(host, msg, port=resolved_port)
            except NetmikoTimeoutException as exc:
                transcript = console.text(20).strip()
                console.close()
                msg = f"Connection timed out: {exc}"
                if transcript:
                    msg += f"\n--- last console output ---\n{transcript}"
                return self._failure(host, msg, port=resolved_port)
            except Exception as exc:  # noqa: BLE001 - surface any transport error
                console.close()
                return self._failure(host, f"{type(exc).__name__}: {exc}", port=resolved_port)

            if auto_bypass_wizard:
                self._maybe_bypass_wizard(connection)

            managed = DeviceConnection(
                config={
                    "host": host,
                    "port": resolved_port,
                    "device_type": device_type,
                    "protocol": protocol,
                    "enable_password": enable_password,
                },
                connection=connection,
                console=console,
                connected_at=opened,
                log_path=log_path,
            )
            self._connections[target] = managed
            self._start_idle_logger(target, managed)
            return {
                "success": True,
                "message": (
                    f"Successfully connected to {target} ({device_type}) "
                    f"via {protocol.upper()}"
                ),
                "host": host,
                "port": resolved_port,
                "target": target,
                "log_file": log_path,
            }

    def _start_idle_logger(self, target: str, conn: DeviceConnection) -> None:
        """Poll a quiet connection so unsolicited console output reaches the log.

        Netmiko writes to ``session_log`` only when someone reads/writes the channel.
        Without this poller, device output emitted while the MCP server is idle stays
        in the transport buffer and never reaches the durable audit file.
        """
        stop = threading.Event()
        conn.idle_log_stop = stop
        thread = threading.Thread(
            target=self._idle_log_loop,
            args=(target, stop),
            name=f"device-mcp-idle-log-{target}",
            daemon=True,
        )
        conn.idle_log_thread = thread
        thread.start()

    def _idle_log_loop(self, target: str, stop: threading.Event) -> None:
        """Best-effort idle drain for unsolicited device output."""
        while not stop.wait(_IDLE_LOG_POLL_INTERVAL):
            acquired = self._lock.acquire(blocking=False)
            if not acquired:
                continue
            try:
                conn = self._connections.get(target)
                if conn is None or not conn.connected:
                    return
                try:
                    chunk = conn.connection.read_channel()
                except Exception as exc:  # noqa: BLE001 - idle logging is best effort
                    if conn.console is not None:
                        conn.console.write(
                            f"\n[device-mcp] idle log stopped: "
                            f"{type(exc).__name__}: {exc}\n"
                        )
                    return
                if chunk:
                    # Netmiko's read_channel() has already written this data to the
                    # session_log, which is our _ConsoleRingLog. Do not write it here
                    # too, or the audit file will contain duplicates.
                    conn.last_activity = _now()
            finally:
                self._lock.release()

    # ------------------------------------------------------------------ execute
    def run_commands(
        self,
        host: str,
        commands: list[str],
        mode: Mode = "auto",
        expect_regex: str | None = None,
        answer: str | None = None,
        port: int | None = None,
    ) -> str:
        """Run one or more commands and return their combined output + footers.

        ``mode="config"`` applies the whole list atomically (``configure``); any other
        mode runs the commands sequentially via :meth:`execute_command`. An interactive
        ``expect_regex``/``answer`` is honored only for a lone command (a ``(y/n)``
        confirm is inherently single-command). This is the single entry point the
        ``execute_command`` tool exposes.
        """
        if not commands:
            raise RuntimeError("'commands' must contain at least one command.")
        if mode == "config":
            return self.configure(host, commands, port)
        single = len(commands) == 1
        results = [
            self.execute_command(
                host,
                cmd,
                mode,
                expect_regex if single else None,
                answer if single else None,
                port,
            )
            for cmd in commands
        ]
        return "\n\n".join(results)

    def execute_command(
        self,
        host: str,
        command: str,
        mode: Mode = "auto",
        expect_regex: str | None = None,
        answer: str | None = None,
        port: int | None = None,
    ) -> str:
        """Run a command and return its raw output plus a ``[device-mcp]`` footer.

        The footer reports whether the device rejected the command (a *device
        error*, distinct from a transport failure) and where the CLI ended up
        (prompt + mode), so the caller can react without re-deriving session state.

        ``mode="raw"`` bypasses mode switching and netmiko's prompt detection and
        drives the channel directly - required at prompts netmiko doesn't know, e.g.
        the bootloader ``monitor#`` shell, where the normal path waits forever for the
        device's usual ``Switch.*`` prompt and the command is never even sent. With
        ``raw`` pass ``expect_regex`` to stop on a pattern (and ``answer`` to reply to
        a confirmation).
        """
        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            if not conn.connected:
                raise RuntimeError(
                    f"No active connection to {host}. Please connect first."
                )

            net = conn.connection
            conn.last_activity = _now()
            if mode == "raw":
                return self._execute_raw(conn, command, expect_regex, answer)
            try:
                self._switch_mode(conn, mode)
                if mode == "config":
                    # Prompt changes in config mode, so use timing-based reads.
                    output = net.send_command_timing(
                        command, read_timeout=_CONFIG_READ_TIMEOUT
                    )
                elif expect_regex and answer is not None:
                    # Interactive confirmation (e.g. BDCOM "(y/n)"): send the
                    # command, wait for the prompt, then answer it. Capture the
                    # answer's full aftermath (e.g. the "System is rebooting" banner)
                    # via a raw drain instead of a second send_command, which would
                    # wait for a clean prompt that a reboot never returns - leaving a
                    # misleadingly empty result.
                    output = net.send_command(
                        command,
                        expect_string=expect_regex,
                        read_timeout=_EXEC_READ_TIMEOUT,
                        auto_find_prompt=False,
                    )
                    try:
                        net.write_channel(answer + getattr(net, "RETURN", "\n"))
                        output += "\n" + _read_until(net, timeout=8.0, idle=1.5)
                    except Exception as confirm_exc:  # noqa: BLE001
                        if "reboot" in command.lower() or "reload" in command.lower():
                            output += f"\n[Connection closed during reboot: {confirm_exc}]"
                        else:
                            raise
                else:
                    output = net.send_command(command, read_timeout=_EXEC_READ_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - report what broke + where we are
                prompt, state = _describe_session(net, conn.console)
                if state in ("awaiting_login", "session_terminated"):
                    return _footer(terminated=True, prompt=prompt, state=state,
                                   hint=_dialect_hint(command))
                return _footer(failure=f"{type(exc).__name__}: {exc}",
                               prompt=prompt, state=state, hint=_dialect_hint(command))

            prompt, state = _describe_session(net, conn.console)
            conn.current_mode = state if state in ("user", "enable", "config") else conn.current_mode
            if state in ("awaiting_login", "session_terminated"):
                # A command that succeeded on the wire but dropped us to login
                # (e.g. 'write memory' on BDCOM) is a terminated session, not "ok".
                return output.rstrip("\n") + "\n\n" + _footer(
                    terminated=True, prompt=prompt, state=state,
                    hint=_dialect_hint(command),
                )
            device_error = _detect_device_error(output)
            return output.rstrip("\n") + "\n\n" + _footer(
                device_error=device_error, prompt=prompt, state=state,
                hint=_dialect_hint(command) if device_error else None,
            )

    def _execute_raw(
        self,
        conn: DeviceConnection,
        command: str,
        expect_regex: str | None,
        answer: str | None,
    ) -> str:
        """Send ``command`` with raw channel I/O - no mode switching, no reliance on
        netmiko's cached prompt. Caller holds the lock. Used for the ``monitor#`` shell
        (and any prompt netmiko can't match), e.g. the final reboot after a recovery
        flash."""
        net = conn.connection
        try:
            try:
                net.read_channel()  # drain residue
            except Exception:  # noqa: BLE001
                pass
            pat = re.compile(expect_regex) if expect_regex else None
            output = _send_and_read(net, command, pat, timeout=_EXEC_READ_TIMEOUT, idle=2.0)
            if expect_regex and answer is not None:
                # Answer the confirmation, then drain the aftermath (e.g. the reboot
                # banner) - the device may not return a clean prompt.
                output += "\n" + _send_and_read(net, answer, None, timeout=8.0, idle=1.5)
        except Exception as exc:  # noqa: BLE001
            prompt, state = _describe_session(net, conn.console)
            return _footer(failure=f"{type(exc).__name__}: {exc}",
                           prompt=prompt, state=state, hint=_dialect_hint(command))
        prompt, state = _describe_session(net, conn.console)
        conn.current_mode = state if state in ("user", "enable", "config") else conn.current_mode
        if state in ("awaiting_login", "session_terminated"):
            return output.rstrip("\n") + "\n\n" + _footer(
                terminated=True, prompt=prompt, state=state, hint=_dialect_hint(command))
        device_error = _detect_device_error(output)
        return output.rstrip("\n") + "\n\n" + _footer(
            device_error=device_error, prompt=prompt, state=state,
            hint=_dialect_hint(command) if device_error else None)

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
                prompt, state = _describe_session(net, conn.console)
                conn.current_mode = state if state in ("user", "enable", "config") else conn.current_mode
                hint = next((_dialect_hint(c) for c in commands if _dialect_hint(c)), None)
                if state in ("awaiting_login", "session_terminated"):
                    return _footer(terminated=True, prompt=prompt, state=state, hint=hint)
                return _footer(failure=str(exc), prompt=prompt, state=state, hint=hint)
            except Exception as exc:  # noqa: BLE001
                prompt, state = _describe_session(net, conn.console)
                hint = next((_dialect_hint(c) for c in commands if _dialect_hint(c)), None)
                if state in ("awaiting_login", "session_terminated"):
                    return _footer(terminated=True, prompt=prompt, state=state, hint=hint)
                return _footer(failure=f"{type(exc).__name__}: {exc}",
                               prompt=prompt, state=state, hint=hint)

            conn.current_mode = "enable"  # send_config_set exits config mode
            prompt, state = _describe_session(net, conn.console)
            conn.current_mode = state if state in ("user", "enable", "config") else conn.current_mode
            if state in ("awaiting_login", "session_terminated"):
                return output.rstrip("\n") + "\n\n" + _footer(
                    terminated=True, prompt=prompt, state=state,
                    hint=next((_dialect_hint(c) for c in commands if _dialect_hint(c)), None),
                )
            return output.rstrip("\n") + "\n\n" + _footer(
                note=f"applied {len(commands)} command(s)", prompt=prompt, state=state
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

            # Capture the base prompt first: after listing help, the device redraws
            # exactly this prompt (ending '#' in enable/config, '>' in user mode)
            # followed by our prefix, which is our "help complete" marker.
            try:
                base_prompt = net.find_prompt()
            except Exception:  # noqa: BLE001
                base_prompt = None

            # Drain any stale buffer (e.g. a previous command's tail) so it can't
            # contaminate the help text or the redrawn-prompt detection.
            try:
                net.read_channel()
            except Exception:  # noqa: BLE001
                pass

            net.write_channel(prefix + "?")
            # Read until the base prompt is redrawn after the help body. Match the
            # actual prompt string so a '#'/'>' inside a help description can't stop
            # us early; fall back to a quiet period when the prompt is unknown.
            # Long lists (e.g. "ip ?") can take a while, so the timeout is generous.
            marker = (
                re.compile(re.escape(base_prompt.strip())) if base_prompt else None
            )
            out = _read_until(net, marker, timeout=15.0, idle=2.0)

            # Erase the prefix the device redrew on the line so it is not prepended
            # to whatever command runs next. Bound the loop to the prefix length
            # (plus margin) so a device that never bells can't spin.
            self._clear_input_line(net, len(prefix) + 8)
            prompt, state = _describe_session(net, conn.console)

        device_error = _detect_device_error(out)
        if device_error:
            footer = _footer(device_error=device_error, prompt=prompt, state=state)
        else:
            footer = _footer(options=_parse_help_tokens(out), prompt=prompt, state=state)
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

    # ----------------------------------------------- file transfer / firmware
    def transfer_file(
        self,
        host: str,
        source: str,
        destination: str,
        server: str | None = None,
        timeout: float = 300.0,
        port: int | None = None,
    ) -> str:
        """Run a BDCOM ``copy`` and report whether the transfer succeeded.

        Builds ``copy <source> <destination> [server]`` and reads until the device
        reports an outcome (``successfully`` or a failure phrase) or the timeout. The
        copy streams a long run of ``#`` progress markers for a large image, so the
        redrawn prompt is *not* the stop condition - waiting on it returns mid-stream
        and misreports a good transfer as failed. ``flash:`` paths and ``tftp:``
        sources pass through unchanged; note the bootloader ``monitor`` ``copy`` only
        accepts ``tftp:`` and limits the source name to 60 chars. Mirrors the
        ``download_configs.py`` / ``tftp_script.py`` copy step.
        """
        # Fail fast on the device's documented 60-char tftp source-name limit
        # instead of rebooting first and letting the device reject the command.
        if source.lower().startswith("tftp:"):
            name = source[len("tftp:"):]
            if len(name) > _TFTP_NAME_LIMIT:
                return _footer(
                    failure=f"tftp source name is {len(name)} chars; this device's "
                    f"bootloader 'copy' allows at most {_TFTP_NAME_LIMIT}",
                    state="unknown",
                )

        cmd = f"copy {source} {destination}"
        if server:
            cmd += f" {server}"
        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            if not conn.connected:
                raise RuntimeError(
                    f"No active connection to {host}. Please connect first."
                )
            net = conn.connection
            conn.last_activity = _now()
            try:
                try:
                    net.read_channel()  # drain residue before issuing the copy
                except Exception:  # noqa: BLE001
                    pass
                out = _send_and_read(net, cmd, _TRANSFER_DONE_RE, timeout=timeout)
                # Large images stream for a while; if the first wait returned without
                # a verdict, poll once more before declaring failure (issue #3).
                if not _TRANSFER_DONE_RE.search(out):
                    out += _read_until(
                        net, _TRANSFER_DONE_RE, timeout=min(120.0, timeout)
                    )
            except Exception as exc:  # noqa: BLE001
                prompt, state = _describe_session(net, conn.console)
                return _footer(failure=f"{type(exc).__name__}: {exc}",
                               prompt=prompt, state=state)
            prompt, state = _describe_session(net, conn.console)

        device_error = _detect_device_error(out)
        if _TRANSFER_OK_RE.search(out):
            footer = _footer(note="transfer ok", prompt=prompt, state=state)
        elif device_error:
            footer = _footer(device_error=device_error, prompt=prompt, state=state)
        elif _TRANSFER_DONE_RE.search(out):
            failed = _TRANSFER_DONE_RE.search(out).group(0).strip()
            footer = _footer(failure=f"transfer rejected: {failed}",
                             prompt=prompt, state=state)
        else:
            footer = _footer(
                failure="transfer did not confirm success (timed out waiting "
                "for the device's confirmation)",
                prompt=prompt, state=state,
            )
        return out.rstrip("\n") + "\n\n" + footer

    def upgrade_firmware(
        self,
        host: str,
        image_url: str,
        server: str,
        flash_name: str = "switch.bin",
        reboot: bool = True,
        port: int | None = None,
    ) -> str:
        """Download a firmware image to flash and (optionally) reboot into it.

        Normal/enable-mode path (from ``tftp_script.py``): ``transfer_file`` the image
        to ``flash:<flash_name>``, require a ``successfully`` confirmation, then
        ``reboot`` answering the ``(y/n)`` prompt. Aborts before rebooting if the
        transfer did not confirm success.
        """
        transfer = self.transfer_file(
            host, image_url, f"flash:{flash_name}", server, timeout=600.0, port=port
        )
        if not _TRANSFER_OK_RE.search(transfer):
            return transfer  # footer already explains the failure; don't reboot
        if not reboot:
            return transfer
        reboot_out = self.execute_command(
            host, "reboot", expect_regex=r"\(y/n\)", answer="y", port=port
        )
        return transfer.rstrip("\n") + "\n\n--- reboot ---\n" + reboot_out

    def enter_monitor_mode(
        self, host: str, port: int | None = None, timeout: float = 180.0
    ) -> str:
        """Drop the device into the bootloader ``monitor#`` shell (two stages).

        (1) Initiate a reboot: always try the hidden menu first - send Ctrl-] (the
        device prints ``menu:`` then single-keystroke options) and press the option
        whose description contains ``reboot`` (works even at a login prompt, no
        credentials needed); if no such menu appears, fall back to the ``reboot``
        command + ``y``. (2) Interrupt the boot: once ``RTC Test`` is seen,
        send a short bounded burst of Ctrl-P (the actual monitor-entry step - without
        it the unit boots normally), then read for ``monitor#``. Distilled from
        ``monitor&*.py`` + ``boot_interrupt.py`` + ``wait.py``. Control bytes and
        patterns are module constants (verify against hardware).
        """
        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            if not conn.connected:
                raise RuntimeError(
                    f"No active connection to {host}. Please connect first."
                )
            net = conn.connection
            conn.last_activity = _now()
            out = ""
            try:
                try:
                    net.read_channel()  # drain residue
                except Exception:  # noqa: BLE001
                    pass
                # Stage 1: initiate the reboot (hidden menu first, reboot+y fallback).
                # Ctrl-] opens the menu; the option is a single keystroke (no newline).
                menu = _send_and_read(
                    net, _MENU_TRIGGER, _MENU_REBOOT_RE, timeout=10.0, idle=1.0,
                    newline=False,
                )
                out += menu
                option = _MENU_REBOOT_RE.search(menu)
                if option:
                    out += _send_and_read(
                        net, option.group(1), _RTC_TEST_RE, timeout=90.0, idle=2.0,
                        newline=False,
                    )
                else:
                    out += _send_and_read(
                        net, "reboot", _CONFIRM_YN_RE, timeout=15.0, idle=2.0
                    )
                    out += _send_and_read(
                        net, "y", _RTC_TEST_RE, timeout=90.0, idle=2.0
                    )

                # Stage 2: interrupt the boot with a short Ctrl-P burst after RTC Test.
                if _RTC_TEST_RE.search(out):
                    for _ in range(5):
                        net.write_channel(_MONITOR_INTERRUPT)
                        time.sleep(0.5)
                out += _read_until(
                    net, _MONITOR_PROMPT_RE, timeout=max(10.0, timeout - 90.0), idle=2.0
                )
            except Exception as exc:  # noqa: BLE001
                prompt, state = _describe_session(net, conn.console)
                return out.rstrip("\n") + "\n\n" + _footer(
                    failure=f"{type(exc).__name__}: {exc}", prompt=prompt, state=state
                )
            prompt, state = _describe_session(net, conn.console)

        if state == "monitor" or _MONITOR_PROMPT_RE.search(out):
            return out.rstrip("\n") + "\n\n" + _footer(
                note="entered monitor mode", prompt="monitor#", state="monitor"
            )
        return out.rstrip("\n") + "\n\n" + _footer(
            failure="did not reach monitor mode (no 'monitor#' seen)",
            prompt=prompt, state=state,
        )

    def recover_firmware(
        self,
        host: str,
        image_url: str,
        server: str,
        monitor_ip: str,
        mask: str = "255.255.255.0",
        flash_name: str = "switch.bin",
        port: int | None = None,
    ) -> str:
        """End-to-end monitor-mode recovery (the shape of the four ``monitor&*.py``).

        ``enter_monitor_mode`` -> assign ``ip addr`` in the monitor shell -> recover
        the image with ``transfer_file`` -> ``reboot`` into it (raw, **no** Ctrl-P this
        time). Aborts if monitor mode isn't reached or the flash transfer doesn't
        confirm.

        ``image_url`` must be a ``tftp:`` source: the bootloader ``monitor`` ``copy``
        only understands ``tftp:`` (an ``ftp://`` URL is rejected as ``Parameter
        invalid``), and the source name is capped at 60 chars. This network's relay
        shorthand is ``tftp:f::<last-chars-of-ftp-dir>/<file>`` (e.g.
        ``tftp:f::53/BD_3954_interAptiv_2.2.0F_154634.bin`` for FTP dir
        ``/BDCOM0053/``), pointed at the ``server`` (the TFTP->FTP gateway IP).
        """
        # Fail fast before a reboot cycle: monitor 'copy' is tftp-only (issue #2).
        if not image_url.lower().startswith("tftp:"):
            scheme = image_url.split(":", 1)[0] if ":" in image_url else image_url
            return _footer(
                failure=f"monitor-mode 'copy' only supports tftp: sources (got "
                f"'{scheme}:'); use the tftp relay form, e.g. tftp:f::53/<file>.bin",
                state="unknown",
            )

        out = self.enter_monitor_mode(host, port=port)
        if "entered monitor mode" not in out:
            return out  # footer explains why monitor mode wasn't reached

        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            net = conn.connection
            conn.last_activity = _now()
            out += "\n--- assign ip ---\n" + _send_and_read(
                net, f"ip addr {monitor_ip} {mask}", _CLI_PROMPT_RE,
                timeout=15.0, idle=2.0,
            )

        transfer = self.transfer_file(
            host, image_url, f"flash:{flash_name}", server, timeout=600.0, port=port
        )
        out += "\n--- transfer ---\n" + transfer
        if not _TRANSFER_OK_RE.search(transfer):
            return out.rstrip("\n") + "\n\n" + _footer(
                failure="flash transfer did not confirm; not rebooting",
                prompt="monitor#", state="monitor",
            )

        with self._lock:
            conn = self._connections[self._resolve(host, port)]
            net = conn.connection
            conn.last_activity = _now()
            out += "\n--- reboot ---\n" + _send_and_read(
                net, "reboot", _CONFIRM_YN_RE, timeout=15.0, idle=2.0
            )
            try:
                out += _send_and_read(net, "y", None, timeout=8.0, idle=2.0)
            except Exception as exc:  # noqa: BLE001
                out += f"\n[Connection activity during reboot: {exc}]"

        return out.rstrip("\n") + "\n\n" + _footer(
            note="recovery complete: rebooting into new image",
            prompt="monitor#", state="monitor",
        )

    # ------------------------------------------------------------- mode switching
    def _switch_mode(self, conn: DeviceConnection, target: Mode) -> None:
        net = conn.connection

        if target == "auto":
            # Run at the current privilege level: de-nest config submodes but do
            # not downgrade. BDCOM lands in enable at connect (our driver), so
            # 'show' works here; a Cisco user-exec session stays user-exec.
            #
            # Trust the tracked mode - _describe_session refreshes it from the real
            # prompt after every command - rather than probing with check_*_mode(),
            # which each write a bare return the device echoes back as prompt noise.
            if conn.current_mode == "config":
                net.exit_config_mode()
                conn.current_mode = "enable"  # exiting config returns to enable
            elif conn.current_mode not in ("user", "enable"):
                # Unknown/never-run: probe once to establish the level.
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
                if conn.idle_log_stop is not None:
                    conn.idle_log_stop.set()
                # Close the audit log after netmiko's final writes are captured.
                if conn.console is not None:
                    conn.console.close()
                self._connections.pop(key, None)
            return {
                "success": True,
                "message": f"Successfully disconnected from {key}",
                "host": host,
                "port": conn.config.get("port"),
                "target": key,
                "log_file": conn.log_path,
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
                    "log_file": conn.log_path,
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
                if conn.idle_log_stop is not None:
                    conn.idle_log_stop.set()
                if conn.console is not None:
                    conn.console.close()
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
