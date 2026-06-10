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
- **🔐 Mode Management**: Automatic switching between user, enable, and configuration modes
- **🌐 Multi-Device Support**: Manage multiple devices simultaneously
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
   git clone https://github.com/very99/cisco-mcp.git device-mcp
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
- `username` (required): Authentication username
- `password` (required): Authentication password
- `device_type` (optional): `cisco_ios` (default), `bdcom`, or any netmiko device type
- `protocol` (optional): `ssh` or `telnet` (default: `ssh`)
- `port` (optional): Custom port number
- `enable_password` (optional): Enable password for privileged mode

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

#### `execute_command`
Execute a command on a connected device.

**Parameters:**
- `host` (required): Target device IP/hostname
- `command` (required): Command to execute
- `mode` (optional): `user`, `enable`, or `config` (default: `user`)

**Example:**
```json
{
  "host": "192.168.1.1",
  "command": "show version",
  "mode": "user"
}
```

#### `disconnect_device`
Disconnect from a device.

**Parameters:**
- `host` (required): Device IP/hostname to disconnect

#### `list_connections`
List all active connections (host, device_type, protocol, current mode, timestamps).

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
| Exit config | `end` | Ctrl-Z / `exit` / `quit` | custom driver |
| Privileged mode | `enable` | `enable` | inherited |
| Disable paging | `terminal length 0` | `terminal length 0` | inherited |
| Enable password | `enable password` | `enable password` | inherited |

Select it per connection with `"device_type": "bdcom"` (or `protocol: "telnet"`
for Telnet). Mode names (`user` / `enable` / `config`) map to BDCOM's user /
management / global-configuration modes.

### 🔒 Security Notes

- This tool is designed for network automation and management
- Credentials are passed per connection and not stored
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
   git clone https://github.com/very99/cisco-mcp.git device-mcp
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

#### `connect_device`
建立到网络设备的连接（参数 `device_type` 选择平台：`cisco_ios` 默认 / `bdcom` / 任意 netmiko 类型）。

#### `execute_command`
在已连接的设备上执行命令。

#### `disconnect_device`
断开与设备的连接。

#### `list_connections`
列出所有活动连接。

### 📟 BDCOM 说明

netmiko 没有内置 BDCOM 驱动，本服务器提供了一个小型自定义驱动
（[`device_mcp/bdcom.py`](device_mcp/bdcom.py)）来适配 BDCOM 的 CLI：进入全局配置用
`config`（而非 `configure terminal`），配置提示符为 `Switch_config#`（无括号），用
Ctrl-Z 退出配置模式。`enable` 特权模式、`terminal length 0` 关闭分页、`enable
password` 均与 Cisco 兼容。连接时设置 `"device_type": "bdcom"` 即可。

### 📝 许可证

本项目采用MIT许可证 - 详见[LICENSE](LICENSE)文件。
