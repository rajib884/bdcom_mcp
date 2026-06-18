"""Custom netmiko driver for BDCOM switches.

netmiko (4.7.0) has no BDCOM platform. BDCOM uses a Cisco-IOS-*like* CLI, but it
differs enough that the stock ``cisco_ios`` driver mishandles it (verified against
the vendor "BDCOM Switch L3" documentation and a real S3954 switch):

    * Global config is entered with ``config`` (not ``configure terminal``).
    * The config prompt is ``Switch_config#`` / ``Switch_config_f0/1#`` - there are
      no ``(config)#`` parentheses, so netmiko's default ``)#`` detection fails.
    * Config mode is left with Ctrl-Z (``\\x1a``), ``exit`` or ``quit`` - there is
      no ``end`` command, and the device does not echo the Ctrl-Z byte.
    * ``terminal width`` is unsupported, and ``terminal length 0`` (disable paging)
      is only accepted in privileged mode - the stock Cisco session prep runs both
      in user mode at login, spamming "Unknown command" and, worse, leaving paging
      ON so later large ``show`` output stalls on ``--More--`` and desyncs.
    * Privileged mode is left with ``exit`` (``Switch#`` -> ``Switch>``), not the
      Cisco default ``disable``.

Enable (``enable``) and both SSH and Telnet transports match Cisco IOS, so we
override only the config-mode trio, session preparation, terminal-width, and
enable-exit, and inherit the rest from the Cisco IOS driver.
"""

from __future__ import annotations

from typing import Any

from netmiko.cisco.cisco_ios import CiscoIosBase, CiscoIosTelnet


class _BdcomMixin:
    """BDCOM CLI overrides, mixed over a Cisco IOS connection class."""

    def session_preparation(self) -> None:
        # BDCOM 'terminal width' is unsupported and 'terminal length 0' is only
        # valid in privileged mode. The stock Cisco prep runs both in whatever
        # mode login lands (user mode here) -> 'Unknown command' spam AND paging
        # left ON (later 'show' output stalls on '--More--' and desyncs). So:
        # settle the channel, set the prompt, enter enable (BDCOM usually needs
        # no enable password), then disable paging where it is actually accepted.
        self._test_channel_read(pattern=r"[>#]")
        self.set_base_prompt()
        try:
            if not self.check_enable_mode():
                self.enable()
            self.disable_paging()
        except Exception:  # noqa: BLE001 - best effort
            # If enable needs a password we weren't given, paging stays on; large
            # user-mode 'show' output may page, but the connection still works.
            pass

    def set_terminal_width(self, *args: Any, **kwargs: Any) -> str:
        # BDCOM has no 'terminal width' command; never send it.
        return ""

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
        # Can't delegate to the base implementation: it first waits for the
        # device to echo back the literal exit_config string before checking
        # `pattern`, but this device does not echo the Ctrl-Z control byte,
        # so that wait always times out.
        output = ""
        if self.check_config_mode():
            self.write_channel(self.normalize_cmd(exit_config))
            output += self.read_until_pattern(pattern=pattern)
            if self.check_config_mode():
                raise ValueError("Failed to exit configuration mode")
        return output

    def exit_enable_mode(self, exit_command: str = "exit") -> str:
        # BDCOM leaves privileged mode with 'exit' (Switch# -> Switch>); the Cisco
        # default 'disable' is untested on this platform.
        return super().exit_enable_mode(exit_command=exit_command)


class BdcomSSH(_BdcomMixin, CiscoIosBase):
    """BDCOM switch over SSH."""


class BdcomTelnet(_BdcomMixin, CiscoIosTelnet):
    """BDCOM switch over Telnet."""
