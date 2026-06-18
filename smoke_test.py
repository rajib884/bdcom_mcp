"""Offline smoke test: verify the FastMCP server loads and behaves correctly.

Does not touch any network device. It inspects the registered tool set, the pure
platform/target helpers, the console ring buffer, and the connection manager's
host:port resolution and error paths.
"""

import asyncio

from device_mcp.bdcom import BdcomSSH, BdcomTelnet
from device_mcp.connection import (
    DeviceConnection,
    DeviceConnectionManager,
    _ConsoleRingLog,
    _target,
    resolve_platform,
)
from device_mcp.server import mcp

EXPECTED_TOOLS = {
    "connect_device",
    "execute_command",
    "disconnect_device",
    "list_connections",
    "get_console_history",
    "read_console_stream",
    "get_help",
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

    # execute_command: interactive + port params surfaced.
    execute = tools["execute_command"]
    eschema = getattr(execute, "parameters", None) or execute.inputSchema
    eparams = eschema["properties"]
    print("execute_command params:", sorted(eparams))
    assert {"expect_string", "answer", "port"} <= set(eparams)

    check_resolve_platform()
    check_target()
    check_console_ring()
    check_target_resolution()
    check_get_help_clears_line()

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
