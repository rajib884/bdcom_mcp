"""Offline smoke test: verify the FastMCP server loads and registers tools.

Does not touch any network device — it only inspects the registered tool set
and exercises the connection manager's error paths.
"""

import asyncio

from cisco_mcp.connection import CiscoConnectionManager
from cisco_mcp.server import mcp


async def main() -> None:
    tool_list = await mcp.list_tools()
    tools = {t.name: t for t in tool_list}
    names = sorted(tools)
    print("Registered tools:", names)

    expected = {
        "connect_cisco_device",
        "execute_cisco_command",
        "disconnect_cisco_device",
        "list_connections",
    }
    assert expected.issubset(set(names)), f"Missing tools: {expected - set(names)}"

    # Show the generated input schema for one tool to confirm Field metadata.
    connect = tools["connect_cisco_device"]
    schema = getattr(connect, "parameters", None) or connect.inputSchema
    params = schema["properties"]
    print("connect_cisco_device params:", sorted(params))
    assert "enable_password" in params

    # Manager error paths (no real device involved).
    mgr = CiscoConnectionManager()
    assert mgr.list_connections() == []
    try:
        mgr.execute_command("10.0.0.1", "show version")
        raise SystemExit("expected RuntimeError for missing connection")
    except RuntimeError as exc:
        print("execute without connection ->", exc)
    disc = mgr.disconnect("10.0.0.1")
    print("disconnect unknown host ->", disc)
    assert disc["success"] is False

    print("OK: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
