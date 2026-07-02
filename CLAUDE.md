# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A Python / FastMCP server ("device-mcp") that lets AI assistants manage network devices (Cisco IOS, BDCOM, any netmiko platform) over SSH/Telnet. Transport is [netmiko](https://github.com/ktbyers/netmiko); BDCOM gets a small custom driver. Requires Python >= 3.10; the repo has a venv at `.venv` (use `.venv/bin/python` / `.venv/bin/pytest` if not activated).

## Commands

```bash
pip install -e ".[dev]"        # install + pytest/pytest-timeout

python -m device_mcp.server    # run the MCP server over stdio (also: device-mcp script)

python smoke_test.py           # offline smoke suite: pure helpers + fakes, no sockets, <1s
pytest                         # emulator integration tests in tests/ (real telnet over loopback)
pytest tests/test_emulator_integration.py::test_connect_enable_password_flow   # single test

python my_tester.py            # manual eyeball harness (NOT pytest) against the emulator
```

Run **both** suites (`python smoke_test.py` and `pytest`) before considering a change done. Each pytest test performs a real netmiko telnet login (~9s); `pyproject.toml` sets a 30s per-test timeout so a hang fails one test instead of stalling the run.

## Architecture

Three layers, plus a test emulator:

- **`device_mcp/server.py`** — thin FastMCP tool layer. Each `@mcp.tool` wraps a method on a module-level `DeviceConnectionManager` and returns a string; exceptions are caught and returned as `"Error: ..."` text (AI-friendly, never raised to the client). Three file-transfer/firmware tools (`transfer_file`, `upgrade_firmware`, `recover_firmware`) are **commented out here but their manager methods still exist and are tested** — re-enabling means uncommenting the wrapper (`enter_monitor_mode` is live).
- **`device_mcp/connection.py`** — the core (~1800 lines). `DeviceConnectionManager` holds long-lived connections keyed by **target = `host:port`** (several devices behind one console-server IP on different ports are independent connections; `port` may be omitted in tool calls only when a host has exactly one connection — see `_resolve`). One `RLock` guards the connection map and the non-reentrant netmiko channels; a per-connection daemon thread polls idle channels (non-blocking lock acquire) so unsolicited output reaches the audit log.
- **`device_mcp/bdcom.py`** — custom netmiko driver: `_BdcomMixin` over the Cisco IOS classes. BDCOM enters config with `config` (prompt `Switch_config#`, no parens), exits config with Ctrl-Z (not echoed, so `exit_config_mode` can't delegate to netmiko's echo-wait), has no `terminal width`, and accepts `terminal length 0` only in enable mode — so `session_preparation` elevates to enable *before* disabling paging (stock Cisco prep would leave paging on and desync later big `show` output). To add another vendor netmiko lacks: write a similar driver module and register it in `_CUSTOM_DRIVERS` in `connection.py`.
- **`tests/switch_emulator.py`** — in-process, stdlib-only Telnet switch emulator speaking `bdcom` and `cisco` dialects (login, enable password, paging/`--More--`, config sub-modes, `?` help, `(y/n)` confirmations, the hidden Ctrl-] boot menu and Ctrl-P monitor interrupt). Tests script it via a `responses` map (exact string, compiled regex, or callable → output).

### Cross-cutting concepts (read these before touching command execution)

- **Result footer.** Every command tool appends one `[device-mcp] <status> | now: <prompt> (<mode>)` line built by `_footer()`. It distinguishes a **device error** (command rejected; caret `^` parsed into `near '<token>'`), a **FAILED** transport error, and **SESSION_TERMINATED** (device dropped to a login prompt, e.g. BDCOM `write memory`). Session state is the closed `SessionState` literal set — abnormal states are reported by name so raw read-buffer text never leaks in as a "mode". Exception: raw mode returns **no footer on clean success** (tests assert `[device-mcp]` is absent).
- **Prompt/state comes from the console ring, not probes.** `_describe_session` classifies the last lines of the `_ConsoleRingLog` instead of calling `find_prompt()`, which spams the channel with CR echoes; only an inconclusive ring falls back to an active probe. `_switch_mode` in `auto` similarly trusts the tracked `conn.current_mode`. Preserve this — the prompt-probe noise was deliberately engineered out.
- **Execution modes.** `raw` (the default, tool and manager alike) drives the channel directly for prompts netmiko doesn't know (bootloader `monitor#`); `auto` runs at the current privilege level without downgrading; `user`/`enable` force a level; `config` applies the whole command list as one atomic block via `send_config_set` with `error_pattern` (rejected line reported, no silent partial config).
- **Console/audit logging.** netmiko's `session_log` writes into `_ConsoleRingLog` (bounded in-memory ring, teed unbounded to `./logs/<host>_<port>_<ts>.log`; dir overridable via `DEVICE_MCP_LOG_DIR`). `session_log_record_writes` stays off — the CLI echoes input, so recording writes would double-log every command. Logs can contain plaintext credentials; `logs/` is git-ignored, never commit its contents.
- **Dialect hints.** `_DIALECT_HINTS` maps known Cisco-isms BDCOM rejects (e.g. `write memory`, `switchport access vlan`, `end`) to the BDCOM equivalent, surfaced as `hint:` in the footer on a device error or terminated session.
- **Recovery paths.** `connect_device(recovery=True)` opens the transport without login/session-prep for a device stuck below a usable prompt (telnet only skips auth); `relogin_device` / `auto_relogin` re-authenticate on the live channel after an idle-timeout logout instead of reconnecting; `enter_monitor_mode` force-reboots (hidden Ctrl-] menu, fallback `reboot`+`y`) and sends a Ctrl-P burst after `RTC Test` to land in the bootloader `monitor#` shell.

### Testing conventions

- Pure helpers in `connection.py` (`_classify_prompt`, `_describe_mode`, `_detect_device_error`, `_dialect_hint`, `_parse_help_tokens`, `_target`, `resolve_platform`, `_ConsoleRingLog`) are deliberately side-effect-free so `smoke_test.py` covers them offline. Keep new parsing/formatting logic in that style.
- `smoke_test.py` asserts the exact registered tool set (`EXPECTED_TOOLS`) — adding, removing, or re-enabling an `@mcp.tool` requires updating it.
- Integration tests get fixtures from `tests/conftest.py`: `manager` (real `DeviceConnectionManager`, auto-disconnected) and `make_switch` (started/stopped `SwitchEmulator` factory). Audit logs are redirected to a tmp dir via an autouse fixture. Pin device output through the emulator's `responses` map and assert on what the manager returns; see existing tests for the pattern.

## Gotchas

- README.md is bilingual (English + 中文) — user-visible feature changes should update both sections.
