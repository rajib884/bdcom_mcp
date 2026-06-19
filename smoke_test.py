"""Offline smoke test: verify the FastMCP server loads and behaves correctly.

Does not touch any network device. It inspects the registered tool set, the pure
platform/target helpers, the console ring buffer, and the connection manager's
host:port resolution and error paths.
"""

import asyncio

from netmiko.exceptions import ConfigInvalidException

from device_mcp.bdcom import BdcomSSH, BdcomTelnet
from device_mcp.connection import (
    DeviceConnection,
    DeviceConnectionManager,
    _ConsoleRingLog,
    _classify_prompt,
    _classify_session,
    _detect_device_error,
    _dialect_hint,
    _parse_help_tokens,
    _target,
    resolve_platform,
)
from device_mcp.server import mcp

EXPECTED_TOOLS = {
    "connect_device",
    "execute_command",
    "configure_device",
    "disconnect_device",
    "list_connections",
    "get_console_history",
    "read_console_stream",
    "get_help",
    "transfer_file",
    "upgrade_firmware",
    "enter_monitor_mode",
    "recover_firmware",
}


def check_resolve_platform() -> None:
    # netmiko-dispatched platforms
    assert resolve_platform("cisco_ios", "ssh") == ("netmiko", "cisco_ios")
    assert resolve_platform("cisco_ios", "telnet") == ("netmiko", "cisco_ios_telnet")
    # explicit _telnet device_type is not double-suffixed
    assert resolve_platform("cisco_ios_telnet", "ssh") == ("netmiko", "cisco_ios_telnet")
    # custom BDCOM driver classes
    assert resolve_platform("bdcom", "ssh") == ("class", BdcomSSH)
    assert resolve_platform("bdcom", "telnet") == ("class", BdcomTelnet)
    assert resolve_platform("BDCOM", "ssh") == ("class", BdcomSSH)  # case-insensitive
    # unknown platform
    try:
        resolve_platform("not_a_real_type", "ssh")
        raise SystemExit("expected ValueError for unknown device_type")
    except ValueError as exc:
        print("unknown device_type ->", exc)
    print("resolve_platform: OK")


def check_target() -> None:
    assert _target("10.0.0.1", None, "ssh") == "10.0.0.1:22"
    assert _target("10.0.0.1", None, "telnet") == "10.0.0.1:23"
    assert _target("10.0.0.1", 10003, "telnet") == "10.0.0.1:10003"
    print("_target: OK")


def check_console_ring() -> None:
    ring = _ConsoleRingLog(maxlen=3)
    ring.write(b"a\nb\nc\nd\n")          # only the last 3 complete lines survive
    assert ring.text(10) == "b\nc\nd", ring.text(10)
    ring.write(b"partial")               # a partial (no newline) line is shown too
    assert ring.text(10).endswith("partial")
    assert ring.text(1) == "partial"
    ring.write(" tail\n")                # str input is accepted as well as bytes
    assert ring.text(1) == "partial tail"
    print("_ConsoleRingLog: OK")


def check_target_resolution() -> None:
    """Same-IP / different-port connections must stay independent."""
    mgr = DeviceConnectionManager()

    def fake(host: str, port: int) -> None:
        mgr._connections[f"{host}:{port}"] = DeviceConnection(
            config={"host": host, "port": port, "device_type": "bdcom",
                    "protocol": "telnet", "enable_password": None},
            connection=None,
        )

    fake("1.1.1.1", 10003)
    fake("1.1.1.1", 10004)
    fake("2.2.2.2", 23)

    # explicit port -> exact key
    assert mgr._resolve("1.1.1.1", 10004) == "1.1.1.1:10004"
    # lone connection on a host resolves without a port
    assert mgr._resolve("2.2.2.2", None) == "2.2.2.2:23"
    # ambiguous host (two ports) without a port -> error naming the ports
    try:
        mgr._resolve("1.1.1.1", None)
        raise SystemExit("expected ambiguity error")
    except RuntimeError as exc:
        assert "Multiple connections" in str(exc) and "10003" in str(exc), exc
    # unknown host / wrong port -> not connected
    for args in (("9.9.9.9", None), ("1.1.1.1", 99999)):
        try:
            mgr._resolve(*args)
            raise SystemExit("expected not-connected error")
        except RuntimeError as exc:
            assert "No active connection" in str(exc), exc

    items = mgr.list_connections()
    assert {i["target"] for i in items} == {
        "1.1.1.1:10003", "1.1.1.1:10004", "2.2.2.2:23"
    }
    assert all({"target", "host", "port"} <= set(i) for i in items)
    print("host:port resolution: OK")


class _FakeCli:
    """A tiny Cisco/BDCOM-like CLI line editor for testing line clearing.

    Models the input-line buffer: a backspace erases the last char, or echoes a
    bell (\\x07) if the line is already empty (BDCOM behavior); ``?`` records the
    current prefix as a help request without consuming the line.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self._out = ""
        self.help_requests: list[str] = []

    def write_channel(self, data: str) -> None:
        for ch in data:
            if ch == "\x08":  # backspace
                if self.buffer:
                    self.buffer = self.buffer[:-1]
                    self._out += "\b \b"
                else:
                    self._out += "\x07"  # bell: nothing left to erase
            elif ch == "?":
                self.help_requests.append(self.buffer)
                self._out += f"\n  <help for {self.buffer!r}>\nSwitch#{self.buffer}"
            elif ch in ("\n", "\r"):
                self.buffer = ""
                self._out += "\nSwitch#"
            else:
                self.buffer += ch
                self._out += ch

    def read_channel(self) -> str:
        data, self._out = self._out, ""
        return data

    def find_prompt(self) -> str:
        return "Switch#"


class _FakeNet:
    """A netmiko-channel stand-in for testing execute_command / configure offline."""

    def __init__(self, *, output: str = "", prompt: str = "Switch#",
                 enable: bool = True, config: bool = False,
                 send_raise: Exception | None = None,
                 config_raise: Exception | None = None) -> None:
        self._output = output
        self._prompt = prompt
        self._enable = enable
        self._config = config
        self._send_raise = send_raise
        self._config_raise = config_raise
        self.sent: list[str] = []
        self.config_calls: list[tuple] = []

    # mode introspection / transitions used by _switch_mode("auto")
    def check_enable_mode(self) -> bool:
        return self._enable

    def check_config_mode(self) -> bool:
        return self._config

    def exit_config_mode(self) -> str:
        self._config = False
        return ""

    def enable(self) -> str:
        self._enable = True
        return ""

    def find_prompt(self) -> str:
        return self._prompt

    def send_command(self, command: str, **kw) -> str:
        self.sent.append(command)
        if self._send_raise:
            raise self._send_raise
        return self._output

    def send_command_timing(self, command: str, **kw) -> str:
        self.sent.append(command)
        return self._output

    def send_config_set(self, config_commands, **kw) -> str:
        self.config_calls.append((list(config_commands), kw))
        if self._config_raise:
            raise self._config_raise
        return self._output


def _attach(mgr: DeviceConnectionManager, key: str, net) -> None:
    host, port = key.rsplit(":", 1)
    mgr._connections[key] = DeviceConnection(
        config={"host": host, "port": int(port), "device_type": "bdcom",
                "protocol": "telnet", "enable_password": None},
        connection=net,
    )


def check_diagnostics() -> None:
    """Pure error/prompt/help parsers."""
    caret = " " * 15 + "^"
    out = f"show interface status\n{caret}\nUnknown command"
    de = _detect_device_error(out)
    assert de and de["message"].startswith("Unknown command"), de
    assert de["near"] == "status", de
    de2 = _detect_device_error("% Invalid input detected at '^' marker.")
    assert de2 and "Invalid input" in de2["message"], de2
    assert _detect_device_error("BDCOM S3954 Software, Version 2.2.0F") is None

    assert _classify_prompt("Switch>") == "user"
    assert _classify_prompt("Switch#") == "enable"
    assert _classify_prompt("Switch_config#") == "config"
    assert _classify_prompt("Switch_config_vlan30#") == "config"
    assert _classify_prompt("R1(config)#") == "config"
    assert _classify_prompt("R1#") == "enable"

    tokens = _parse_help_tokens(
        "  interface                  -- Interface status\n"
        "  ip                         -- IP config\n"
        "Switch_config#"
    )
    assert tokens == ["interface", "ip"], tokens
    print("diagnostics helpers: OK")


def check_execute_footer() -> None:
    mgr = DeviceConnectionManager()

    caret = " " * 15 + "^"
    err = f"show interface status\n{caret}\nUnknown command"
    _attach(mgr, "h:23", _FakeNet(output=err, prompt="Switch#"))
    res = mgr.execute_command("h", "show interface status", port=23)
    assert "Unknown command" in res                      # raw output preserved
    assert "[device-mcp] device error: Unknown command near 'status'" in res, res
    assert "now: Switch# (enable)" in res, res

    _attach(mgr, "h:24", _FakeNet(output="...Version 2.2.0F", prompt="Switch#"))
    ok = mgr.execute_command("h", "show version", port=24)
    assert ok.rstrip().endswith("[device-mcp] ok | now: Switch# (enable)"), ok

    _attach(mgr, "h:25", _FakeNet(send_raise=RuntimeError("Pattern not detected"),
                                  prompt="Switch#"))
    fail = mgr.execute_command("h", "show running-config", port=25)
    assert "[device-mcp] FAILED:" in fail and "Pattern not detected" in fail, fail
    print("execute_command footer: OK")


def check_configure() -> None:
    mgr = DeviceConnectionManager()

    net = _FakeNet(output="", prompt="Switch#")
    _attach(mgr, "h:23", net)
    cmds = ["vlan 30", "exit", "interface GigaEthernet0/1", "exit"]
    res = mgr.configure("h", cmds, port=23)
    assert net.config_calls, "send_config_set was not called"
    sent, kw = net.config_calls[0]
    assert sent == cmds, sent
    assert kw.get("error_pattern"), "error_pattern not forwarded"
    assert "applied 4 command(s)" in res, res

    net2 = _FakeNet(prompt="Switch_config_vlan30#", config=True,
                    config_raise=ConfigInvalidException(
                        "Invalid input detected at command: switchport access vlan 30"))
    _attach(mgr, "h:24", net2)
    res2 = mgr.configure("h", ["switchport access vlan 30"], port=24)
    assert "[device-mcp] FAILED:" in res2, res2
    assert "switchport access vlan 30" in res2, res2
    assert "(config)" in res2, res2   # reports it ended in a config submode
    print("configure: OK")


def check_get_help_normalize() -> None:
    """A manual trailing '?' and multiple trailing spaces are normalized."""
    mgr = DeviceConnectionManager()
    cli = _FakeCli()
    mgr._connections["h:23"] = DeviceConnection(
        config={"host": "h", "port": 23, "device_type": "bdcom", "protocol": "telnet"},
        connection=cli,
    )
    mgr.get_help("h", "show int?", port=23)     # stray trailing '?' stripped
    mgr.get_help("h", "show int   ", port=23)   # multiple trailing spaces -> one
    assert cli.help_requests == ["show int", "show int "], cli.help_requests
    print("get_help normalize: OK")


def check_get_help_clears_line() -> None:
    """Regression: consecutive get_help calls must not leak the prior prefix.

    The original bug prepended a previous prefix to the next request, e.g.
    ``show int`` then ``show interface `` produced ``show intshow interface ?``.
    """
    mgr = DeviceConnectionManager()
    cli = _FakeCli()
    mgr._connections["h:23"] = DeviceConnection(
        config={"host": "h", "port": 23, "device_type": "bdcom", "protocol": "telnet"},
        connection=cli,
    )

    mgr.get_help("h", "show int", port=23)
    assert cli.buffer == "", f"input line not cleared after get_help: {cli.buffer!r}"
    mgr.get_help("h", "show interface ", port=23)
    assert cli.buffer == "", f"input line not cleared after get_help: {cli.buffer!r}"

    # The device saw exactly our two prefixes - no leakage between calls.
    assert cli.help_requests == ["show int", "show interface "], cli.help_requests
    print("get_help line-clear: OK")


class _FakeLoginNet:
    """A netmiko stand-in stuck at a login prompt (a desynced session)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def check_enable_mode(self) -> bool:
        return False

    def check_config_mode(self) -> bool:
        return False

    def find_prompt(self) -> str:
        return "Username:"

    def send_command(self, command: str, **kw) -> str:
        self.sent.append(command)
        raise RuntimeError("Pattern not detected: 'Switch.*'")

    def send_command_timing(self, command: str, **kw) -> str:
        self.sent.append(command)
        raise RuntimeError("Pattern not detected")


class _FakeTransfer:
    """A netmiko stand-in that replies to a 'copy' with a scripted transfer log."""

    RETURN = "\n"

    def __init__(self, response: str) -> None:
        self._response = response
        self._buf = ""
        self.sent: list[str] = []

    def write_channel(self, data: str) -> None:
        self.sent.append(data)
        if data.strip().startswith("copy"):
            self._buf += self._response

    def read_channel(self) -> str:
        data, self._buf = self._buf, ""
        return data

    def find_prompt(self) -> str:
        return "Switch#"


class _FakeBoot:
    """A netmiko stand-in that reaches 'monitor#' only after a Ctrl-P burst."""

    RETURN = "\n"

    def __init__(self) -> None:
        self._buf = ""
        self.ctrlp = 0
        self.sent: list[str] = []

    def write_channel(self, data: str) -> None:
        self.sent.append(data)
        if data == "\x1d":  # Ctrl-]: open the hidden menu (printed as "menu:")
            self._buf += ("\nmenu:\n3       dump rawlog info\n4       reboot\n"
                          "5       debug info\n6       console info\n")
        elif data == "4":  # the reboot option, a single keystroke (no newline)
            self._buf += ("\nSystem is rebooting, flag=0xf000c\nU-Boot 2011.12\n"
                          "RTC Test......................PASS!\n")
        elif data == "\x10":  # Ctrl-P interrupts the boot into the monitor shell
            self.ctrlp += 1
            self._buf += "monitor#"

    def read_channel(self) -> str:
        data, self._buf = self._buf, ""
        return data

    def find_prompt(self) -> str:
        return "monitor#"


def check_session_state() -> None:
    assert _classify_session("Username: ") == "awaiting_login"
    assert _classify_session("Password:") == "awaiting_login"
    assert _classify_session("Authentication failed!") == "awaiting_login"
    assert _classify_session("User admin logged out on console 0") == "session_terminated"
    assert _classify_session("monitor#") == "monitor"
    assert _classify_session("Switch>") == "user"
    assert _classify_session("Switch#") == "enable"
    assert _classify_session("Switch_config#") == "config"
    assert _classify_session("") == "unknown"
    assert _classify_session("rd") == "unknown"  # junk help token, not a mode
    print("session-state classifier: OK")


def check_dialect_hint() -> None:
    assert "write" in (_dialect_hint("write memory") or "")
    assert "switchport pvid" in (_dialect_hint("switchport access vlan 10") or "")
    assert "config" in (_dialect_hint("configure terminal") or "")
    assert _dialect_hint("show vlan brief")
    assert _dialect_hint("show interface status")
    assert _dialect_hint("show version") is None
    print("dialect hints: OK")


def check_session_terminated() -> None:
    mgr = DeviceConnectionManager()
    _attach(mgr, "h:23", _FakeLoginNet())
    res = mgr.execute_command("h", "show version", port=23)
    assert "SESSION_TERMINATED" in res, res
    assert "now: awaiting_login" in res, res          # no raw "Username:" leak
    # write memory dropped us to login: the footer should still carry the hint.
    _attach(mgr, "h:24", _FakeLoginNet())
    res2 = mgr.execute_command("h", "write memory", port=24)
    assert "SESSION_TERMINATED" in res2 and "hint:" in res2, res2
    print("session terminated reporting: OK")


def check_transfer_file() -> None:
    mgr = DeviceConnectionManager()
    ok_log = ("copy tftp:img.bin flash:switch.bin 1.1.1.1\nTFTP:\n!!!!!\n"
              "successfully receive 8027243 bytes.\nSwitch#")
    _attach(mgr, "h:23", _FakeTransfer(ok_log))
    res = mgr.transfer_file("h", "tftp:img.bin", "flash:switch.bin", "1.1.1.1", port=23)
    assert "[device-mcp] transfer ok" in res, res

    fail_log = "copy tftp:img.bin flash:switch.bin 1.1.1.1\nTFTP:\ntimeout, aborted\nSwitch#"
    _attach(mgr, "h:24", _FakeTransfer(fail_log))
    res2 = mgr.transfer_file("h", "tftp:img.bin", "flash:switch.bin", "1.1.1.1", port=24)
    assert "FAILED: transfer did not confirm" in res2, res2
    print("transfer_file: OK")


def check_enter_monitor_mode() -> None:
    mgr = DeviceConnectionManager()
    net = _FakeBoot()
    _attach(mgr, "h:23", net)
    res = mgr.enter_monitor_mode("h", port=23, timeout=30.0)
    assert "entered monitor mode" in res, res
    assert "now: monitor# (monitor)" in res, res
    assert net.ctrlp >= 1, net.ctrlp            # the Ctrl-P burst actually fired
    assert "\x1d" in net.sent, net.sent         # opened the menu with Ctrl-]
    assert "4" in net.sent, net.sent            # pressed the reboot option (no newline)
    print("enter_monitor_mode: OK")


async def main() -> None:
    tool_list = await mcp.list_tools()
    tools = {t.name: t for t in tool_list}
    names = sorted(tools)
    print("Registered tools:", names)
    assert EXPECTED_TOOLS == set(names), (
        f"tool mismatch: missing {EXPECTED_TOOLS - set(names)}, "
        f"extra {set(names) - EXPECTED_TOOLS}"
    )

    # connect_device: device_type/enable_password present; auth now optional.
    connect = tools["connect_device"]
    cschema = getattr(connect, "parameters", None) or connect.inputSchema
    cparams = cschema["properties"]
    print("connect_device params:", sorted(cparams))
    assert {"device_type", "enable_password", "port"} <= set(cparams)
    required = set(cschema.get("required", []))
    assert "host" in required
    assert "username" not in required and "password" not in required, required

    # execute_command: interactive + port params surfaced; default mode is auto.
    execute = tools["execute_command"]
    eschema = getattr(execute, "parameters", None) or execute.inputSchema
    eparams = eschema["properties"]
    print("execute_command params:", sorted(eparams))
    assert {"expect_string", "answer", "port"} <= set(eparams)
    assert eparams["mode"].get("default") == "auto", eparams["mode"]

    # configure_device: batch config tool with a commands list.
    configure = tools["configure_device"]
    fschema = getattr(configure, "parameters", None) or configure.inputSchema
    print("configure_device params:", sorted(fschema["properties"]))
    assert "commands" in fschema["properties"]

    check_resolve_platform()
    check_target()
    check_console_ring()
    check_target_resolution()
    check_diagnostics()
    check_session_state()
    check_dialect_hint()
    check_execute_footer()
    check_session_terminated()
    check_configure()
    check_transfer_file()
    check_enter_monitor_mode()
    check_get_help_clears_line()
    check_get_help_normalize()

    # Manager error paths (no real device involved).
    mgr = DeviceConnectionManager()
    assert mgr.list_connections() == []
    try:
        mgr.execute_command("10.0.0.1", "show version")
        raise SystemExit("expected RuntimeError for missing connection")
    except RuntimeError as exc:
        print("execute without connection ->", exc)
    disc = mgr.disconnect("10.0.0.1")
    print("disconnect unknown host ->", disc)
    assert disc["success"] is False
    # Unknown device_type is rejected without any network I/O.
    bad = mgr.connect("10.0.0.1", "u", "p", device_type="not_a_real_type")
    print("connect bad device_type ->", bad)
    assert bad["success"] is False

    print("OK: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
