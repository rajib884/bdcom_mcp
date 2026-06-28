"""Manual, ad-hoc exploratory harness for the connection manager against the emulator.

This is NOT a pytest test (the automated suite lives in ``tests/``). It spins up a
``SwitchEmulator`` and walks ``DeviceConnectionManager`` through a realistic session so
you can eyeball the output by hand. Run it directly:

    python my_tester.py

It is guarded by ``if __name__ == "__main__":`` so importing it has no side effects.
"""

import time
import re
from pprint import pprint

from device_mcp.connection import DeviceConnectionManager
from tests.switch_emulator import SwitchEmulator


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
            device_type="bdcom",
            protocol="telnet"
        ))
        print("=====")

        print("\n--- get history ---")
        print(mgr.get_console_history("127.0.0.1", port=sw.port, limit=1000))
        print("=====")
    finally:
        sw.stop()


if __name__ == "__main__":
    main()
