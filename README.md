# bluetooth-auth

[![CI](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml/badge.svg)](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml)
[![Renovate](https://img.shields.io/badge/renovate-enabled-brightgreen.svg)](https://github.com/sshawn9/bluetooth-auth/issues/2)
[![English](https://img.shields.io/badge/lang-English-blue)](./README.md)
[![简体中文](https://img.shields.io/badge/lang-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-red)](./README.zh-CN.md)

Use an iPhone's system Bluetooth support for passwordless authentication, on-demand connection, and Noctalia automatic locking on NixOS. Runtime programs are Rust and system integration is provided by NixOS modules; The Python BLE prototypes remain in the experiment archive.

The project is built around one specified user, one phone, and the `hci0` adapter. A Bluetooth connection is a convenient authentication condition: when the phone reconnects, passwordless eligibility returns, while unlocking or privilege escalation still requires user action. Normal password authentication remains the fallback.

## How it works

The connection check reads Linux's current connection table and requires exactly one connected, encrypted LE link for the target address.

- When connected, it succeeds immediately.
- When disconnected and waiting is allowed, it temporarily registers a Consumer Control HID over GATT service and advertises it, waiting for the paired iPhone to reconnect and encrypt the link.
- After an attempt succeeds, fails, or times out, it releases the temporary HID service and advertisement. It does not request a Bluetooth disconnect.

Connection helpers share one lock. For a timed connection attempt, waiting for the lock, service registration, advertising, and waiting for a connection all consume the same attempt budget; an attempt does not retry internally.

The HID service uses the computer's existing Bluetooth adapter and identity and can coexist with other Bluetooth capabilities. It sends no key reports and does not stop audio services. Completed hardware validation and its limits are recorded in the [iPhone BLE experiment archive](experiments/iphone_ble/README.md); a short test is not a guarantee for every device or iOS version.

## Prerequisites and initial pairing

Linux, a running BlueZ, and powered `hci0` are required. The NixOS module does not replace system Bluetooth configuration. Automatic locking requires Noctalia v5. Optional Keyring unlocking requires GNOME Keyring, SOPS, and a decryption key available to the user.

**Initial pairing must happen while the HID service is running. An existing ordinary audio pairing cannot be assumed to work with this HID/LE path.**

Build the tools in the repository:

```sh
nix build .
```

Open the computer's Bluetooth manager first and keep it available to confirm pairing. The helper requires the shared lock file to exist. Prepare it if the NixOS module has not already done so, then run:

```sh
sudo mkdir -p /run/bluetooth-auth
sudo touch /run/bluetooth-auth/hci0.lock
sudo ./result/bin/bluetooth-auth-hid-server
```

The helper waits for the shared lock before changing Bluetooth settings and holds it until cleanup finishes, preventing background connection tasks from registering another HID advertisement at the same time. It temporarily disables Classic Bluetooth discoverability, enables pairing without a timeout, and starts a discoverable HID LE advertisement. Audio services remain registered. **Do not re-enable the computer's discoverability switch while the helper is running.**

In iPhone **Settings → Bluetooth**, let the device list refresh, select the computer's current name, and confirm pairing on both sides. The helper uses the Bluetooth manager's existing pairing agent; it does not accept pairing for you. If an old pairing only works for audio, remove that phone's old pairing manually and pair again while the HID service is running.

Press `Ctrl+C` after pairing. This tool has no timeout and does not exit automatically after pairing. On `Ctrl+C`, `SIGTERM`, or a setup error, it releases temporary HID and advertising resources, attempts to restore the original discoverable, connectable, pairable, and pairing-timeout settings, then releases the lock. Pairing records and the shared lock file are retained. Successful restoration prints `Original adapter settings restored; pairing records retained.`; restoration failures are reported on stderr and produce a nonzero exit status.

Restoration recovers the setting values, not the remaining time of an existing discoverability or pairing window; timed windows restart. `SIGKILL` (`kill -9`) and power loss prevent cleanup from running.

Obtain the phone's identity address from the computer's Bluetooth manager after pairing. Put exactly one identity address in a runtime file, on one line, and point later configuration at that file. Use the phone identity address, not the computer adapter address or a temporary random address.

## NixOS integration

Merge the following into your flake while retaining your existing system configuration. Replace `alice` and the address-file path. Complete the HID pairing above and prepare the address file before enabling authentication.

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    bluetooth-auth.url = "github:sshawn9/bluetooth-auth";
  };

  outputs = { nixpkgs, bluetooth-auth, ... }: {
    nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        bluetooth-auth.nixosModules.default
        {
          hardware.bluetooth.enable = true;

          my.security.bluetoothAuth = {
            enable = true;
            trustedUser = "alice";
            device.address.file = "/run/secrets/bluetooth_address";

            connection.timeoutMs = 7000;
            autoConnect.enable = true;
            auth.sudo.enable = true;

            # Enable as appropriate for the environment.
            # auth.polkit.enable = true;
            # auth.locker.enable = true;
            # auth.locker.pamService = "login";
            # auth.greetd.enable = true;
            # noctaliaAutoLock.enable = true;
          };
        }
      ];
    };
  };
}
```

The main switch places the package commands on the system `PATH`; individual integrations remain disabled by default. `trustedUser` can be omitted when using only system-level automatic connection. Specify it for authentication, Noctalia automatic locking, or Keyring unlocking.

### Device address and SOPS

A system that already imports the sops-nix NixOS module can name an existing secret directly; this project does not require a separate SOPS module import:

```nix
{
  sops.secrets.bluetooth_address = { };
  my.security.bluetoothAuth.device.address.sopsSecretName = "bluetooth_address";
}
```

`device.address.sopsSecretName` takes precedence over `device.address.file`, uses the secret's runtime path, and sets `group = cfg.accessGroup` and `mode = "0440"`. The default group is `bluetooth-auth-connect`; the module adds the configured user and, with polkit enabled, `polkituser` to it.

Providing `device.address.file` directly is also supported, but the caller manages its group, permissions, and availability. User services need the configured user to read it; polkit also needs `polkituser` to read it. The file must be available before the relevant program starts, and its contents are not passed in command-line arguments.

## Authentication and automatic connection

sudo, locker, and greetd use PAM; polkit uses its own authorization rule. They all invoke `bluetooth-auth-link --connect -1`:

| Check result | Current authentication | Later action |
| --- | --- | --- |
| Target encrypted LE link is connected | Passes when the user and entry conditions match | No connection needed |
| Target is disconnected | Continues with ordinary password authentication | Requests one background connection attempt for a later authentication |
| Check or request fails | Continues with ordinary password authentication | Reports the runtime error |

Authentication entry points do not wait for the background connection. A later successful connection cannot turn the Bluetooth check that already returned failure into success.

PAM rules are restricted to the configured user; sudo also handles that user as the requesting user. polkit additionally requires an active local session and an action in `auth.polkit.allowedActions`. Its default list covers selected power, systemd, NetworkManager, UDisks, and UPower actions; see the complete list in the [polkit module](modules/nixos/polkit-auth.nix). Set it to `[]` to authorize no actions through Bluetooth.

`auth.greetd` can be enabled directly. `auth.locker.pamService` defaults to `login` and must match the PAM service used by the locker. Changing a shared `login` PAM service also affects other entry points that use it.

With `autoConnect` enabled, a connection is also attempted in advance at these points:

| Event | Execution |
| --- | --- |
| BlueZ starts or restarts | systemd starts the existing one-shot connection service |
| Suspend, hibernation, hybrid sleep, or suspend-then-hibernate completes successfully | The sleep service's `OnSuccess` starts the one-shot connection service |
| `hci0` `Powered` changes to `true` | `bluetooth-auth-power-monitor` directly calls the Rust connection function |

The power monitor blocks on D-Bus messages while idle. During a connection attempt it waits for that attempt to finish, then resumes listening; failures and timeouts are not retried automatically. `autoConnect` does not add periodic reconnection or a Noctalia idle-resume hook.

## Noctalia automatic locking

With `noctaliaAutoLock` enabled, a systemd user service for the specified user runs continuously after `graphical-session.target` starts.

Each iteration reads Noctalia's lock state and calls `query_or_connect`. If the session is unlocked and the phone still cannot connect, it calls `noctalia msg session lock`, then queries the lock state again after 300 ms. It then sleeps according to the state after that action:

| State after the action | Default sleep |
| --- | --- |
| Unlocked, connected | 30 seconds |
| Unlocked, disconnected (lock not yet confirmed) | 30 seconds |
| Locked, connected | 120 seconds |
| Locked, disconnected | 60 seconds |

While locked it still checks and connects the phone as needed; restoring a connection does not unlock the session. This is a persistent process with dynamic sleeps, not a systemd timer.

Noctalia must be available in the user environment. Before starting `graphical-session.target`, import `WAYLAND_DISPLAY` into the user systemd environment; the service also uses that user's `XDG_RUNTIME_DIR`. The NixOS module defines the user service and limits it with `ConditionUser`; no Home Manager module is needed.

## GNOME Keyring unlocking

Bluetooth passwordless login does not supply a login password, so `pam_gnome_keyring` may not unlock the login keyring. The optional `gnomeKeyringUnlock` runs once after the graphical session starts and retains the existing password-login unlock path.

```nix
{
  services.gnome.gnome-keyring.enable = true;

  my.security.bluetoothAuth.gnomeKeyringUnlock = {
    enable = true;
    password.sopsFile = ./keyring.enc.yaml;
    password.sopsField = "login_keyring_password";
    password.ageKeyFile = "/home/alice/.config/sops/age/keys.txt";
  };
}
```

The selected top-level string in the SOPS file holds the **existing login-keyring password**. A password hash cannot substitute for the decryption password. This depends on the earlier user and Bluetooth-address configuration; the user must be able to read the encrypted file and its decryption key.

The program first queries the login keyring and exits if it is already unlocked. If still locked, it checks or attempts to establish the specified phone's encrypted LE connection. Only when that condition holds does it decrypt the password with SOPS and pass it to GNOME Keyring. It skips normally when Bluetooth is disconnected, exits with an error for runtime failures, and does not retry automatically.

It calls the SOPS tool itself. Decrypted output remains in process memory and goes to D-Bus; it creates no plaintext password runtime file and does not place the password in arguments or environment variables. Unlocking uses GNOME Keyring's specific interface and requires an existing login keyring.

## NixOS options

All paths are relative to `my.security.bluetoothAuth`.

| Option | Default | Description |
| --- | --- | --- |
| `enable` | `false` | Installs tools and enables module configuration; enable each integration separately. |
| `package` | flake package | Package providing the five Rust programs. |
| `trustedUser` | `""` | User permitted for passwordless authentication and user services. |
| `accessGroup` | `"bluetooth-auth-connect"` | Access group for the connection socket, lock file, and optional address file. |
| `device.address.file` | `""` | Runtime file containing the phone identity address. |
| `device.address.sopsSecretName` | `null` | Name of the referenced sops-nix secret; overrides the address path and configures group read permission. |
| `connection.timeoutMs` | `7000` | Per-attempt budget for background connection, the power monitor, and user services. |
| `autoConnect.enable` | `false` | Connect in advance at boot, BlueZ restart, sleep resume, and Bluetooth power-on. |
| `auth.sudo.enable` | `false` | sudo PAM integration. |
| `auth.polkit.enable` | `false` | polkit authorization integration. |
| `auth.polkit.allowedActions` | desktop action list in the module | polkit actions permitted through Bluetooth. |
| `auth.locker.enable` | `false` | Locker PAM integration. |
| `auth.locker.pamService` | `"login"` | PAM service used by the locker. |
| `auth.greetd.enable` | `false` | greetd PAM integration. |
| `auth.greetd.pamService` | `"greetd"` | PAM service used by greetd. |
| `noctaliaAutoLock.enable` | `false` | Enable the Noctalia automatic-lock user service. |
| `noctaliaAutoLock.sleepIntervalsMs.unlockedConnected` | `30000` | Sleep after an unlocked, connected check. |
| `noctaliaAutoLock.sleepIntervalsMs.unlockedDisconnected` | `30000` | Sleep after an unlocked, disconnected check. |
| `noctaliaAutoLock.sleepIntervalsMs.lockedConnected` | `120000` | Sleep after a locked, connected check. |
| `noctaliaAutoLock.sleepIntervalsMs.lockedDisconnected` | `60000` | Sleep after a locked, disconnected check. |
| `gnomeKeyringUnlock.enable` | `false` | Enable automatic GNOME login-keyring unlocking. |
| `gnomeKeyringUnlock.password.sopsFile` | required when enabled | SOPS-encrypted file holding the existing keyring password. |
| `gnomeKeyringUnlock.password.sopsField` | `"login_keyring_password"` | Top-level string field in the SOPS file. |
| `gnomeKeyringUnlock.password.ageKeyFile` | `null` | User age-key path; when unset, SOPS uses its own key-discovery mechanism. |

## Command-line tools

When the module is enabled, all five commands are directly available. Their build-directory counterparts are in `./result/bin/`; Cargo outputs are in `./target/release/`.

| Command | Purpose |
| --- | --- |
| `bluetooth-auth-link` | Query the target encrypted LE connection, optionally wait for a connection, or notify the background service. |
| `bluetooth-auth-hid-server` | Argument-free manual pairing helper; holds the shared lock, temporarily hides Classic discovery, and provides HID LE advertising until stopped. |
| `bluetooth-auth-noctalia-auto-lock` | Continuously check, connect as needed, and lock in a Noctalia user session. |
| `bluetooth-auth-keyring-unlock` | Unlock a GNOME login keyring through SOPS when needed. |
| `bluetooth-auth-power-monitor` | Listen for `hci0` Bluetooth power-on events and attempt a connection directly. |

All commands except the argument-free HID pairing tool provide `--help`.

### Querying and connecting

```sh
bluetooth-auth-link --address-file /run/secrets/bluetooth_address --connect 0
bluetooth-auth-link --address-file /run/secrets/bluetooth_address --connect 7000
bluetooth-auth-link --address-file /run/secrets/bluetooth_address --connect -1
```

| `--connect` | Connected | Disconnected |
| --- | --- | --- |
| `0` | Exit `0` | Exit `1`; query only |
| Positive, such as `7000` | Exit `0` | Attempt once and wait; the positive value is the millisecond budget |
| Negative, conventionally `-1` | Exit `0` | Notify the background service; this invocation still exits `1` |

Without `--connect`, the default is `15000`. A negative magnitude does not set a timeout; the background service uses Nix's `connection.timeoutMs`. No connection and an ordinary timeout are silent, runtime errors go to stderr, and invalid arguments exit `2`.

Synchronous connections and the manual HID pairing helper use `/run/bluetooth-auth/hci0.lock`. The module creates it when automatic connection or any authentication/user-service integration is enabled; enabling only the main module switch does not create it. Keep the shared file between runs and never delete or replace it while in use. Asynchronous mode also requires `/run/bluetooth-auth/connect.sock`. The socket is enabled by sudo, polkit, locker, greetd, Noctalia automatic locking, or Keyring integration; `autoConnect` alone does not create it.

When testing build outputs manually without the NixOS module, prepare the lock file and run one connection attempt as root:

```sh
sudo mkdir -p /run/bluetooth-auth
sudo touch /run/bluetooth-auth/hci0.lock
sudo ./result/bin/bluetooth-auth-link --address-file /path/to/bluetooth-address --connect 7000
```

### User services and power monitor

Before running automatic locking manually, stop an enabled `bluetooth-auth-auto-lock.service` user service to avoid duplicate processes. Run it in the current Noctalia user session:

```sh
bluetooth-auth-noctalia-auto-lock --address-file /run/secrets/bluetooth_address --timeout-ms 7000
```

`--help` lists the four state-specific `--*-interval-ms` options. Manual operation also needs access to the address file and shared lock.

Stop an enabled `bluetooth-auth-power-monitor.service` system service before running the power monitor manually:

```sh
sudo bluetooth-auth-power-monitor --address-file /run/secrets/bluetooth_address --timeout-ms 7000
```

This command needs system D-Bus permission to acquire `org.bluetooth_auth.PowerMonitor`; the `autoConnect` module supplies that configuration. It responds only to later `Powered=true` signals and does not make an extra connection attempt at startup.

### Keyring command

Run it in the user session, with `sops` on `PATH`:

```sh
bluetooth-auth-keyring-unlock --sops-file /path/to/keyring.enc.yaml --sops-key login_keyring_password
```

It does not check Bluetooth by default. Add `--address-file /run/secrets/bluetooth_address --timeout-ms 7000` to require the target connection. The NixOS Keyring integration passes an address file.

Exit `0` means normal completion, including skipping an unlock because the Bluetooth condition is unmet. Runtime failures exit `1`; invalid arguments exit `2`.

## systemd services and troubleshooting

| Unit | Scope | Purpose |
| --- | --- | --- |
| `bluetooth-auth-connect.service` | system | One connection task with a time budget. |
| `bluetooth-auth-connect.socket` | system | Receives asynchronous connection requests and activates the one-shot task. |
| `bluetooth-auth-power-monitor.service` | system | Bluetooth power-on event monitor. |
| `bluetooth-auth-auto-lock.service` | user | Noctalia automatic-lock loop. |
| `bluetooth-auth-keyring-unlock.service` | user | One Keyring unlock after graphical-session startup. |

View logs for enabled integrations:

```sh
journalctl -b -u bluetooth-auth-connect.service -u bluetooth-auth-power-monitor.service
journalctl --user -b -u bluetooth-auth-auto-lock.service -u bluetooth-auth-keyring-unlock.service
```

An ordinary failure to connect can leave the connection service in a nonzero/`failed` systemd state; that does not by itself mean the program crashed. Read stderr for the cause. For connection problems, first check the HID/LE pairing, address file, `hci0` power, and shared lock.

For Noctalia problems, run `noctalia msg status` in the relevant user session and inspect `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR` in `systemctl --user show-environment`. For Keyring problems, confirm that the login keyring exists, the user's SOPS key decrypts the selected file, and the stored value is the current keyring password.

## Building, testing, and experiments

```sh
nix build .
nix flake check
```

Development environment and Rust tests:

```sh
nix develop
cargo build --release --locked
cargo test --locked
```

Building outside Nix requires Rust/Cargo, a C compiler, `pkg-config`, and D-Bus development files; tests also require `dbus-daemon`. Runtime tools do not require Python.

Rust tests use a private D-Bus and simulated HCI. They cover connection criteria, HID identity checks, lock contention, timeout cleanup, Bluetooth power-on events, and Noctalia flows. The flake also contains isolated integration checks for authentication, connection scheduling, and Keyring unlocking.

The [iPhone BLE experiment directory](experiments/iphone_ble/README.md) keeps successful and unsuccessful ANCS, CTS, and HID records, Python prototypes, and reproduction steps. The [results report](experiments/iphone_ble/RESULTS.md) describes the scope of hardware validation. Commands in the experiment archive and the current Rust programs are maintained separately.

## License

MIT. See [LICENSE](LICENSE).
