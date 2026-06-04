# bluetooth-auth

[![CI](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml/badge.svg)](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml)
[![Renovate](https://img.shields.io/badge/renovate-enabled-brightgreen.svg)](https://github.com/sshawn9/bluetooth-auth/issues/2)
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

## Project Scope

This project is developed around the author's own NixOS desktop environment.
It is intentionally not a generalized authentication framework. The included
NixOS modules are useful as working examples, but you should read the source
and adapt the PAM, polkit, locker, greetd, and session details to your own
system before relying on them.

The Bluetooth-based flow has been tested by the author and works in that
environment. Bluetooth trust still carries real risk: a connected device is
only a convenience signal. The same design can also be used as a starting point
for other trusted-device schemes if Bluetooth is not the right signal for your
machine.

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

## Device State

`bluetooth-auth` does not manage pairing. The configured device only needs to
already be paired and trusted in BlueZ. Pair it however you normally manage
Bluetooth devices on your system, then store its address in the runtime file
referenced by `config.bluetoothAddressFile`.

## NixOS Installation

Add the flake input:

```nix
{
  inputs.bluetooth-auth.url = "github:sshawn9/bluetooth-auth";
}
```

Import the NixOS module, configure the trusted user and device address, and
enable the integrations you actually want:

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
          config = {
            user = "alice";
            bluetoothAddressFile = "/run/secrets/auth_bluetooth_address";
            autoConnect.checkIntervalSeconds = 30;
            autoConnect.deviceUnvailableGraceSeconds = 300;
            autoConnect.exceptionGraceSeconds = 300;
            autoLock.checkIntervalSeconds = 40;
            autoLock.sleepAfterLockSeconds = 3;
            autoLock.exceptionGraceSeconds = 300;
            sudoAuth.timeoutSeconds = 2;
            polkitAuth.timeoutSeconds = 2;
            lockerAuth.timeoutSeconds = 2;
            greetdAuth.timeoutSeconds = 2;
          };

          # All integrations are disabled by default. Enable only the pieces
          # that match your system.
          autoConnect.enable = true;
          autoLock.enable = true;
          sudoAuth.enable = true;
          # polkitAuth.enable = true;
          # lockerAuth = {
          #   enable = true;
          #   pamService = "login";
          # };
          # greetdAuth.enable = true;
        };
      }
    ];
  };
}
```

If you manage the device address with a runtime secret manager such as
`sops-nix`, point `config.bluetoothAddressFile` at the generated secret path:

```nix
{
  sops.secrets.auth_bluetooth_address = { };

  my.security.bluetoothAuth = {
    enable = true;
    config = {
      user = "alice";
      bluetoothAddressFile = config.sops.secrets.auth_bluetooth_address.path;
    };
  };
}
```

`my.security.bluetoothAuth.enable = true` only enables the module namespace.
Every integration has its own `enable` option, and all of them default to
`false`.

Set `my.security.bluetoothAuth.config.user` whenever `autoLock`, `sudoAuth`,
`polkitAuth`, `lockerAuth`, or `greetdAuth` is enabled. If you only want
automatic reconnects, enable just that integration:

```nix
{
  my.security.bluetoothAuth = {
    enable = true;
    config.bluetoothAddressFile = "/run/secrets/auth_bluetooth_address";
    autoConnect.enable = true;
  };
}
```

## NixOS Options

**Core**

`my.security.bluetoothAuth.enable`
: Default: `false`. Enables the module namespace. Individual integrations still
  need to be enabled explicitly.

`my.security.bluetoothAuth.package`
: Default: flake package. Package that provides the command-line tools.

`my.security.bluetoothAuth.config`
: Default: `{}`. Attribute set converted to the generated JSON config passed to
  the commands.

`my.security.bluetoothAuth.config.user`
: Default: `""`. User trusted by sudo/polkit/PAM auth and targeted by
  auto-lock.

`my.security.bluetoothAuth.config.bluetoothAddressFile`
: Default: `""`. Runtime file containing the Bluetooth device address.

**Auto Connect**

`my.security.bluetoothAuth.autoConnect.enable`
: Default: `false`. Keeps the trusted device connected. The system must already
  provide `bluetooth.service`.

`my.security.bluetoothAuth.config.autoConnect.checkIntervalSeconds`
: Default: `30`. Polling interval when the device is already connected. Values
  below 5 seconds are treated as 5 seconds by the CLI.

`my.security.bluetoothAuth.config.autoConnect.deviceUnvailableGraceSeconds`
: Default: `300`. Delay when the adapter is powered off or the device is not
  paired/trusted. Values below 10 seconds are treated as 10 seconds by the CLI.

`my.security.bluetoothAuth.config.autoConnect.exceptionGraceSeconds`
: Default: `300`. Delay after a BlueZ or D-Bus error before retrying. Values
  below 10 seconds are treated as 10 seconds by the CLI.

**Auto Lock**

`my.security.bluetoothAuth.autoLock.enable`
: Default: `false`. Locks the session when the device disconnects.

`my.security.bluetoothAuth.config.autoLock.checkIntervalSeconds`
: Default: `40`. Polling interval for connection and lock checks. Values below
  5 seconds are treated as 5 seconds by the CLI.

`my.security.bluetoothAuth.config.autoLock.sleepAfterLockSeconds`
: Default: `3`. Delay after successfully starting the lock service. Values
  below 0 seconds are treated as 0 seconds by the CLI.

`my.security.bluetoothAuth.config.autoLock.exceptionGraceSeconds`
: Default: `300`. Delay after a BlueZ or D-Bus error before retrying. Values
  below 10 seconds are treated as 10 seconds by the CLI.

**sudo PAM**

`my.security.bluetoothAuth.sudoAuth.enable`
: Default: `false`. Allows the trusted user to pass `sudo` auth when the device
  is connected.

`my.security.bluetoothAuth.config.sudoAuth.timeoutSeconds`
: Default: `2`. Timeout for the sudo PAM Bluetooth check. Values above 5
  seconds are treated as 5 seconds by the CLI.

**polkit**

`my.security.bluetoothAuth.polkitAuth.enable`
: Default: `false`. Adds a polkit rule that calls the Bluetooth auth helper.

`my.security.bluetoothAuth.config.polkitAuth.timeoutSeconds`
: Default: `2`. Timeout for the polkit Bluetooth check. Values above 5 seconds
  are treated as 5 seconds by the CLI.

`my.security.bluetoothAuth.config.polkitAuth.allowedActions`
: Default: common desktop actions. polkit action ids that Bluetooth auth may
  authorize. Defaults include login1 power/session actions, systemd unit
  management, NetworkManager, UDisks mount/unlock/eject, and power profile
  controls. Set to `[]` to authorize no polkit actions.

**Locker PAM**

`my.security.bluetoothAuth.lockerAuth.enable`
: Default: `false`. Adds a PAM rule to the configured locker PAM service.

`my.security.bluetoothAuth.lockerAuth.pamService`
: Default: `"login"`. PAM service name used by the locker. This default matches
  Noctalia v4.7.7 when `NOCTALIA_PAM_SERVICE` is unset; review before enabling
  because `/etc/pam.d/login` is broader than Noctalia alone.

`my.security.bluetoothAuth.config.lockerAuth.timeoutSeconds`
: Default: `2`. Timeout for the locker PAM Bluetooth check. Values above 5
  seconds are treated as 5 seconds by the CLI.

**greetd PAM**

`my.security.bluetoothAuth.greetdAuth.enable`
: Default: `false`. Adds a PAM rule to the configured greetd PAM service.

`my.security.bluetoothAuth.greetdAuth.pamService`
: Default: `"greetd"`. PAM service name used by greetd.

`my.security.bluetoothAuth.config.greetdAuth.timeoutSeconds`
: Default: `2`. Timeout for the greetd PAM Bluetooth check. Values above 5
  seconds are treated as 5 seconds by the CLI.

## Services

The NixOS module may create these systemd units:

| Unit | Type | Purpose |
| --- | --- | --- |
| `bluetooth-auth-auto-connect.service` | system | Starts at `multi-user.target`, requires and orders after `bluetooth.service`, and orders before `greetd.service`/`display-manager.service` if those jobs exist. It then periodically calls BlueZ `Device1.Connect` for the configured device when it is paired, trusted, and disconnected. |
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

The package exposes these commands:

Each command accepts an optional path to a JSON config file. When no path is
provided, it reads the package's built-in `config.json`.

```json
{
  "user": "alice",
  "bluetoothAddressFile": "/run/secrets/auth_bluetooth_address",
  "autoConnect": {
    "checkIntervalSeconds": 30,
    "deviceUnvailableGraceSeconds": 300,
    "exceptionGraceSeconds": 300
  },
  "autoLock": {
    "checkIntervalSeconds": 40,
    "sleepAfterLockSeconds": 3,
    "exceptionGraceSeconds": 300
  },
  "sudoAuth": {
    "timeoutSeconds": 2
  },
  "polkitAuth": {
    "timeoutSeconds": 2,
    "allowedActions": [
      "org.freedesktop.login1.power-off",
      "org.freedesktop.login1.power-off-multiple-sessions",
      "org.freedesktop.login1.reboot",
      "org.freedesktop.login1.reboot-multiple-sessions",
      "org.freedesktop.login1.suspend",
      "org.freedesktop.login1.suspend-multiple-sessions",
      "org.freedesktop.login1.hibernate",
      "org.freedesktop.login1.hibernate-multiple-sessions",
      "org.freedesktop.login1.lock-sessions",
      "org.freedesktop.systemd1.manage-units",
      "org.freedesktop.systemd1.reload-daemon",
      "org.freedesktop.NetworkManager.enable-disable-network",
      "org.freedesktop.NetworkManager.enable-disable-wifi",
      "org.freedesktop.NetworkManager.network-control",
      "org.freedesktop.NetworkManager.settings.modify.own",
      "org.freedesktop.NetworkManager.settings.modify.system",
      "org.freedesktop.NetworkManager.wifi.scan",
      "org.freedesktop.udisks2.filesystem-mount",
      "org.freedesktop.udisks2.filesystem-mount-system",
      "org.freedesktop.udisks2.filesystem-unmount-others",
      "org.freedesktop.udisks2.encrypted-unlock",
      "org.freedesktop.udisks2.encrypted-unlock-system",
      "org.freedesktop.udisks2.eject-media",
      "org.freedesktop.udisks2.power-off-drive",
      "org.freedesktop.UPower.PowerProfiles.switch-profile",
      "org.freedesktop.UPower.enable-charging-limit"
    ]
  },
  "lockerAuth": {
    "timeoutSeconds": 2
  },
  "greetdAuth": {
    "timeoutSeconds": 2
  }
}
```

```console
bluetooth-auth-auto-connect /path/to/config.json
```

Keeps the configured paired and trusted BlueZ device connected. The command
exits only when interrupted. `autoConnect.checkIntervalSeconds` controls the
normal connected-device polling interval, `deviceUnvailableGraceSeconds`
controls the wait when the adapter or device is unavailable, and
`exceptionGraceSeconds` controls retries after BlueZ or D-Bus errors.

```console
bluetooth-auth-auto-lock /path/to/config.json
```

Polls the configured device connection state and starts
`noctalia-lock.service` when the device is disconnected and the configured
user's active local Wayland session is unlocked. `autoLock.checkIntervalSeconds`
controls normal polling, `sleepAfterLockSeconds` controls the delay after
starting the lock service, and `exceptionGraceSeconds` controls retries after
BlueZ or D-Bus errors.

```console
bluetooth-auth-oneshot-auth /path/to/config.json sudo
```

Checks whether the current PAM request belongs to the configured user and
whether the configured device is connected. Exit status `0` means the check
succeeded. Exit status `1` means authentication should continue to the next PAM
rule. `sudoAuth.timeoutSeconds` controls the Bluetooth check timeout.

```console
bluetooth-auth-oneshot-auth /path/to/config.json polkit
```

Checks whether the configured device is connected. The NixOS polkit rule checks
the subject user, active local session state, and `polkitAuth.allowedActions`
before calling this mode.

```console
bluetooth-auth-oneshot-auth /path/to/config.json locker
bluetooth-auth-oneshot-auth /path/to/config.json greetd
```

PAM helpers for locker and greetd integrations. They check `PAM_USER` against
the configured user and then check whether the configured device is connected.

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

- Use only a paired and trusted device you control.
- Keep the normal `sudo` password path available.
- Consider disabling `sudoAuth` if you only want auto-connect and auto-lock.
- Review the PAM rule before using this on a shared or high-risk machine.
- Prefer `config.bluetoothAddressFile` for secret-managed device addresses so
  the address is read at runtime instead of embedded in generated Nix artifacts.
- A connected Bluetooth device does not prove that the device owner is present
  or attentive.

When using `config.bluetoothAddressFile`, make sure the generated secret file
is readable by every enabled integration. The systemd services normally run as
root. The `sudoAuth` PAM helper is invoked through `pam_exec.so seteuid`, so its
effective user may need permission to read the secret file depending on your
PAM setup.

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

MIT. See [LICENSE](./LICENSE).
