# Cisco MCP Server

[English](#english) | [中文](#中文)

---

## English

A comprehensive MCP (Model Context Protocol) server for Cisco device management via SSH/Telnet. Execute commands and manage Cisco routers, switches, and firewalls through AI assistants like Claude and Amazon Q.

### ✨ Features

- **🔌 Dual Protocol Support**: Connect via SSH or Telnet
- **🔄 Persistent Connections**: Maintain long-lived connections for efficient command execution
- **🎯 Universal Command Execution**: Execute any Cisco command through a single interface
- **🔐 Mode Management**: Automatic switching between user, enable, and configuration modes
- **🌐 Multi-Device Support**: Manage multiple Cisco devices simultaneously
- **🤖 AI-Friendly**: Natural language command translation through AI assistants
- **📊 Connection Monitoring**: Track active connections and their status

> **Implementation:** This is the Python / [FastMCP](https://github.com/jlowin/fastmcp)
> server. SSH/Telnet transport is handled by [netmiko](https://github.com/ktbyers/netmiko),
> which is purpose-built for network devices (robust prompt detection, paging,
> and user/enable/config mode handling).

### 🚀 Quick Start

1. **Clone and Install**
   ```bash
   git clone https://github.com/very99/cisco-mcp.git
   cd cisco-mcp
   python -m venv .venv
   # Windows:  .venv\Scripts\activate
   # macOS/Linux:  source .venv/bin/activate
   pip install -e .          # or: pip install -r requirements.txt
   ```

2. **Run / Verify**
   ```bash
   python -m cisco_mcp.server   # serves over stdio
   ```

3. **Configure MCP Client**

   Add to your MCP configuration (e.g., Claude Desktop). Use the Python
   interpreter from the virtual environment you created above:
   ```json
   {
     "mcpServers": {
       "cisco-mcp": {
         "command": "/path/to/cisco-mcp/.venv/bin/python",
         "args": ["-m", "cisco_mcp.server"]
       }
     }
   }
   ```
   On Windows, use `\path\to\cisco-mcp\.venv\Scripts\python.exe`. If you
   installed the package (`pip install -e .`), you can instead point `command`
   at the generated `cisco-mcp` executable with no `args`.

### 🛠 Available Tools

#### `connect_cisco_device`
Establish a connection to a Cisco device.

**Parameters:**
- `host` (required): IP address or hostname
- `username` (required): Authentication username
- `password` (required): Authentication password
- `protocol` (optional): "ssh" or "telnet" (default: "ssh")
- `port` (optional): Custom port number
- `enable_password` (optional): Enable password for privileged mode

**Example:**
```json
{
  "host": "192.168.1.1",
  "username": "admin",
  "password": "password123",
  "protocol": "ssh",
  "enable_password": "enable123"
}
```

#### `execute_cisco_command`
Execute a command on a connected Cisco device.

**Parameters:**
- `host` (required): Target device IP/hostname
- `command` (required): Cisco command to execute
- `mode` (optional): "user", "enable", or "config" (default: "user")

**Example:**
```json
{
  "host": "192.168.1.1",
  "command": "show version",
  "mode": "user"
}
```

#### `disconnect_cisco_device`
Disconnect from a Cisco device.

**Parameters:**
- `host` (required): Device IP/hostname to disconnect

#### `list_connections`
List all active connections.

### 💡 Usage Examples

#### Basic Device Information
```
AI: "Connect to router 192.168.1.1 and show me the device information"
```
The AI will:
1. Use `connect_cisco_device` to establish connection
2. Use `execute_cisco_command` with "show version"

#### Interface Configuration
```
AI: "Configure interface GigabitEthernet0/1 with IP 10.1.1.1/24"
```
The AI will:
1. Use `execute_cisco_command` with mode "config"
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

### 🔧 Supported Cisco Commands

This MCP server supports **all** Cisco IOS commands, including but not limited to:

#### Show Commands
- `show version` - Device information
- `show running-config` - Current configuration
- `show ip interface brief` - Interface summary
- `show ip route` - Routing table
- `show vlan brief` - VLAN information
- `show interface status` - Interface status
- `show cdp neighbors` - CDP neighbors
- `show mac address-table` - MAC address table

#### Configuration Commands
- `configure terminal` - Enter configuration mode
- `interface <interface>` - Configure interface
- `ip address <ip> <mask>` - Set IP address
- `no shutdown` - Enable interface
- `vlan <vlan-id>` - Create/configure VLAN
- `router ospf <process-id>` - Configure OSPF

#### Diagnostic Commands
- `ping <destination>` - Test connectivity
- `traceroute <destination>` - Trace route
- `show tech-support` - Technical support information

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
Cisco MCP Server
    ↓ SSH/Telnet
Cisco Devices (Routers/Switches/Firewalls)
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

一个全面的MCP（模型上下文协议）服务器，用于通过SSH/Telnet管理Cisco设备。通过Claude和Amazon Q等AI助手执行命令并管理Cisco路由器、交换机和防火墙。

### ✨ 功能特性

- **🔌 双协议支持**: 支持SSH或Telnet连接
- **🔄 持久连接**: 维护长连接以实现高效的命令执行
- **🎯 通用命令执行**: 通过单一接口执行任何Cisco命令
- **🔐 模式管理**: 自动在用户、特权和配置模式之间切换
- **🌐 多设备支持**: 同时管理多个Cisco设备
- **🤖 AI友好**: 通过AI助手进行自然语言命令转换
- **📊 连接监控**: 跟踪活动连接及其状态

### 🚀 快速开始

1. **克隆并安装**
   ```bash
   git clone https://github.com/very99/cisco-mcp.git
   cd cisco-mcp
   python -m venv .venv
   # Windows:  .venv\Scripts\activate
   # macOS/Linux:  source .venv/bin/activate
   pip install -e .          # 或: pip install -r requirements.txt
   ```

2. **运行 / 验证**
   ```bash
   python -m cisco_mcp.server   # 通过 stdio 提供服务
   ```

3. **配置MCP客户端**

   添加到您的MCP配置中（例如Claude Desktop），使用上面创建的虚拟环境中的 Python 解释器：
   ```json
   {
     "mcpServers": {
       "cisco-mcp": {
         "command": "/path/to/cisco-mcp/.venv/bin/python",
         "args": ["-m", "cisco_mcp.server"]
       }
     }
   }
   ```
   Windows 请使用 `\path\to\cisco-mcp\.venv\Scripts\python.exe`。

### 🛠 可用工具

#### `connect_cisco_device`
建立到Cisco设备的连接。

#### `execute_cisco_command`
在已连接的Cisco设备上执行命令。

#### `disconnect_cisco_device`
断开与Cisco设备的连接。

#### `list_connections`
列出所有活动连接。

### 💡 使用示例

#### 基本设备信息
```
AI: "连接到路由器192.168.1.1并显示设备信息"
```

#### 接口配置
```
AI: "配置接口GigabitEthernet0/1的IP为10.1.1.1/24"
```

#### 网络故障排除
```
AI: "检查核心交换机的路由表和接口状态"
```

### 🔧 支持的Cisco命令

此MCP服务器支持**所有**Cisco IOS命令，包括但不限于：

- 显示命令（show commands）
- 配置命令（configuration commands）
- 诊断命令（diagnostic commands）

### 📝 许可证

本项目采用MIT许可证 - 详见[LICENSE](LICENSE)文件。