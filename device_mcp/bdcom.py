"""Custom netmiko driver for BDCOM switches.

netmiko (4.7.0) has no BDCOM platform. BDCOM uses a Cisco-IOS-*like* CLI, but its
global-configuration mode differs enough that the stock ``cisco_ios`` driver
mishandles it (verified against the vendor "BDCOM Switch L3" documentation):

    * Global config is entered with ``config`` (not ``configure terminal``).
    * The config prompt is ``Switch_config#`` / ``Switch_config_f0/1#`` - there are
      no ``(config)#`` parentheses, so netmiko's default ``)#`` detection fails.
    * Config mode is left with Ctrl-Z (``\\x1a``), ``exit`` or ``quit`` - there is
      no ``end`` command.

Everything else (``enable`` privileged mode, ``terminal length 0`` paging, the
``enable password``, and both SSH and Telnet transports) matches Cisco IOS, so we
only override the config-mode trio and inherit the rest from the Cisco IOS driver.
"""

from __future__ import annotations

from netmiko.cisco.cisco_ios import CiscoIosBase, CiscoIosTelnet


class _BdcomMixin:
    """BDCOM config-mode overrides, mixed over a Cisco IOS connection class."""

    def check_config_mode(
        self, check_string: str = "_config", pattern: str = "", force_regex: bool = False
    ) -> bool:
        # "_config" is present in both Switch_config# and Switch_config_f0/1#, and
        # absent from user (Switch>) and management (Switch#) prompts.
        return super().check_config_mode(
            check_string=check_string, pattern=pattern, force_regex=force_regex
        )

    def config_mode(
        self, config_command: str = "config", pattern: str = "", re_flags: int = 0
    ) -> str:
        return super().config_mode(
            config_command=config_command, pattern=pattern, re_flags=re_flags
        )

    def exit_config_mode(self, exit_config: str = "\x1a", pattern: str = r"#.*") -> str:
        # Ctrl-Z returns directly to management mode from any config sub-level.
        return super().exit_config_mode(exit_config=exit_config, pattern=pattern)


class BdcomSSH(_BdcomMixin, CiscoIosBase):
    """BDCOM switch over SSH."""


class BdcomTelnet(_BdcomMixin, CiscoIosTelnet):
    """BDCOM switch over Telnet."""
