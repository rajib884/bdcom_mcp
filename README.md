# Device MCP Server

[English](#english) | [中文](#中文)

---

## English

A comprehensive MCP (Model Context Protocol) server for **network device**
management via SSH/Telnet. Execute commands and manage routers, switches, and
firewalls through AI assistants like Claude and Amazon Q. Works with **Cisco
IOS**, **BDCOM**, and any other [netmiko](https://github.com/ktbyers/netmiko)-supported
platform.

### ✨ Features

- **🔌 Dual Protocol Support**: Connect via SSH or Telnet
- **🧩 Multi-Vendor**: Cisco IOS (default), BDCOM, and 400+ netmiko platforms via a `device_type` selector
- **🔄 Persistent Connections**: Maintain long-lived connections for efficient command execution
- **🎯 Universal Command Execution**: Execute any device command through a single interface
- **🔐 Mode Management**: `raw` default drives the channel directly at the live prompt; `auto` runs at the current privilege level without downgrading; `user`/`enable`/`config` force a level
- **🧾 Self-describing results**: every command returns its raw output plus a `[device-mcp]` footer reporting device errors (vs transport failures) and where the CLI ended up — see [Result footer](#-result-footer)
- **🧱 One-or-many commands**: `execute_command` takes a command **list**; `mode=config` applies it as one config block (sub-mode nesting handled; a rejected line stops the block and is reported — lines already applied stay), other modes run them in order
- **🌐 Multi-Device Support**: Manage many devices at once — including several behind **one IP on different ports** (console/terminal servers), addressed as `host:port`
- **✅ Interactive Confirmations**: Answer `(y/n)` / `[confirm]` prompts (e.g. `reboot`, `delete startup-config`) via `expect_regex` + `answer`
- **📜 Console Diagnostics & audit logs**: live raw-I/O history (`get_console_history`) plus an always-on per-connection log file under `./logs` (`DEVICE_MCP_LOG_DIR` to relocate) capturing the full session for later audit
- **❓ Inline Help**: Query CLI `?` help with `get_help` (parsed `options:` footer)
- **💡 Dialect hints**: a rejected/aborted command that's a known Cisco-ism gets a `hint:` with the BDCOM equivalent; a dropped session is reported as `SESSION_TERMINATED`
- **🛠 Raw mode**: `mode=raw` (the default) drives the channel directly for prompts netmiko doesn't know — e.g. rebooting from the bootloader `monitor#` shell
- **🤖 AI-Friendly**: Natural language command translation through AI assistants
- **📊 Connection Monitoring**: Track active connections and their status

> **Implementation:** Python / [FastMCP](https://github.com/jlowin/fastmcp) server.
> SSH/Telnet transport is handled by [netmiko](https://github.com/ktbyers/netmiko),
> which is purpose-built for network devices (robust prompt detection, paging, and
> user/enable/config mode handling). BDCOM uses a small custom driver — see
> [BDCOM notes](#-bdcom-notes).

### 🚀 Quick Start

1. **Clone and Install**
   ```bash
   git clone https://github.com/rajib884/bdcom_mcp.git device-mcp
   cd device-mcp
   python -m venv .venv
   # Windows:  .venv\Scripts\activate
   # macOS/Linux:  source .venv/bin/activate
   pip install -e .          # or: pip install -r requirements.txt
   ```

2. **Run / Verify**
   ```bash
   python -m device_mcp.server   # serves over stdio
   ```

3. **Configure MCP Client**

   Add to your MCP configuration (e.g., Claude Desktop). Use the Python
   interpreter from the virtual environment you created above:
   ```json
   {
     "mcpServers": {
       "device-mcp": {
         "command": "/path/to/device-mcp/.venv/bin/python",
         "args": ["-m", "device_mcp.server"]
       }
     }
   }
   ```
   On Windows, use `\path\to\device-mcp\.venv\Scripts\python.exe`. If you
   installed the package (`pip install -e .`), you can instead point `command`
   at the generated `device-mcp` executable with no `args`.

### 🛠 Available Tools

#### `connect_device`
Establish a connection to a network device.

**Parameters:**
- `host` (required): IP address or hostname
- `username` (optional): Authentication username (omit for unsecured consoles)
- `password` (optional): Authentication password (omit for unsecured consoles)
- `device_type` (optional): `cisco_ios` (default), `bdcom`, or any netmiko device type
- `protocol` (optional): `ssh` or `telnet` (default: `ssh`)
- `port` (optional): Custom port number (default 22 SSH / 23 Telnet). The connection
  is keyed by `host:port`, so several devices behind one IP stay independent.
- `enable_password` (optional): Enable password for privileged mode. Many devices
  (e.g. BDCOM with `aaa authentication enable default none`) need **none** — leave
  it unset and `enable` mode still works.

**Example (Cisco):**
```json
{
  "host": "192.168.1.1",
  "username": "admin",
  "password": "password123",
  "device_type": "cisco_ios",
  "protocol": "ssh",
  "enable_password": "enable123"
}
```

**Example (BDCOM):**
```json
{
  "host": "192.168.1.2",
  "username": "admin",
  "password": "password123",
  "device_type": "bdcom",
  "enable_password": "enable123"
}
```

**Audit log:** every connection's full device I/O (from the login banner through
disconnect) is written to `./logs/<host>_<port>_<UTC-timestamp>.log` — the directory
is overridable with the `DEVICE_MCP_LOG_DIR` env var. The server also polls idle
connections, so unsolicited console output (reboot progress, link events, login
prompts) is captured even when no tool call is actively executing. The path is
returned as `log_file` in the connect result (and in `list_connections`). A failed
connection still leaves its partial transcript on disk (handy for diagnosing a bad
login).
> ⚠️ These logs capture raw I/O and can contain plaintext credentials — treat the
> `logs/` directory as sensitive (it is git-ignored).

#### `execute_command`
Run **one or more** commands on a connected device. Returns each command's **raw
output** plus a one-line `[device-mcp]` footer reporting any device error and where
the CLI ended up (see [Result footer](#-result-footer)).

**Parameters:**
- `host` (required): Target device IP/hostname
- `commands` (required): Ordered list of commands. A single command is a one-item
  list, e.g. `["show version"]`.
- `mode` (optional): `raw` (default), `auto`, `user`, `enable`, or `config`.
  - `auto` runs at the current privilege level **without downgrading** (so `show`
    works on BDCOM, which connects in enable); `user`/`enable` force that level.
  - **`config`** applies the whole list as one config block (enters/exits config
    mode, handles sub-mode prompts like `Switch_config_vlan30#`). A rejected line
    stops the block and the footer reports which one failed; lines **before** it
    are already applied and stay applied (no rollback), and config mode is exited
    so the session isn't left stranded. Send a sub-mode `exit` as its own list
    item when moving between contexts.
  - **`raw`** (the default) bypasses mode switching and prompt detection, driving
    the channel directly — needed at prompts netmiko doesn't know, notably the
    bootloader `monitor#` shell (where a normal `reboot` waits for the device's
    usual `Switch.*` prompt and is never sent).
- `expect_regex` (optional): Regex for an interactive confirmation prompt (honored
  only when a single command is given)
- `answer` (optional): Reply to send when `expect_regex` matches
- `port` (optional): Required only when several devices share an IP

If the session is sitting at a **login prompt** (idle logout, reboot), the command
is **not** typed into the `Username:` field: with `auto_relogin` (the connect
default) one re-login attempt is made first, otherwise the footer says to call
`relogin_device`. And if `expect_regex` never matches but the device is back at a
prompt, the real output is returned with a `never matched` note instead of a bare
`ReadTimeout`.

**Example (single show):**
```json
{ "host": "192.168.1.1", "commands": ["show version"] }
```

**Config block (BDCOM VLAN + access port):**
```json
{
  "host": "192.168.100.34",
  "port": 10003,
  "mode": "config",
  "commands": [
    "vlan 30", "exit",
    "interface GigaEthernet0/1",
    "switchport mode access", "switchport pvid 30", "exit"
  ]
}
```

**Interactive (BDCOM reboot):**
```json
{
  "host": "192.168.100.34",
  "port": 10003,
  "commands": ["reboot"],
  "mode": "enable",
  "expect_regex": "\\(y/n\\)",
  "answer": "y"
}
```
To reboot from the bootloader `monitor#` shell, use the same call with `"mode":
"raw"`.

#### `relogin_device`
Re-authenticate on the **same socket** after the device dropped to its login prompt
(idle logout, reboot) — no disconnect/reconnect needed. Credentials default to the
ones given at connect. The channel is drained until quiet and a bare RETURN provokes
a fresh prompt first (this rides out post-reboot console churn and BDCOM's
`Press RETURN to get started`), with one internal retry; if the session is actually
still logged in, nothing is typed (credentials at a live prompt would run as
commands) and the footer reports `already logged in`.

**Parameters:** `host` (required), `username` / `password` (optional overrides,
e.g. for a recovery connect opened without credentials), `port` (optional).

#### `disconnect_device`
Disconnect from a device.

**Parameters:**
- `host` (required): Device IP/hostname to disconnect
- `port` (optional): Required only when several devices share an IP

#### `list_connections`
List all active connections (`target` = `host:port`, host, port, device_type,
protocol, current mode, timestamps).

#### `get_console_history`
Return the last N lines of raw console I/O captured for a connection — useful for
auditing logins, prompt-matching failures, and reboots.

**Parameters:** `host` (required), `limit` (optional, default 100), `port` (optional).

> ⚠️ History can contain sensitive output (e.g. a config dump exposes plaintext
> credentials). Treat it as sensitive.

#### `read_console_stream`
Read live console output **without sending a command** — accumulates whatever the
device emits until a pattern matches or the timeout elapses. Handy for watching a
device reboot back to its login prompt. A read that saw nothing returns a
`no output within Ns` footer (so "no data" isn't mistaken for a failure). Only
bytes arriving **during** the call are returned; output emitted between tool calls
goes to the console history — use `get_console_history` for that backlog.

**Parameters:** `host` (required), `expect_pattern` (optional regex),
`timeout` (optional seconds, default 10, capped at 120), `port` (optional).

#### `get_help`
Send `command_prefix + '?'` and return the device's inline CLI help, then clear the
input line so the next command runs cleanly. The footer lists the parsed next-token
`options:` (or flags an invalid prefix). The prefix is normalized (a stray trailing
`?` is dropped, multiple trailing spaces collapse to one), and long lists like
`ip ?` are no longer truncated.

**Parameters:** `host` (required), `command_prefix` (optional, e.g. `"show "`),
`port` (optional).

#### `enter_monitor_mode`
Drop the device into the bootloader `monitor#` shell. Two stages: (1) initiate a
reboot — tries the hidden boot menu first (**Ctrl-]**, then the single-keystroke
reboot option; works even at a login prompt), falling back to `reboot`+`y`; (2)
after `RTC Test`, sends a short **Ctrl-P** burst to interrupt the boot into monitor
mode (without it the unit boots normally).
Returns the boot transcript and a `now: monitor#` footer on success.

**Parameters:** `host` (required), `timeout` (optional seconds, default 180),
`port` (optional).

---

> **⚠️ The three file-transfer / firmware tools below are currently disabled**
> (commented out in [`server.py`](device_mcp/server.py)). Their manager methods still
> exist and are unit-tested; re-enable the `@mcp.tool` wrappers to expose them.

#### `transfer_file`
Run a BDCOM `copy <source> <destination> [server]` (config backup or image fetch)
and report whether the transfer confirmed success. `flash:` paths, `tftp:` and
`ftp://user:pass@host/dir/file` URLs, and BDCOM's shorthand all pass through. Works
in normal CLI/enable mode **and** in the bootloader `monitor` shell.

**Parameters:** `host`, `source`, `destination` (required), `server` (optional
trailing TFTP/FTP IP), `timeout` (optional, default 120), `port` (optional).

#### `upgrade_firmware`
Download an image to `flash:<flash_name>`, require a `successfully` confirmation,
then (by default) `reboot` into it answering the `(y/n)` prompt. Aborts before
rebooting if the transfer doesn't confirm. Normal/enable-mode path — for a unit too
broken to boot that far, use `recover_firmware`.

**Parameters:** `host`, `image_url`, `server` (required), `flash_name` (optional,
default `switch.bin`), `reboot` (optional bool, default true), `port` (optional).

#### `recover_firmware`
End-to-end firmware recovery via the `monitor#` shell, for a unit too broken to
upgrade normally: `enter_monitor_mode` → assign `monitor_ip` → `transfer_file` the
image → `reboot` into it. Aborts (before any reboot) if monitor mode isn't reached,
the flash transfer doesn't confirm, or `image_url` isn't a `tftp:` source.

`image_url` **must be a `tftp:` source**: the bootloader `monitor` `copy` only
understands `tftp:` (an `ftp://` URL is rejected as `Parameter invalid`) and caps the
source name at 60 chars. This fleet's relay shorthand is
`tftp:f::<last-chars-of-ftp-dir>/<file>` — e.g.
`tftp:f::53/BD_3954_interAptiv_2.2.0F_154634.bin` for FTP dir `/BDCOM0053/` — pointed
at `server` (the TFTP→FTP gateway IP, distinct from the FTP host).

**Parameters:** `host`, `image_url` (a `tftp:` source), `server`, `monitor_ip`
(required), `mask` (optional, default `255.255.255.0`), `flash_name` (optional,
default `switch.bin`), `port` (optional).

### 🧾 Result footer

Every command tool appends one compact status line so the model never has to guess
session state:

```
<raw command output>

[device-mcp] ok | now: Switch# (enable)
[device-mcp] device error: Unknown command near 'status' | hint: BDCOM has no 'show interface status'; use 'show ip interface brief'. | now: Switch# (enable)
[device-mcp] SESSION_TERMINATED: device returned to login prompt — reconnect required | now: awaiting_login
[device-mcp] FAILED: <transport exception> | now: <prompt|state>
[device-mcp] applied 3 command(s) | now: Switch_config_g0/1# (config: interface g0/1)
[device-mcp] options: interface, ip, ipv6 | now: Switch_config# (config)
```

- **device error** — the device *rejected* the command (with the offending token
  when it prints a `^` caret); distinct from a **FAILED** transport error.
- **SESSION_TERMINATED** — the device dropped the session back to a login prompt
  (e.g. an idle logout, or `write memory` which BDCOM treats as a forced logout);
  recover with `relogin_device` (same socket) or `disconnect_device` +
  `connect_device`. Reported instead of a raw `ReadTimeout`, so a desync is
  immediately obvious. While the session sits at that login prompt, commands are
  refused rather than typed into the `Username:` field.
- **hint** — for a known Cisco→BDCOM gotcha (see the cheat-sheet below) the footer
  appends the right command to use; shown on a device error or `SESSION_TERMINATED`.
- **now** — the live prompt plus a **parsed mode label** (never raw read-buffer
  text). The mode is read from the prompt suffix (the hostname is arbitrary), and
  config **sub-modes** are named so you know exactly where the CLI is:

  | Prompt | `now:` label |
  |---|---|
  | `Switch>` | `user` |
  | `Switch#` | `enable` |
  | `Switch_config#` | `config` |
  | `Switch_config_g0/1#` | `config: interface g0/1` |
  | `Switch_config_vlan10#` | `config: vlan 10` |
  | `Switch_config_line#` | `config: line` |
  | `monitor#` | `monitor` (BootROM recovery) |

  Any other `_config_<x>` / `(config-<x>)` sub-mode is reported as `config: <x>`.
  Abnormal states with no usable prompt are reported by name instead:
  `awaiting_login` / `session_terminated` / `unknown`.

### 💡 Usage Examples

#### Basic Device Information
```
AI: "Connect to router 192.168.1.1 and show me the device information"
```
The AI will:
1. Use `connect_device` to establish connection
2. Use `execute_command` with "show version"

#### Interface Configuration
```
AI: "Configure interface GigabitEthernet0/1 with IP 10.1.1.1/24"
```
The AI will:
1. Use `execute_command` with mode "config"
2. Execute: "interface GigabitEthernet0/1"
3. Execute: "ip address 10.1.1.1 255.255.255.0"

#### Network Troubleshooting
```
AI: "Check the routing table and interface status on the core switch"
```
The AI will execute multiple commands:
- "show ip route"
- "show ip interface brief"
- "show interface status"

#### Multiple devices behind one console server
When a terminal/console server exposes several devices on one IP at different
ports, connect to each with its `port`, then address tools by `host` + `port`:
```
connect_device  host=192.168.100.34  port=10003  device_type=bdcom  protocol=telnet
connect_device  host=192.168.100.34  port=10004  device_type=bdcom  protocol=telnet
execute_command host=192.168.100.34  port=10003  commands=["show version"]
```
`list_connections` shows each as a distinct `target` (`192.168.100.34:10003`,
`192.168.100.34:10004`). If a host has only one connection, `port` can be omitted.

### 🔧 Supported Commands

This server is a generic command executor — it forwards **any** command the
device accepts. Cisco IOS and BDCOM share most of the common CLI:

| Category | Examples |
|---|---|
| Show | `show version`, `show running-config`, `show ip interface brief`, `show ip route`, `show vlan brief`, `show mac address-table` |
| Config | `interface <if>`, `ip address <ip> <mask>`, `no shutdown`, `vlan <id>`, `router ospf <id>` |
| Diagnostic | `ping <dest>`, `traceroute <dest>`, `show tech-support` |

### 📟 BDCOM notes

netmiko has no built-in BDCOM driver, so this server ships a small custom one
([`device_mcp/bdcom.py`](device_mcp/bdcom.py)) that adapts BDCOM's Cisco-IOS-*like*
CLI. The differences it handles (verified against the BDCOM Switch L3 docs):

| Aspect | Cisco IOS | BDCOM | Handled by |
|---|---|---|---|
| Enter global config | `configure terminal` | `config` | custom driver |
| Config prompt | `host(config)#` | `Switch_config#` (no parens) | custom driver |
| Exit config | `end` | Ctrl-Z (device doesn't echo it) | custom driver |
| Exit privileged | `disable` | `exit` (`Switch#` → `Switch>`) | custom driver |
| Set terminal width | `terminal width 511` | *(unsupported)* | custom driver (skipped) |
| Disable paging | `terminal length 0` (any mode) | `terminal length 0` **(enable mode only)** | custom driver (in enable mode) |
| Enter enable | `enable` (+secret) | `enable` (often **no** secret) | inherited |

Select it per connection with `"device_type": "bdcom"` (or `protocol: "telnet"`
for Telnet). Mode names (`user` / `enable` / `config`) map to BDCOM's user /
management / global-configuration modes.

**Why a custom session setup:** BDCOM rejects `terminal width` and only accepts
`terminal length 0` (disable paging) in privileged mode. netmiko's stock Cisco
setup runs both at login in **user** mode, which fails *and leaves paging on* — so
the next large `show` stalls on `--More--` and desyncs the session. The BDCOM
driver instead enters enable mode and disables paging there.

**Field tips (from real-hardware testing):**
- Use `device_type: "bdcom"` for BDCOM switches — required for config-mode ops.
- `enable` needs no password on a default BDCOM (`aaa authentication enable default
  none`); just omit `enable_password`.
- A config dump exposes plaintext credentials — treat `show running-config` output
  (and `get_console_history`) as sensitive.
- If a session ever desyncs, `disconnect_device` then `connect_device` for a clean
  one rather than retrying the failing command. The footer now says
  `SESSION_TERMINATED` when the device has dropped you to a login prompt; after a
  plain idle logout, `relogin_device` re-authenticates on the same socket instead.
- **Firmware:** use `upgrade_firmware` for a healthy unit; for one that can't boot
  far enough, `recover_firmware` enters the bootloader `monitor#` shell (reboot via
  the hidden boot menu — Ctrl-] then the reboot option — then a Ctrl-P burst after
  `RTC Test`) and re-flashes over TFTP/FTP. Use `transfer_file` on its own to back up
  a config (`flash:` → `tftp:`).

#### BDCOM CLI cheat-sheet (commands the server can't enforce)

The server is a generic executor, so these BDCOM-specific rules are on the caller
(the footer's `device error` flags a violation, and for the common Cisco-isms below
it appends the right command as a `hint:`):

- **Save config:** use bare `write` (or `write all`), **not** Cisco `write memory` —
  on BDCOM the latter is treated as a forced logout (`SESSION_TERMINATED`).
- **`show` differences:** there is no `show interface status` (use `show ip interface
  brief`) or `show vlan brief` (use `show vlan`).
- **Access-port VLAN:** there is no Cisco `switchport access vlan <id>`. Use
  `switchport mode access` then `switchport pvid <id>`.
- **VRF needs an RD first:** after `ip vrf <name>` / `ipv6 vrf <name>`, set
  `rd <a>:<b>` **before** referencing the VRF anywhere (`vrf forwarding`, routes) —
  otherwise: `%Err, VRF '<n>' does not exist or does not have a RD.`
- **Order around VRF on an SVI:** setting `vrf forwarding` / `ipv6 vrf forwarding`
  on an interface **clears its existing IP/IPv6 addresses**. Set the VRF first, then
  (re-)apply the addresses.
- **Sub-modes don't auto-exit:** `vlan X`, `interface X`, `ip vrf X` each open a
  sub-mode; send an explicit `exit` between contexts — `execute_command` with
  `mode=config` and the `exit` lines in the list handles this.
- **Privileged for `show`:** BDCOM `show` needs enable mode; the session lands in
  enable at connect and the default `raw` mode runs at that live prompt, so no
  `mode` is needed.

### 🔒 Security Notes

- This tool is designed for network automation and management
- Credentials are passed per connection and not stored
- A `transfer_file` / firmware `copy` URL may embed plaintext FTP credentials
  (`ftp://user:pass@host/...`); treat the returned transfer output and
  `get_console_history` as sensitive
- Use appropriate network security practices
- Consider using SSH keys for enhanced security (future enhancement)

### 🏗 Architecture

```
AI Assistant (Claude/Amazon Q)
    ↓ Natural Language
MCP Client
    ↓ Tool Calls
Device MCP Server  ── netmiko (cisco_ios) / custom driver (bdcom)
    ↓ SSH/Telnet
Network Devices (Routers/Switches/Firewalls)
```

### 🤝 Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 中文

一个全面的MCP（模型上下文协议）服务器，用于通过SSH/Telnet管理**网络设备**。通过Claude和Amazon Q等AI助手执行命令并管理路由器、交换机和防火墙。支持**Cisco IOS**、**BDCOM**以及任何 [netmiko](https://github.com/ktbyers/netmiko) 支持的平台。

### ✨ 功能特性

- **🔌 双协议支持**: 支持SSH或Telnet连接
- **🧩 多厂商支持**: 通过 `device_type` 选择 Cisco IOS（默认）、BDCOM 以及 400+ netmiko 平台
- **🔄 持久连接**: 维护长连接以实现高效的命令执行
- **🎯 通用命令执行**: 通过单一接口执行任何设备命令
- **🔐 模式管理**: 自动在用户、特权和配置模式之间切换
- **🌐 多设备支持**: 同时管理多个设备
- **🤖 AI友好**: 通过AI助手进行自然语言命令转换
- **📊 连接监控**: 跟踪活动连接及其状态

### 🚀 快速开始

1. **克隆并安装**
   ```bash
   git clone https://github.com/rajib884/bdcom_mcp.git device-mcp
   cd device-mcp
   python -m venv .venv
   # Windows:  .venv\Scripts\activate
   # macOS/Linux:  source .venv/bin/activate
   pip install -e .          # 或: pip install -r requirements.txt
   ```

2. **运行 / 验证**
   ```bash
   python -m device_mcp.server   # 通过 stdio 提供服务
   ```

3. **配置MCP客户端**

   添加到您的MCP配置中（例如Claude Desktop），使用上面创建的虚拟环境中的 Python 解释器：
   ```json
   {
     "mcpServers": {
       "device-mcp": {
         "command": "/path/to/device-mcp/.venv/bin/python",
         "args": ["-m", "device_mcp.server"]
       }
     }
   }
   ```
   Windows 请使用 `\path\to\device-mcp\.venv\Scripts\python.exe`。

### 🛠 可用工具

连接以 `host:port` 为键，因此同一 IP、不同端口的多台设备（控制台/终端服务器）互不影响；
当某主机只有一个连接时，工具的 `port` 参数可省略。

#### `connect_device`
建立到网络设备的连接（参数 `device_type` 选择平台：`cisco_ios` 默认 / `bdcom` / 任意 netmiko 类型）。
`username` / `password` 可选（用于无认证的控制台）；默认 BDCOM 进入 `enable` 无需密码。

#### `execute_command`
在已连接的设备上执行**一条或多条**命令（`commands` 为命令列表，单条命令用单元素列表）。
`mode` 默认 `raw`（直接驱动通道、在当前提示符下执行，用于 netmiko 不认识的提示符，如引导
加载器 `monitor#`，普通 `reboot` 会一直等待 `Switch.*` 提示符而发不出去）；`auto` 在当前
权限级别执行、不降级（BDCOM 连接后处于 enable，`show` 可直接工作），也可强制
`user`/`enable`；**`config`** 将整个列表作为一个配置块下发（自动进出配置模式、处理子模式
提示符）；某条被拒绝时该块停止执行，footer 指出是哪一条——**之前的行已生效且不会回滚**，
并自动退出配置模式以免会话滞留在子模式。
返回每条命令的原始输出加一行 `[device-mcp]` footer。支持
`expect_regex` + `answer` 应答交互式 `(y/n)` 确认（仅单条命令时生效）；`port` 仅在同一 IP
有多台设备时需要。若会话停在登录提示符，命令**不会**被输入到 `Username:` 字段：默认先尝试
自动重新登录，否则 footer 提示调用 `relogin_device`。

#### `relogin_device`
设备因空闲超时/重启回到登录提示符后，在**同一连接**上重新认证（无需断开重连）。凭据默认
沿用连接时提供的。会先等通道安静并发送回车唤出新的提示符（可跨过启动期刷屏和
`Press RETURN to get started`），内部重试一次；若会话其实仍在登录状态，则不输入任何内容并
报告 `already logged in`。

#### `disconnect_device`
断开与设备的连接（同一 IP 多设备时需指定 `port`）。

#### `list_connections`
列出所有活动连接（含 `target` = `host:port`）。

#### `get_console_history`
返回某连接最近 N 行原始控制台 I/O，用于审计登录、提示符不匹配和重启。可能包含明文凭据，请按敏感数据处理。

#### `read_console_stream`
不发送命令，直接读取实时控制台输出，直到匹配正则或超时（适合观察设备重启回到登录提示符）。

#### `get_help`
发送 `command_prefix + '?'` 返回设备的内联 CLI 帮助，并清理输入行；footer 列出解析出的
下一级 `options:`。会归一化前缀（去掉手动多写的 `?`、合并多余尾随空格），长列表（如 `ip ?`）
不再被截断。

#### `enter_monitor_mode`
让设备进入引导加载器 `monitor#` 恢复外壳：先通过隐藏启动菜单（Ctrl-]，单键选项）或
`reboot`+`y` 触发重启，在 `RTC Test` 后发送 Ctrl-P 中断启动进入 monitor 模式。
成功时返回启动记录及 `now: monitor#` footer。

### 📟 BDCOM 说明

netmiko 没有内置 BDCOM 驱动，本服务器提供了一个小型自定义驱动
（[`device_mcp/bdcom.py`](device_mcp/bdcom.py)）来适配 BDCOM 的 CLI：进入全局配置用
`config`（而非 `configure terminal`），配置提示符为 `Switch_config#`（无括号），用
Ctrl-Z 退出配置模式，特权模式用 `exit` 退出。BDCOM 不支持 `terminal width`，且
`terminal length 0`（关闭分页）仅在特权模式可用——因此驱动会先进入 enable 模式再关闭分页，
避免大输出在 `--More--` 处卡住导致会话错乱。默认 BDCOM `enable` 无需密码。连接时设置
`"device_type": "bdcom"` 即可。

**BDCOM CLI 速查（服务器无法强制，需调用方遵循）：**
- 接入口 VLAN 用 `switchport mode access` + `switchport pvid <id>`（没有 `switchport access vlan`）。
- VRF 必须先在 `ip vrf`/`ipv6 vrf` 内设置 `rd a:b` 再被引用，否则报 `does not have a RD`。
- 在接口上设置 `vrf forwarding` 会清除其已配置的 IP/IPv6 地址——先设 VRF，再配地址。
- 子模式（`vlan`/`interface`/`ip vrf`）不会自动退出，上下文之间需 `exit`，或用 `execute_command` 的 `mode=config`。

### 📝 许可证

本项目采用MIT许可证 - 详见[LICENSE](LICENSE)文件。
