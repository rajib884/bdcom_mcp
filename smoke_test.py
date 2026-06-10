"""Offline smoke test: verify the FastMCP server loads and registers tools.

Does not touch any network device. It inspects the registered tool set, the
pure platform-resolution helper, and the connection manager's error paths.
"""

import asyncio

from device_mcp.bdcom import BdcomSSH, BdcomTelnet
from device_mcp.connection import DeviceConnectionManager, resolve_platform
from device_mcp.server import mcp


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


async def main() -> None:
    tool_list = await mcp.list_tools()
    tools = {t.name: t for t in tool_list}
    names = sorted(tools)
    print("Registered tools:", names)

    expected = {
        "connect_device",
        "execute_command",
        "disconnect_device",
        "list_connections",
    }
    assert expected.issubset(set(names)), f"Missing tools: {expected - set(names)}"

    # Confirm the generated input schema exposes device_type + enable_password.
    connect = tools["connect_device"]
    schema = getattr(connect, "parameters", None) or connect.inputSchema
    params = schema["properties"]
    print("connect_device params:", sorted(params))
    assert "device_type" in params
    assert "enable_password" in params

    check_resolve_platform()

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
