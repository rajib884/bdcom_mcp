# Device MCP Server - Usage Examples

## Basic Connection and Commands

### 1. Connect to a Device

Cisco IOS (default `device_type`):
```json
{
  "tool": "connect_device",
  "arguments": {
    "host": "192.168.1.1",
    "username": "admin",
    "password": "secret123",
    "device_type": "cisco_ios",
    "protocol": "ssh",
    "enable_password": "enable123"
  }
}
```

BDCOM switch (telnet; a default BDCOM `enable` needs no password, so
`enable_password` is omitted):
```json
{
  "tool": "connect_device",
  "arguments": {
    "host": "192.168.1.2",
    "username": "admin",
    "password": "secret123",
    "device_type": "bdcom",
    "protocol": "telnet"
  }
}
```

Several devices behind one console/terminal server (same IP, different ports) -
each is a distinct `host:port` connection:
```json
{ "tool": "connect_device",
  "arguments": { "host": "192.168.100.34", "port": 10003, "username": "admin",
                 "password": "admin", "device_type": "bdcom", "protocol": "telnet" } }
```
```json
{ "tool": "connect_device",
  "arguments": { "host": "192.168.100.34", "port": 10004, "username": "admin",
                 "password": "admin", "device_type": "bdcom", "protocol": "telnet" } }
```

### 2. Execute Basic Show Commands

```json
{
  "tool": "execute_command",
  "arguments": {
    "host": "192.168.1.1",
    "command": "show version",
    "mode": "user"
  }
}
```

```json
{
  "tool": "execute_command",
  "arguments": {
    "host": "192.168.1.1",
    "command": "show ip interface brief",
    "mode": "enable"
  }
}
```

### 3. Configuration — prefer `configure_device` for multi-step

Send an ordered command list as one block; config mode is entered/exited
automatically and sub-mode prompt changes are handled. Put an `exit` as its own
list item when moving between contexts:

```json
{
  "tool": "configure_device",
  "arguments": {
    "host": "192.168.1.1",
    "commands": [
      "interface GigabitEthernet0/1",
      "ip address 10.1.1.1 255.255.255.0",
      "no shutdown",
      "exit"
    ]
  }
}
```

A single config line can still go through `execute_command` with `mode: "config"`,
but for sequences `configure_device` reports exactly which line failed:

```json
{ "tool": "configure_device",
  "arguments": { "host": "192.168.1.1",
                 "commands": ["interface GigabitEthernet0/1", "no shutdown", "exit"] } }
```

#### BDCOM VLAN + SVI + VRF (ordering matters)

`switchport pvid` (not `switchport access vlan`); set the VRF `rd` before using the
VRF; set `vrf forwarding` **before** the interface addresses (it clears them):

```json
{
  "tool": "configure_device",
  "arguments": {
    "host": "192.168.100.34",
    "port": 10003,
    "commands": [
      "vlan 30", "exit",
      "ipv6 vrf VRF30", "rd 30:30", "exit",
      "interface GigaEthernet0/1", "switchport mode access", "switchport pvid 30", "exit",
      "interface VLAN30",
      "ipv6 vrf forwarding VRF30",
      "ipv6 address 30::1/64",
      "exit"
    ]
  }
}
```

### 4. Interactive Commands (confirmations)

Commands that wait for a `(y/n)` / `[confirm]` answer (e.g. `reboot`,
`delete startup-config`) use `expect_string` (a regex) + `answer`. Include `port`
when the device shares its IP with others:
```json
{
  "tool": "execute_command",
  "arguments": {
    "host": "192.168.100.34",
    "port": 10003,
    "command": "reboot",
    "mode": "enable",
    "expect_string": "\\(y/n\\)",
    "answer": "y"
  }
}
```

### 5. Console Diagnostics

Audit the raw I/O captured for a connection (logins, desyncs, reboots):
```json
{ "tool": "get_console_history",
  "arguments": { "host": "192.168.100.34", "port": 10003, "limit": 200 } }
```

Watch a device reboot back to its login prompt without sending a command:
```json
{ "tool": "read_console_stream",
  "arguments": { "host": "192.168.100.34", "port": 10003,
                 "expect_pattern": "Username:", "timeout": 120 } }
```

Ask the CLI what `show` subcommands exist (`?` help):
```json
{ "tool": "get_help",
  "arguments": { "host": "192.168.100.34", "port": 10003, "command_prefix": "show " } }
```

## Natural Language Examples for AI Assistants

### Network Troubleshooting
**User**: "Check the network connectivity and interface status on router 192.168.1.1"

**AI Assistant will**:
1. Connect to the device
2. Execute: `show ip interface brief`
3. Execute: `show interface status`
4. Execute: `ping 8.8.8.8`

### Device Information
**User**: "Get detailed information about the switch at 10.0.0.1"

**AI Assistant will**:
1. Connect to the device
2. Execute: `show version`
3. Execute: `show inventory`

### VLAN Configuration
**User**: "Create VLAN 100 named 'Sales' on the switch"

**AI Assistant will**:
1. Connect to the device
2. Enter config mode
3. Execute: `vlan 100`
4. Execute: `name Sales`

### Interface Configuration
**User**: "Configure port Gi0/1 for VLAN 10 with description 'Server Port'"

**AI Assistant will**:
1. Connect to the device
2. Enter config mode
3. Execute: `interface GigabitEthernet0/1`
4. Execute: `description Server Port`
5. Execute: `switchport mode access`
6. Execute: `switchport access vlan 10`

## Common Commands Reference (Cisco IOS / BDCOM)

These are widely supported across Cisco IOS and BDCOM. The server forwards any
command the device accepts, so vendor-specific commands work too.

### Show Commands
- `show version` - Device information and OS version
- `show running-config` - Current running configuration
- `show startup-config` - Startup configuration
- `show ip interface brief` - IP interface summary
- `show interface brief` - Interface status summary (BDCOM)
- `show vlan brief` - VLAN information
- `show ip route` - Routing table
- `show arp` - ARP table
- `show mac address-table` - MAC address table
- `show inventory` - Hardware inventory
- `show cpu` - CPU utilization (BDCOM; Cisco uses `show processes cpu`)
- `show flash` / `dir` - Flash contents

### Configuration Commands
- `interface <interface-name>` - Enter interface configuration
- `ip address <ip> <mask>` - Set IP address
- `no shutdown` / `shutdown` - Enable / disable interface
- `description <text>` - Set interface description
- `vlan <vlan-id>` - Create or enter VLAN configuration
- `name <vlan-name>` - Set VLAN name
- `switchport mode access` - Set port to access mode
- `switchport access vlan <vlan-id>` - Assign port to VLAN

> Note: enter config mode via `mode: "config"`. The server issues the right
> mode-entry command per platform (`configure terminal` on Cisco, `config` on BDCOM).

### Diagnostic Commands
- `ping <destination>` - Test connectivity
- `traceroute <destination>` - Trace network path
- `show tech-support` - Comprehensive diagnostic information
- `show logging` - System log messages

## Multi-Device Management

### Managing Multiple Devices
```json
{
  "tool": "list_connections",
  "arguments": {}
}
```

This shows all active connections, each with its `target` (`host:port`), host,
port, device_type, protocol, current mode, and timestamps.

### Disconnect from Device
```json
{
  "tool": "disconnect_device",
  "arguments": {
    "host": "192.168.100.34",
    "port": 10003
  }
}
```
`port` is required only when several devices share the IP; otherwise just pass `host`.

## Error Handling

The MCP server provides detailed error messages for:
- Connection failures
- Authentication errors
- Command execution errors
- Network timeouts
- Unsupported `device_type` values
- Ambiguous targets (several devices on one IP without a `port`)

All errors are returned in a structured format for easy parsing by AI assistants.
