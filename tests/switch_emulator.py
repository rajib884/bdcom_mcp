"""A small, scriptable Telnet switch emulator for integration tests.

The unit tests in ``smoke_test.py`` replace netmiko's methods with hand-written
fakes, so they never exercise the real connection path. This emulator closes that
gap: it is an actual TCP server that speaks a Cisco/BDCOM-like CLI over a socket, so
a test can point the *real* ``DeviceConnectionManager`` (and netmiko's ``BdcomTelnet``
/ ``CiscoIosTelnet`` driver) at it and drive login, ``session_preparation``, enable
and config-mode transitions, paging, command I/O and inline ``?`` help end to end.

The point of the emulator is control: a test supplies a ``responses`` map of
``command -> output`` (string, regex, or callable) and asserts on what the manager
returns. Everything else (prompts, login, the BDCOM ``config``/Ctrl-Z quirks) is
modelled faithfully enough that netmiko's own session machinery drives it unmodified.

Only Telnet is implemented: SSH and Telnet share netmiko's entire CLI layer above
the transport, so a Telnet emulator covers the command/mode/driver logic without a
paramiko server. It depends on nothing outside the standard library.

Example
-------
    with SwitchEmulator(responses={"show version": "BDCOM Software, Version 2.2.0F"}) as sw:
        mgr = DeviceConnectionManager()
        mgr.connect("127.0.0.1", "admin", "admin", device_type="bdcom",
                    protocol="telnet", port=sw.port)
        print(mgr.execute_command("127.0.0.1", "show version", port=sw.port))
"""

from __future__ import annotations

import re
import socket
import socketserver
import threading
from typing import Any, Callable, Optional, Pattern, Union

# A response value: a literal string, or a callable taking the typed command and
# returning the output string. Keys may be exact strings or compiled regexes.
Responder = Union[str, Callable[[str], str]]

# Telnet control bytes we must cope with (RFC 854). We never initiate option
# negotiation; we just strip whatever the client's telnet library sends so it can
# never reach the command line buffer.
_IAC = 0xFF
_SB = 0xFA
_SE = 0xF0

_CTRL_Z = "\x1a"  # exit config mode on BDCOM (sent without a newline, never echoed)
_BACKSPACE = "\x08"
_BEL = "\x07"

_CRLF = "\r\n"

# How often a blocked handler wakes to notice the server is stopping.
_RECV_TIMEOUT = 0.5


class _Server(socketserver.ThreadingTCPServer):
    """Threaded TCP server tuned for tests.

    ``daemon_threads`` + ``block_on_close=False`` mean :meth:`stop` returns at once
    instead of joining a client handler that is still blocked reading the socket -
    which deadlocks if the emulator is torn down before the netmiko client that is
    talking to it disconnects.
    """

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


def _strip_iac(data: bytes) -> bytes:
    """Remove Telnet IAC command/negotiation/subnegotiation sequences from ``data``."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b != _IAC:
            out.append(b)
            i += 1
            continue
        # data[i] == IAC
        if i + 1 >= n:
            break  # dangling IAC at the buffer edge; drop it
        nxt = data[i + 1]
        if nxt == _IAC:  # escaped literal 0xFF
            out.append(_IAC)
            i += 2
        elif nxt == _SB:  # subnegotiation: skip up to IAC SE
            j = i + 2
            while j + 1 < n and not (data[j] == _IAC and data[j + 1] == _SE):
                j += 1
            i = j + 2
        else:  # WILL/WONT/DO/DONT (or any other) + option byte
            i += 3
    return bytes(out)


class SwitchEmulator:
    """A controllable Telnet CLI server emulating a BDCOM/Cisco switch.

    Bind to an OS-assigned ephemeral port on the loopback interface and serve each
    client on its own thread, mirroring netmiko's one-socket-per-connection model.
    Start it explicitly with :meth:`start` (and :meth:`stop` when done) or use it as a
    context manager. The assigned port is available as :attr:`port`.
    """

    def __init__(
        self,
        *,
        responses: Optional[dict[Union[str, Pattern[str]], Responder]] = None,
        confirmations: Optional[dict[str, dict[str, Any]]] = None,
        hostname: str = "Switch",
        dialect: str = "bdcom",
        username: Optional[str] = "admin",
        password: Optional[str] = "admin",
        enable_password: Optional[str] = None,
        unknown_command_reply: Optional[Callable[[str], str]] = None,
        banner: str = "",
    ) -> None:
        if dialect not in ("bdcom", "cisco"):
            raise ValueError("dialect must be 'bdcom' or 'cisco'")
        self.responses = responses or {}
        # Interactive confirmations: command -> {"prompt": "...(y/n)?",
        # "answers": {"y": "Rebooting...", "n": "Aborted."}}. The command emits the
        # prompt and waits for a line; the matching answer's text is then returned.
        self.confirmations = confirmations or {}
        self.hostname = hostname
        self.dialect = dialect
        self.username = username
        self.password = password
        self.enable_password = enable_password
        self.banner = banner
        self._unknown = unknown_command_reply or _default_unknown_reply
        # Per-test toggles a test can flip mid-session: terminate forces every client
        # back to the login prompt (a desynced session); paging emits a --More-- pager.
        self.terminate = False
        self.paging = False
        # Commands every connected handler has seen, for assertions (thread-safe-ish:
        # tests use one client at a time).
        self.commands_seen: list[str] = []

        self.stopping = False  # set on stop() so blocked handlers exit promptly
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> "SwitchEmulator":
        emulator = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # noqa: D401 - socketserver hook
                _Session(emulator, self.request).run()

        self.stopping = False
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="switch-emulator", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping = True
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("emulator not started")
        return self._server.server_address[1]

    def __enter__(self) -> "SwitchEmulator":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------ response
    def lookup(self, command: str) -> Optional[str]:
        """Return the configured output for ``command``, or ``None`` if unmapped.

        Exact string keys win over regex keys; a callable value is invoked with the
        full typed command so a test can build a dynamic reply.
        """
        if command in self.responses:
            return _render(self.responses[command], command)
        for key, value in self.responses.items():
            if isinstance(key, re.Pattern) and key.search(command):
                return _render(value, command)
        return None


def _render(value: Responder, command: str) -> str:
    return value(command) if callable(value) else value


def _default_unknown_reply(command: str) -> str:
    """A Cisco/BDCOM-style rejection with a caret under the offending token.

    Mirrors the device output ``_detect_device_error`` parses: the last word of the
    command is marked with a ``^`` so the manager reports ``... near '<word>'``.
    """
    word = command.split()[-1] if command.split() else command
    caret_col = command.rfind(word)
    return f"{' ' * caret_col}^\nUnknown command."


class _Session:
    """Drives one client connection: login, prompts, and the CLI state machine."""

    def __init__(self, emu: SwitchEmulator, sock: Any) -> None:
        self.emu = emu
        self.sock = sock
        self.line = ""              # the current (un-submitted) input line
        self.echo = True            # off only while reading a password
        self.mode = "user"          # user | enable | config
        self.submode = ""           # config sub-mode token, e.g. "g0/1" or "vlan30"
        # Pending login state: None once authenticated / when no auth is required.
        self.await_field: Optional[str] = None  # "username" | "password" | None
        self.await_enable = False   # next line is the enable secret
        self.await_confirm: Optional[dict] = None  # active interactive confirmation
        self._pending_user = ""
        self._lf_pending = False    # saw a CR; swallow a following LF (CRLF = one line)
        self._outbuf: list[str] = []  # output batched until the next flush

    # --------------------------------------------------------------- prompt model
    def prompt(self) -> str:
        host = self.emu.hostname
        if self.mode == "user":
            return f"{host}>"
        if self.mode == "enable":
            return f"{host}#"
        # config
        if self.emu.dialect == "cisco":
            return f"{host}(config-{self.submode})#" if self.submode else f"{host}(config)#"
        return f"{host}_config_{self.submode}#" if self.submode else f"{host}_config#"

    # ------------------------------------------------------------------ socket io
    def send(self, text: str) -> None:
        """Buffer output; it is flushed as one TCP write per received chunk.

        Coalescing matters: a real device returns its echo + output + prompt in one
        burst, and netmiko's prompt detection races if we split a prompt across two
        packets (it can read a bare newline first and miss the prompt, desyncing the
        session). Batching per recv keeps each response atomic.
        """
        self._outbuf.append(text)

    def _flush(self) -> None:
        if not self._outbuf:
            return
        data = "".join(self._outbuf).encode("utf-8", "replace")
        self._outbuf.clear()
        try:
            self.sock.sendall(data)
        except OSError:
            pass  # client gone; the recv loop will see EOF and exit

    def run(self) -> None:
        self.sock.settimeout(_RECV_TIMEOUT)
        try:
            self._greet()
            self._flush()
            buf = bytearray()
            while True:
                try:
                    data = self.sock.recv(4096)
                except socket.timeout:
                    if self.emu.stopping:
                        return  # emulator is shutting down; let the handler exit
                    continue
                if not data:
                    return  # client closed
                buf += _strip_iac(data)
                # Process decoded characters; keep undecodable bytes for next recv.
                text = buf.decode("utf-8", "ignore")
                buf.clear()
                for ch in text:
                    self._feed(ch)
                self._flush()  # one atomic response per received chunk
        except _Disconnect:
            self._flush()
            return
        except OSError:
            return
        finally:
            try:
                self.sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ greeting
    def _greet(self) -> None:
        if self.emu.username is not None or self.emu.password is not None:
            self.await_field = "username" if self.emu.username is not None else "password"
            if self.emu.banner:
                self.send(self.emu.banner + _CRLF)
            self.send(_CRLF + ("Username: " if self.await_field == "username" else "Password: "))
            if self.await_field == "password":
                self.echo = False
        else:
            # Unsecured port: land straight at the user prompt.
            if self.emu.banner:
                self.send(self.emu.banner + _CRLF)
            self.send(_CRLF + self.prompt())

    # ----------------------------------------------------------- input dispatch
    def _feed(self, ch: str) -> None:
        # CRLF / CR / LF all terminate a line; collapse a CR+LF pair into one.
        if ch == "\r":
            self._lf_pending = True
            self._submit_line()
            return
        if ch == "\n":
            if self._lf_pending:
                self._lf_pending = False
                return
            self._submit_line()
            return
        self._lf_pending = False

        if ch == _CTRL_Z:
            # BDCOM exits config mode on Ctrl-Z, with no echo of the control byte.
            self._handle_ctrl_z()
            return
        if ch == "?":
            self._handle_help()
            return
        if ch == _BACKSPACE:
            self._handle_backspace()
            return
        # Ordinary character: buffer it and echo it (devices echo typed input, which
        # the connection layer relies on for command verification).
        self.line += ch
        if self.echo:
            self.send(ch)

    def _submit_line(self) -> None:
        line, self.line = self.line, ""
        if self.echo:
            self.send(_CRLF)
        if self.await_confirm is not None:
            self._handle_confirm(line.strip())
            return
        if self.await_enable:
            self._handle_enable_secret(line)
            return
        if self.await_field is not None:
            self._handle_login(line)
            return
        self._handle_command(line.strip())

    # ---------------------------------------------------------------------- login
    def _handle_login(self, line: str) -> None:
        if self.await_field == "username":
            self._pending_user = line.strip()
            self.await_field = "password"
            self.echo = False
            self.send("Password: ")
            return
        # await_field == "password"
        self.echo = True
        user_ok = self.emu.username is None or self._pending_user == self.emu.username
        pass_ok = self.emu.password is None or line == self.emu.password
        if user_ok and pass_ok:
            self.await_field = None
            self.mode = "user"
            # A blank line before the prompt gives session_preparation's channel
            # read a clean '>' to latch onto.
            self.send(_CRLF + self.prompt())
        else:
            # Reject and drop the connection: netmiko sees EOF and raises a clean
            # NetmikoAuthenticationException instead of retrying for ~30s.
            self.send(_CRLF + "% Authentication failed" + _CRLF)
            raise _Disconnect()

    # -------------------------------------------------------------------- command
    def _handle_command(self, cmd: str) -> None:
        if self.emu.terminate:
            # Simulate a command (e.g. BDCOM 'write memory') that logs the session
            # out: redraw the login prompt, then drop the connection. Closing makes
            # netmiko fail fast instead of waiting out its read timeout for a prompt
            # that will never come, and the connection layer still sees the
            # 'Username:' in the console tail and reports the session as terminated.
            self.send(_CRLF + "Username: ")
            raise _Disconnect()
        if cmd:
            self.emu.commands_seen.append(cmd)

        low = cmd.lower()
        if cmd == "":
            self.send(self.prompt())
            return

        # Mode transitions ----------------------------------------------------
        if low == "enable" and self.mode == "user":
            self._enter_enable()
            return
        if low in ("disable", "exit") and self.mode == "enable":
            self.mode = "user"
            self.send(self.prompt())
            return
        if self._is_config_cmd(low) and self.mode == "enable":
            self.mode = "config"
            self.submode = ""
            self.send(self.prompt())
            return
        if self.mode == "config":
            self._handle_config_line(cmd, low)
            return

        # Paging / terminal knobs --------------------------------------------
        if low.startswith("terminal length"):
            # BDCOM only accepts this in enable; here we just acknowledge it.
            self.send(self.prompt())
            return
        if low.startswith("terminal width"):
            if self.emu.dialect == "bdcom":
                self.send(self._unknown_reply(cmd))
            else:
                self.send(self.prompt())
            return

        # Interactive confirmation: emit the prompt and wait for an answer line.
        if cmd in self.emu.confirmations:
            entry = self.emu.confirmations[cmd]
            self.await_confirm = entry
            self.send(entry.get("prompt", "Are you sure? (y/n)"))
            return

        # Ordinary show/exec command -----------------------------------------
        output = self.emu.lookup(cmd)
        if output is None:
            self.send(self._unknown_reply(cmd))
            return
        self._send_output(output)

    def _enter_enable(self) -> None:
        if self.emu.enable_password:
            # Read the secret on the next line without echo, then elevate.
            self.await_enable = True
            self.echo = False
            self.send("Password: ")
            return
        self.mode = "enable"
        self.send(self.prompt())

    def _handle_enable_secret(self, line: str) -> None:
        self.await_enable = False
        self.echo = True
        if line == self.emu.enable_password:
            self.mode = "enable"
            self.send(self.prompt())
        else:
            self.send("% Bad secrets" + _CRLF + self.prompt())

    def _handle_confirm(self, line: str) -> None:
        """Resolve an interactive confirmation: emit the answer's text, redraw prompt."""
        entry = self.await_confirm or {}
        self.await_confirm = None
        answers = entry.get("answers", {})
        out = answers.get(line.strip().lower(), entry.get("default", ""))
        body = (out + "\n").replace("\n", _CRLF) if out else ""
        self.send(body + self.prompt())

    def _is_config_cmd(self, low: str) -> bool:
        if self.emu.dialect == "cisco":
            return low in ("configure terminal", "config terminal", "conf t", "configure")
        return low in ("config", "configure")

    def _handle_config_line(self, cmd: str, low: str) -> None:
        # Leaving config sub-modes and config itself.
        if self.emu.dialect == "cisco" and low == "end":
            self.mode = "enable"
            self.submode = ""
            self.send(self.prompt())
            return
        if low in ("exit", "quit"):
            if self.submode:
                self.submode = ""
            else:
                self.mode = "enable"
            self.send(self.prompt())
            return
        # Entering a sub-mode.
        token = self._submode_token(low)
        if token is not None:
            self.submode = token
            self.send(self.prompt())
            return
        # A plain config command: apply it (configured output, if any) and redraw.
        output = self.emu.lookup(cmd)
        if output is None:
            self.send(self.prompt())
        else:
            self._send_output(output)

    def _submode_token(self, low: str) -> Optional[str]:
        """Map a config command to the sub-mode token the prompt would show.

        BDCOM: ``interface g0/1`` -> ``g0/1`` (prompt ``Switch_config_g0/1#``);
        ``vlan 30`` -> ``vlan30``; ``line vty 0`` -> ``line``. Cisco uses ``-if`` etc.
        """
        m = re.match(r"interface\s+(\S+)", low)
        if m:
            return "if" if self.emu.dialect == "cisco" else m.group(1)
        m = re.match(r"vlan\s+(\d+)", low)
        if m:
            return "vlan" if self.emu.dialect == "cisco" else f"vlan{m.group(1)}"
        if low.startswith("line "):
            return "line"
        return None

    # ----------------------------------------------------------------- help / ?
    def _handle_help(self) -> None:
        """Answer an inline ``?``: list help, then redraw the prompt + typed prefix.

        The connection layer waits for the redrawn prompt as its "help done" marker
        and then backspaces the prefix off the line, so the prefix must stay buffered.
        """
        prefix = self.line
        body = self._help_body(prefix)
        # Help text, then the prompt with the prefix still on the input line.
        self.send(_CRLF + body + self.prompt() + prefix)

    def _help_body(self, prefix: str) -> str:
        key = ("help", prefix.strip())
        custom = self.emu.responses.get(key)  # type: ignore[arg-type]
        if custom is not None:
            return _render(custom, prefix) + _CRLF
        # A generic two-option help listing in the device's ``token  -- desc`` shape.
        return (
            "  interface                  -- Select an interface" + _CRLF
            + "  ip                         -- Global IP configuration" + _CRLF
        )

    def _handle_backspace(self) -> None:
        # Erase one char, or ring the bell if the line is already empty (BDCOM
        # behavior the input-line clearer relies on to know when to stop).
        if self.line:
            self.line = self.line[:-1]
            self.send("\b \b")
        else:
            self.send(_BEL)

    def _handle_ctrl_z(self) -> None:
        if self.mode == "config":
            self.mode = "enable"
            self.submode = ""
        self.send(_CRLF + self.prompt())

    # ------------------------------------------------------------------- output
    def _send_output(self, output: str) -> None:
        body = output if output.endswith("\n") else output + "\n"
        body = body.replace("\n", _CRLF)
        if self.emu.paging:
            self._send_paged(body)
        else:
            self.send(body + self.prompt())

    def _send_paged(self, body: str) -> None:
        # Emit a --More-- pager every few lines; a space or return advances it.
        lines = body.split(_CRLF)
        chunk = 5
        for i in range(0, len(lines), chunk):
            self.send(_CRLF.join(lines[i : i + chunk]))
            if i + chunk < len(lines):
                self.send(_CRLF + " --More-- ")
        self.send(self.prompt())

    def _unknown_reply(self, cmd: str) -> str:
        # The line-submit already echoed a CRLF, so the caret line sits directly under
        # the echoed command - which is where _detect_device_error expects it.
        return self.emu._unknown(cmd).replace("\n", _CRLF) + _CRLF + self.prompt()


class _Disconnect(Exception):
    """Internal signal to tear the client connection down."""
