"""Manual, ad-hoc exploratory harness for the connection manager against the emulator.

This is NOT a pytest test (the automated suite lives in ``tests/``). It spins up
``SwitchEmulator`` instances and walks ``DeviceConnectionManager`` through realistic
sessions so you can eyeball the actual input/output by hand instead of guessing from
the assertions in ``tests/test_emulator_integration.py``. Run it directly:

    python my_tester.py

Each ``demo_*`` function below mirrors one scenario from the pytest suite (same name,
same emulator setup) but prints the real request/response instead of asserting on it.
Guarded by ``if __name__ == "__main__":`` so importing it has no side effects.
"""

import time
import re
from pprint import pprint

from device_mcp.connection import DeviceConnectionManager
from tests.switch_emulator import SwitchEmulator

HOST = "127.0.0.1"


def dynamic_show_users(cmd: str) -> str:
    """Simulates a 'show users' command that returns a timestamp."""
    return f"Line     User      Host(s)              Idle\r\n* 0     admin     idle                 00:00:00\r\n(Generated at {time.strftime('%H:%M:%S')})"


def main() -> None:
    # ------------------------------------------------------------
    # 1. Define dynamic responses and help contexts
    # ------------------------------------------------------------
    responses = {
        # exact match
        "show version": "BDCOM Software, Version 2.2.0F, RELEASE SOFTWARE",
        # regex match – any "show interface ..." returns a generic counter
        re.compile(r"show interface\s+\S+"): "GigabitEthernet0/1 is up, line protocol is up\r\n  ...",
        # callable dynamic response
        "show users": dynamic_show_users,
        # help context for enable mode
        ("help_enable", ""): "  show           -- Show running system information\r\n  configure      -- Enter configuration mode",
    }

    confirmations = {
        "reboot": {
            "prompt": "Do you want to reboot the Switch(y/n)?",
            "answers": {
                "y": "System is rebooting...\nRTC Test.......OK",
                "n": "Reboot canceled."
            },
            "default": "Reboot canceled."
        }
    }

    sw = SwitchEmulator(
        username="admin",
        password="admin",
        enable_password="secret",
        dialect="bdcom",
        responses=responses,
        confirmations=confirmations,
        banner="\n\nWelcome to BDCOM Switch\n\n"
    )
    sw.start()

    mgr = DeviceConnectionManager()
    try:
        print("\n--- Connection List ---")
        pprint(mgr.list_connections())
        print("=====")

        print("\n--- disconnect ---")
        pprint(mgr.disconnect("127.0.0.1", port=sw.port))
        print("=====")

        print("\n--- connect ---")
        pprint(mgr.connect(
            host="127.0.0.1",
            port=sw.port,
            username="admin",
            password="admin",
            enable_password="secret",
            device_type="bdcom",
            protocol="telnet"
        ))
        print("=====")

        print("\n--- connect again ---")
        pprint(mgr.connect(
            host="127.0.0.1",
            port=sw.port,
            username="admin",
            password="admin",
            enable_password="secret",
            device_type="bdcom",
            protocol="telnet"
        ))
        print("=====")

        print("\n--- show version ---")
        print(mgr.run_commands("127.0.0.1", ["show version"], port=sw.port))
        print("=====")

        print("\n--- show interface g0/1 (regex) ---")
        print(mgr.run_commands("127.0.0.1", ["show interface g0/1"], port=sw.port))
        print("=====")

        print("\n--- both show version and show interface g0/1 (regex) ---")
        print(mgr.run_commands("127.0.0.1", ["show version", "show interface g0/1"], port=sw.port))
        print("=====")

        print("\n--- reboot y ---")
        print(mgr.run_commands("127.0.0.1", ["reboot"], port=sw.port, expect_regex="(y/n)", answer="y"))
        print("=====")

        print("\n--- monitor mode ---")
        print(mgr.enter_monitor_mode("127.0.0.1", port=sw.port, timeout=5))
        print("=====")

        print("\n--- config ---")
        print(mgr.run_commands("127.0.0.1", ["config"], port=sw.port))
        print("=====")

        print("\n--- exit ---")
        print(mgr.run_commands("127.0.0.1", ["\x1a"], port=sw.port))
        print("=====")

        print("\n--- show users ---")
        print(mgr.run_commands("127.0.0.1", ["show users"], port=sw.port))
        print("=====")

        print("\n--- exit x2 ---")
        print(mgr.run_commands("127.0.0.1", ["exit", "exit"], port=sw.port))
        print("=====")

        print("\n--- show users ---")
        print(mgr.run_commands("127.0.0.1", ["show users"], port=sw.port))
        print("=====")

        print("\n--- get history ---")
        print(mgr.get_console_history("127.0.0.1", port=sw.port, limit=1000))
        print("=====")

        print("\n--- list connection ---")
        pprint(mgr.list_connections())
        print("=====")

        print("\n--- disconnect ---")
        pprint(mgr.disconnect("127.0.0.1", port=sw.port))
        print("=====")

        print("\n--- list connection ---")
        pprint(mgr.list_connections())
        print("=====")

        print("\n--- connect again ---")
        pprint(mgr.connect(
            host="127.0.0.1",
            port=sw.port,
            username="admin",
            password="admin",
            enable_password="secret",
            device_type="bdcom",
            protocol="telnet"
        ))
        print("=====")

        print("\n--- get history ---")
        print(mgr.get_console_history("127.0.0.1", port=sw.port, limit=1000))
        print("=====")
    finally:
        sw.stop()


# ======================================================================
# Scenario demos below - one per test in tests/test_emulator_integration.py.
# Each is self-contained (own emulator + own manager) so it can be read and
# run in isolation. They print real request/response pairs instead of
# asserting on them.
# ======================================================================


def demo_connect_reaches_enable_and_disables_paging() -> None:
    """A BDCOM Telnet connect logs in, elevates to enable, and turns paging off."""
    sw = SwitchEmulator(username="admin", password="admin", dialect="bdcom").start()
    mgr = DeviceConnectionManager()
    try:
        print("--- connect(device_type='bdcom', protocol='telnet') ---")
        pprint(mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                           device_type="bdcom", protocol="telnet"))
        print("commands the device actually saw:", sw.commands_seen)
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_connect_unsecured_no_credentials() -> None:
    """A port with no username/password lands straight at the prompt and works."""
    sw = SwitchEmulator(username=None, password=None,
                        responses={"show version": "BDCOM Software, Version 2.2.0F"}).start()
    mgr = DeviceConnectionManager()
    try:
        print("--- connect(no credentials) ---")
        pprint(mgr.connect(host=HOST, device_type="bdcom", protocol="telnet", port=sw.port))
        print("--- execute_command('show version') ---")
        print(mgr.execute_command(HOST, "show version", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_connect_auth_failure() -> None:
    """Wrong credentials -> connect fails and surfaces the device's last output."""
    sw = SwitchEmulator(password="s3cret").start()
    mgr = DeviceConnectionManager()
    try:
        print("--- connect(password='wrong') ---")
        pprint(mgr.connect(host=HOST, port=sw.port, username="admin", password="wrong",
                           device_type="bdcom", protocol="telnet"))
    finally:
        sw.stop()


def demo_connect_enable_password_flow() -> None:
    """An enable secret is sent at the device's Password: prompt to reach enable."""
    sw = SwitchEmulator(enable_password="enpw",
                        responses={"show clock": "12:00:00 UTC Mon Jun 25 2026"}).start()
    mgr = DeviceConnectionManager()
    try:
        print("--- connect(enable_password='enpw') ---")
        pprint(mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                           enable_password="enpw", device_type="bdcom", protocol="telnet"))
        print("--- execute_command('show clock', mode='enable') ---")
        print(mgr.execute_command(HOST, "show clock", mode="enable", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_execute_command_lean_output() -> None:
    """A clean raw command returns just the device output ending at the prompt - no footer."""
    sw = SwitchEmulator(responses={
        "show version": "BDCOM(tm) S3954, Version 2.2.0F\nuptime 5 days",
    }).start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        print("--- execute_command('show version') ---")
        print(mgr.execute_command(HOST, "show version", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_execute_command_per_command_output() -> None:
    """The emulator returns exactly the mapped output for each distinct command (incl. regex-keyed)."""
    sw = SwitchEmulator(responses={
        "show ip interface brief": "Interface  IP-Address  Status\nVLAN1  10.0.0.1  up",
        re.compile(r"^show mac "): "Mac Address Table\n0000.1111.2222  VLAN1  Gi0/1",
    }).start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        print("--- execute_command('show ip interface brief') ---")
        print(mgr.execute_command(HOST, "show ip interface brief", port=sw.port))
        print("--- execute_command('show mac address-table') (regex-keyed match) ---")
        print(mgr.execute_command(HOST, "show mac address-table", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_unknown_command_error() -> None:
    """An unmapped command yields the caret/'Unknown command' the manager flags, with a dialect hint."""
    sw = SwitchEmulator().start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        print("--- execute_command('show interface status') (Cisco-style, unmapped on BDCOM) ---")
        print(mgr.execute_command(HOST, "show interface status", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_run_commands_sequential() -> None:
    """run_commands executes multiple commands sequentially and concatenates lean output."""
    sw = SwitchEmulator(responses={"show clock": "12:00:00", "show version": "v2.2.0F"}).start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        print("--- run_commands(['show version', 'show clock']) ---")
        print(mgr.run_commands(HOST, ["show version", "show clock"], port=sw.port))
        print("commands the device actually saw (last 2):", sw.commands_seen[-2:])
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_configure_block() -> None:
    """A config block enters config, applies each line, and exits via Ctrl-Z."""
    sw = SwitchEmulator().start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        cmds = ["vlan 30", "exit", "interface g0/1", "exit"]
        print(f"--- configure({cmds}) ---")
        print(mgr.configure(HOST, cmds, port=sw.port))
        print("commands the device actually saw:", sw.commands_seen)
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_configure_submode() -> None:
    """Entering 'interface g0/1' drives the BDCOM 'Switch_config_g0/1#' prompt."""
    sw = SwitchEmulator().start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        print("--- execute_command('interface g0/1', mode='config') ---")
        print(mgr.execute_command(HOST, "interface g0/1", mode="config", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_get_help_parses_options() -> None:
    """A real '?' round-trip returns the device help, parsed into option tokens."""
    sw = SwitchEmulator().start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        print("--- get_help('show ') ---")
        print(mgr.get_help(HOST, "show ", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_get_help_custom_listing() -> None:
    """A test can script the help body for a specific prefix."""
    sw = SwitchEmulator(responses={
        ("help_enable", "show ip"): "  route                      -- Routing table\n"
                             "  ospf                       -- OSPF status",
    }).start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        print("--- get_help('show ip ') ---")
        print(mgr.get_help(HOST, "show ip ", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_confirmation_decline() -> None:
    """Decline a reboot's (y/n) prompt over the real interactive path."""
    sw = SwitchEmulator(confirmations={"reboot": {
        "prompt": "Do you want to reboot the Switch(y/n)?",
        "answers": {"y": "System is rebooting...", "n": "Reboot canceled."},
    }}).start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        print("--- execute_command('reboot', expect_regex='(y/n)', answer='n') ---")
        print(mgr.execute_command(HOST, "reboot", expect_regex="(y/n)", answer="n", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_session_termination() -> None:
    """A command that drops the CLI back to login is flagged in the footer."""
    sw = SwitchEmulator(responses={"write memory": "Building configuration..."}).start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        sw.terminate = True  # next command lands at the login prompt, then drops the socket
        print("--- execute_command('write memory') while sw.terminate=True ---")
        print(mgr.execute_command(HOST, "write memory", port=sw.port))
    finally:
        sw.stop()


def demo_recovery_connect() -> None:
    """recovery=True opens the transport without login or session_preparation."""
    sw = SwitchEmulator(username=None, password=None,
                        responses={"show version": "BDCOM Software, Version 2.2.0F"}).start()
    mgr = DeviceConnectionManager()
    try:
        print("--- connect(recovery=True) ---")
        pprint(mgr.connect(host=HOST, device_type="bdcom", protocol="telnet",
                           port=sw.port, recovery=True))
        print("commands the device saw (should be empty - no enable/paging):", sw.commands_seen)
        print("--- execute_command('show version') over the raw recovery channel ---")
        print(mgr.execute_command(HOST, "show version", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_relogin_after_idle_drop() -> None:
    """An idle-timeout drop to the login prompt is recovered without a reconnect."""
    sw = SwitchEmulator(responses={"show clock": "12:00:00"}).start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet")
        sw.drop_to_login = True  # next command lands back at the login prompt, socket stays open
        print("--- execute_command('show clock', expect_regex='Username:') after idle drop ---")
        print(mgr.execute_command(HOST, "show clock", expect_regex="Username:", port=sw.port))
        print("--- relogin() ---")
        print(mgr.relogin(HOST, port=sw.port))
        print("--- execute_command('show clock') again, now that we're back in ---")
        print(mgr.execute_command(HOST, "show clock", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_auto_relogin() -> None:
    """With auto_relogin=True, a command after an idle drop re-auths and runs - no explicit relogin call."""
    sw = SwitchEmulator(responses={"show clock": "12:00:00"}).start()
    mgr = DeviceConnectionManager()
    try:
        mgr.connect(host=HOST, port=sw.port, username="admin", password="admin",
                    device_type="bdcom", protocol="telnet", auto_relogin=True)
        sw.drop_to_login = True
        print("--- execute_command('show clock', expect_regex='Username:') trips the drop ---")
        print(mgr.execute_command(HOST, "show clock", expect_regex="Username:", port=sw.port))
        print("--- execute_command('show clock') again: auto-relogin kicks in transparently ---")
        print(mgr.execute_command(HOST, "show clock", port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


def demo_cisco_dialect() -> None:
    """The Cisco dialect uses (config)#/end and stays in user mode at login."""
    sw = SwitchEmulator(dialect="cisco", responses={"show version": "Cisco IOS Software"}).start()
    mgr = DeviceConnectionManager()
    try:
        print("--- connect(device_type='cisco_ios') ---")
        pprint(mgr.connect(host=HOST, username="admin", password="admin",
                           device_type="cisco_ios", protocol="telnet", port=sw.port))
        print("--- execute_command('show version') ---")
        print(mgr.execute_command(HOST, "show version", port=sw.port))
        print("--- configure(['interface Gig0/1', 'exit']) ---")
        print(mgr.configure(HOST, ["interface Gig0/1", "exit"], port=sw.port))
    finally:
        mgr.disconnect(HOST, port=sw.port)
        sw.stop()


SCENARIOS = [
    demo_connect_reaches_enable_and_disables_paging,
    demo_connect_unsecured_no_credentials,
    demo_connect_auth_failure,
    demo_connect_enable_password_flow,
    demo_execute_command_lean_output,
    demo_execute_command_per_command_output,
    demo_unknown_command_error,
    demo_run_commands_sequential,
    demo_configure_block,
    demo_configure_submode,
    demo_get_help_parses_options,
    demo_get_help_custom_listing,
    demo_confirmation_decline,
    demo_session_termination,
    demo_recovery_connect,
    demo_relogin_after_idle_drop,
    demo_auto_relogin,
    demo_cisco_dialect,
]


def run_all_scenarios() -> None:
    """Run every demo_* scenario in turn, each against its own emulator + manager.

    Scenarios are independent (their own emulator/manager), so one raising doesn't
    stop the rest - the traceback is printed in place and the loop moves on.
    """
    for fn in SCENARIOS:
        title = fn.__name__.removeprefix("demo_").replace("_", " ")
        print(f"\n{'#' * 78}\n# {title}\n# {(fn.__doc__ or '').strip()}\n{'#' * 78}\n")
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
    run_all_scenarios()
