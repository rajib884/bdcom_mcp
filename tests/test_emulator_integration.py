"""End-to-end tests that drive the real connection manager + netmiko drivers against
the in-process :class:`SwitchEmulator`.

Unlike ``smoke_test.py`` (which swaps netmiko out for fakes), every test here opens a
genuine Telnet session over a loopback socket, so it exercises login, the BDCOM
``session_preparation``/enable/paging path, real prompt detection, and the
``execute_command`` / ``configure`` / ``get_help`` round-trips as a deployed server
would. The emulator's ``responses`` map is how each test pins the device output for a
given command.
"""

from __future__ import annotations

import re

import pytest

HOST = "127.0.0.1"


def _connect(manager, sw, **overrides):
    """Connect the manager to an emulator, returning the connect() result dict."""
    params = dict(
        host=HOST,
        username="admin",
        password="admin",
        device_type="bdcom",
        protocol="telnet",
        port=sw.port,
    )
    params.update(overrides)
    return manager.connect(**params)


# --------------------------------------------------------------------- connect

def test_connect_reaches_enable_and_disables_paging(manager, make_switch):
    """A BDCOM Telnet connect logs in, elevates to enable, and turns paging off.

    This is the path the unit fakes never run: netmiko's telnet_login plus the custom
    BdcomMixin.session_preparation (enter enable, ``terminal length 0``).
    """
    sw = make_switch()
    res = _connect(manager, sw)
    assert res["success"] is True, res
    assert f"{HOST}:{sw.port}" in res["message"]
    # session_preparation actually elevated and disabled paging on the wire.
    assert "enable" in sw.commands_seen
    assert "terminal length 0" in sw.commands_seen


def test_connect_unsecured_no_credentials(manager, make_switch):
    """A port with no username/password lands straight at the prompt and works."""
    sw = make_switch(username=None, password=None,
                     responses={"show version": "BDCOM Software, Version 2.2.0F"})
    res = manager.connect(host=HOST, device_type="bdcom", protocol="telnet", port=sw.port)
    assert res["success"] is True, res
    out = manager.execute_command(HOST, "show version", port=sw.port)
    assert "Version 2.2.0F" in out


def test_connect_auth_failure_reports_transcript(manager, make_switch):
    """Wrong credentials -> connect fails and surfaces the device's last output."""
    sw = make_switch(password="s3cret")
    res = _connect(manager, sw, password="wrong")
    assert res["success"] is False, res
    assert "Authentication failed" in res["message"]


def test_connect_enable_password_flow(manager, make_switch):
    """An enable secret is sent at the device's ``Password:`` prompt to reach enable."""
    sw = make_switch(enable_password="enpw",
                     responses={"show clock": "12:00:00 UTC Mon Jun 25 2026"})
    res = _connect(manager, sw, enable_password="enpw")
    assert res["success"] is True, res
    out = manager.execute_command(HOST, "show clock", mode="enable", port=sw.port)
    assert "12:00:00" in out
    assert "now: Switch# (enable)" in out


# ----------------------------------------------------------------- execute

def test_execute_command_returns_lean_output(manager, make_switch):
    """A clean raw command returns just the device output ending at the prompt - no footer.

    raw is the default mode: the trailing prompt is already embedded in the output, so a
    success footer would only add redundant tokens. We assert it is absent.
    """
    sw = make_switch(responses={"show version": "BDCOM(tm) S3954, Version 2.2.0F\nuptime 5 days"})
    _connect(manager, sw)
    out = manager.execute_command(HOST, "show version", port=sw.port)
    assert "Version 2.2.0F" in out
    assert "uptime 5 days" in out
    assert "[device-mcp]" not in out, out  # no footer on a clean run
    assert out.rstrip().endswith("Switch#"), out  # output ends at the live prompt


def test_execute_command_controls_output_per_command(manager, make_switch):
    """The emulator returns exactly the mapped output for each distinct command."""
    sw = make_switch(responses={
        "show ip interface brief": "Interface  IP-Address  Status\nVLAN1  10.0.0.1  up",
        re.compile(r"^show mac "): "Mac Address Table\n0000.1111.2222  VLAN1  Gi0/1",
    })
    _connect(manager, sw)
    brief = manager.execute_command(HOST, "show ip interface brief", port=sw.port)
    assert "10.0.0.1" in brief and "VLAN1" in brief
    # Regex-keyed response: any "show mac ..." command maps to the table.
    macs = manager.execute_command(HOST, "show mac address-table", port=sw.port)
    assert "0000.1111.2222" in macs


def test_unknown_command_is_a_device_error(manager, make_switch):
    """An unmapped command yields the caret/``Unknown command`` the manager flags."""
    sw = make_switch()
    _connect(manager, sw)
    out = manager.execute_command(HOST, "show interface status", port=sw.port)
    assert "Unknown command" in out
    assert "[device-mcp] ERROR:" in out
    # The Cisco->BDCOM dialect hint rides along on a rejected command.
    assert "hint:" in out
    assert "now: Switch# (enable)" in out


def test_run_commands_sequential_exec(manager, make_switch):
    sw = make_switch(responses={"show clock": "12:00:00", "show version": "v2.2.0F"})
    _connect(manager, sw)
    out = manager.run_commands(HOST, ["show version", "show clock"], port=sw.port)
    # Lean raw output: no per-command footer, but both results and the prompt are present.
    assert "[device-mcp]" not in out
    assert "v2.2.0F" in out and "12:00:00" in out
    assert out.count("Switch#") >= 2  # each command echoes back at the prompt
    assert sw.commands_seen[-2:] == ["show version", "show clock"]


# ----------------------------------------------------------------- configure

def test_configure_block_applies_and_exits(manager, make_switch):
    """A config block enters config, applies each line, and exits via Ctrl-Z."""
    sw = make_switch()
    _connect(manager, sw)
    cmds = ["vlan 30", "exit", "interface g0/1", "exit"]
    out = manager.configure(HOST, cmds, port=sw.port)
    assert "applied 4 command(s)" in out
    assert "now: Switch# (enable)" in out  # ended back in enable, not stranded
    # The device actually saw config mode and every command in order.
    assert "config" in sw.commands_seen
    for c in cmds:
        assert c in sw.commands_seen


def test_configure_reports_submode_on_the_wire(manager, make_switch):
    """Entering ``interface g0/1`` drives the BDCOM ``Switch_config_g0/1#`` prompt."""
    sw = make_switch()
    _connect(manager, sw)
    out = manager.execute_command(HOST, "interface g0/1", mode="config", port=sw.port)
    # execute_command(mode="config") leaves us in the sub-mode; the footer names it.
    assert "config: interface g0/1" in out, out


# ----------------------------------------------------------------- help

def test_get_help_parses_options(manager, make_switch):
    """A real ``?`` round-trip returns the device help, parsed into option tokens.

    (We assert on the parsed options - the feature under test - rather than the
    footer's session label, which on a just-connected device still has the login
    banner inside its recent-console window.)
    """
    sw = make_switch()
    _connect(manager, sw)
    out = manager.get_help(HOST, "show ", port=sw.port)
    assert "interface" in out and "ip" in out
    assert "[device-mcp] options: interface, ip" in out


def test_get_help_custom_listing(manager, make_switch):
    """A test can script the help body for a specific prefix."""
    sw = make_switch(responses={
        ("help_enable", "show ip"): "  route                      -- Routing table\n"
                             "  ospf                       -- OSPF status",
    })
    _connect(manager, sw)
    out = manager.get_help(HOST, "show ip ", port=sw.port)
    assert "route" in out and "ospf" in out
    assert "options: route, ospf" in out


# ------------------------------------------------------- interactive confirm

def _reboot_switch(make_switch):
    return make_switch(confirmations={"reboot": {
        "prompt": "Do you want to reboot the Switch(y/n)?",
        "answers": {"y": "System is rebooting...", "n": "Reboot canceled."},
    }})


def test_confirmation_with_plain_pattern_declines(manager, make_switch):
    """Decline a reboot's (y/n) over the real interactive path.

    The pattern is the plain substring '(y/n)' — valid JSON with no backslash escapes
    (the escaped form '\\(y/n\\)' is invalid JSON and is what tripped callers up). It is
    matched as a substring against the device's "...(y/n)?", and the answer 'n' is then
    sent and its aftermath captured.
    """
    sw = _reboot_switch(make_switch)
    _connect(manager, sw)
    out = manager.execute_command(HOST, "reboot", expect_regex="(y/n)", answer="n",
                                  port=sw.port)
    assert "Reboot canceled" in out
    assert "rebooting" not in out.lower()
    # Declining is a clean outcome -> lean raw output, no footer, ends at the prompt.
    assert "[device-mcp]" not in out
    assert out.rstrip().endswith("Switch#"), out


# ------------------------------------------------------------ session state

def test_session_termination_is_reported(manager, make_switch):
    """A command that drops the CLI back to login is flagged in the footer."""
    sw = make_switch(responses={"write memory": "Building configuration..."})
    _connect(manager, sw)
    sw.terminate = True  # next command lands at the login prompt
    out = manager.execute_command(HOST, "write memory", port=sw.port)
    # A terminated session is a problem, so the footer IS appended even in raw mode.
    assert "device returned to login prompt" in out
    assert "now: awaiting_login" in out


# ----------------------------------------------------------------- dialect

def test_cisco_dialect_prompts_and_config(manager, make_switch):
    """The Cisco dialect uses ``(config)#``/``end`` and stays in user mode at login."""
    sw = make_switch(dialect="cisco", responses={"show version": "Cisco IOS Software"})
    res = manager.connect(host=HOST, username="admin", password="admin",
                          device_type="cisco_ios", protocol="telnet", port=sw.port)
    assert res["success"] is True, res
    out = manager.execute_command(HOST, "show version", port=sw.port)
    assert "Cisco IOS Software" in out
    # cisco_ios has no auto-enable in session_preparation, so we sit in user mode:
    # lean raw output ends at the user-mode '>' prompt (no footer on a clean run).
    assert "[device-mcp]" not in out
    assert out.rstrip().endswith("Switch>"), out
    cfg = manager.configure(HOST, ["interface Gig0/1", "exit"], port=sw.port)
    assert "applied 2 command(s)" in cfg
    assert "configure terminal" in sw.commands_seen
