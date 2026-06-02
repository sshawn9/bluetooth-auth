# bluetooth-auth

[![CI](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml/badge.svg)](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml)
[![Renovate](https://img.shields.io/badge/renovate-enabled-brightgreen.svg)](./renovate.json)
[![English](https://img.shields.io/badge/lang-English-blue)](./README.md)
[![简体中文](https://img.shields.io/badge/lang-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-red)](./README.zh-CN.md)

Small BlueZ D-Bus helpers for Bluetooth-based authentication workflows on
NixOS.

`bluetooth-auth` watches one trusted Bluetooth device and uses its connection
state as a convenience signal for local desktop security workflows. It can keep
the device connected, lock the session when the device disconnects, and allow a
specific trusted user to pass `sudo` authentication while the device is
connected.

> Bluetooth proximity is a convenience factor, not strong authentication. Use
> this for trusted personal machines where the ergonomics are worth the risk,
> and keep normal password authentication available as the fallback.

## Features

- Maintains a BlueZ `org.bluez.Device1` connection for a paired and trusted
  device.
- Locks the active local Wayland session when the device is no longer
  connected.
- Adds an optional `sudo` PAM rule that succeeds for one configured user when
  the trusted Bluetooth device is connected.
- Ships as a Nix flake with a NixOS module and a Python package.
- Uses BlueZ over the system D-Bus through `dbus-fast`.

## Requirements

- Linux with BlueZ.
- NixOS for the included module.
- Python 3.13 if running the command-line tools outside the Nix package.
- A Bluetooth device that is already paired and trusted in BlueZ.
- Noctalia for the included lock integration.

The current device path assumes the adapter is `hci0`.

## Pair and Trust a Device

Use `bluetoothctl` to pair and trust the device before enabling the services:

```console
bluetoothctl
power on
agent on
default-agent
scan on
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
scan off
quit
```

Replace `AA:BB:CC:DD:EE:FF` with the device address you want to use as the
auth signal.

## NixOS Installation

Add the flake input:

```nix
{
  inputs.bluetooth-auth.url = "github:sshawn9/bluetooth-auth";
}
```

Import the NixOS module and configure the trusted user and device address:

```nix
{
  inputs,
  nixpkgs,
  ...
}:

{
  nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";

    modules = [
      inputs.bluetooth-auth.nixosModules.default

      {
        my.security.bluetoothAuth = {
          enable = true;
          user = "alice";
          bluetoothAddress = "AA:BB:CC:DD:EE:FF";

          autoConnect.enable = true;
          autoLock = {
            enable = true;
            checkIntervalSeconds = 30;
          };
          sudoAuth.enable = true;
        };
      }
    ];
  };
}
```

When `my.security.bluetoothAuth.enable = true`, the feature modules are enabled
by default:

- `autoConnect.enable`
- `autoLock.enable`
- `sudoAuth.enable`

Set `my.security.bluetoothAuth.user` whenever `autoLock` or `sudoAuth` is
enabled. If you only want automatic reconnects, disable the other integrations:

```nix
{
  my.security.bluetoothAuth = {
    enable = true;
    bluetoothAddress = "AA:BB:CC:DD:EE:FF";
    autoLock.enable = false;
    sudoAuth.enable = false;
  };
}
```

## NixOS Options

| Option | Default | Description |
| --- | --- | --- |
| `my.security.bluetoothAuth.enable` | `false` | Enables the Bluetooth authentication services. |
| `my.security.bluetoothAuth.package` | flake package | Package that provides the command-line tools. |
| `my.security.bluetoothAuth.user` | `null` | User trusted by sudo auth and targeted by auto-lock. |
| `my.security.bluetoothAuth.bluetoothAddress` | `""` | Bluetooth device address to monitor through BlueZ. |
| `my.security.bluetoothAuth.autoConnect.enable` | `true` | Keeps the trusted device connected. |
| `my.security.bluetoothAuth.autoLock.enable` | `true` | Locks the session when the device disconnects. |
| `my.security.bluetoothAuth.autoLock.checkIntervalSeconds` | `30` | Polling interval for connection and lock checks. Values below 30 seconds are treated as 30 seconds by the CLI. |
| `my.security.bluetoothAuth.sudoAuth.enable` | `true` | Allows the trusted user to pass `sudo` auth when the device is connected. |

## Services

The NixOS module may create these systemd units:

| Unit | Type | Purpose |
| --- | --- | --- |
| `bluetooth-auth-auto-connect.service` | system | Periodically calls BlueZ `Device1.Connect` for the configured device when it is paired, trusted, and disconnected. |
| `bluetooth-auth-auto-lock.service` | system | Checks the configured device and starts `noctalia-lock.service` when the device is disconnected and the active Wayland session is not already locked. |
| `noctalia-lock.service` | system | Runs the configured user's Noctalia user service with the right runtime and D-Bus environment. |
| `noctalia-lock.service` | user | Calls `noctalia-shell ipc call lockScreen lock`. |

Inspect service status with:

```console
systemctl status bluetooth-auth-auto-connect.service
systemctl status bluetooth-auth-auto-lock.service
systemctl status noctalia-lock.service
systemctl --user status noctalia-lock.service
```

## Command-Line Tools

The package exposes three commands:

```console
bluetooth-auth-auto-connect AA:BB:CC:DD:EE:FF
```

Keeps a paired and trusted BlueZ device connected. The command exits only when
interrupted.

```console
bluetooth-auth-auto-lock alice AA:BB:CC:DD:EE:FF 30
```

Polls the device connection state and starts `noctalia-lock.service` when the
device is disconnected and the user's active local Wayland session is unlocked.

```console
bluetooth-auth-sudo-auth alice AA:BB:CC:DD:EE:FF
```

Checks whether the current PAM request belongs to `alice` and whether the
device is connected. Exit status `0` means the check succeeded. Exit status `1`
means authentication should continue to the next PAM rule.

## Build and Development

Build the package:

```console
nix build .
```

Run the flake checks:

```console
nix flake check
```

Enter a development shell:

```console
nix develop
```

Install Python dependencies in a local environment:

```console
uv sync
```

## Security Model

`bluetooth-auth` treats "the configured device is connected to BlueZ" as a
local trust signal. That signal can be useful, but it is weaker than a password,
hardware security key, or cryptographic challenge-response protocol.

Important implications:

- Pair and trust only a device you control.
- Keep the normal `sudo` password path available.
- Consider disabling `sudoAuth` if you only want auto-connect and auto-lock.
- Review the PAM rule before using this on a shared or high-risk machine.
- A connected Bluetooth device does not prove that the device owner is present
  or attentive.

## Troubleshooting

Check that BlueZ knows the device and that it is trusted:

```console
bluetoothctl info AA:BB:CC:DD:EE:FF
```

Watch service logs:

```console
journalctl -u bluetooth-auth-auto-connect.service -f
journalctl -u bluetooth-auth-auto-lock.service -f
journalctl -u noctalia-lock.service -f
journalctl --user -u noctalia-lock.service -f
```

If auto-lock does not trigger, confirm that `loginctl` reports an active local
Wayland session for the configured user:

```console
loginctl list-sessions
loginctl show-session <session-id>
```

If Noctalia does not lock, confirm that the configured user has
`noctalia-shell` available at:

```text
/etc/profiles/per-user/<user>/bin/noctalia-shell
```

## License

No license file is currently included.
