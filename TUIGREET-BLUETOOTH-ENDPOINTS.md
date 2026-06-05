# BlueZ A2DP Endpoint Registration Before tuigreet

This note documents a boot-time Bluetooth auto-connect failure. The visible behavior was:

- `bluetooth-auth-auto-connect.service` started early during boot;
- the tuigreet login screen appeared, but the trusted Bluetooth device was not connected from the host side;
- `bluetooth-auth-oneshot-auth ... greetd` fell back because `Device1.Connected` was false;
- authentication only succeeded after typing the password or manually connecting to the computer from the Bluetooth device.

Current module status: the shipped NixOS module force-disables
`greetdAuth.enable`. The investigation below documents why early media
endpoints were added and what must happen if greetd authentication is enabled
in a local variant, but setting `greetdAuth.earlyMediaEndpoints.enable` alone
does not take effect in the shipped module.

## Symptom

The early boot logs showed auto-connect calling BlueZ immediately:

```text
bluetooth-auth-auto-connect: device is disconnected; requesting BlueZ connection
bluetoothd: src/service.c:btd_service_connect() a2dp-source profile connect failed for AA:BB:CC:DD:EE:FF: Protocol not available
bluetooth-auth-auto-connect: BlueZ or D-Bus error; retrying ...: DBusError: br-connection-unknown
```

The important line is:

```text
a2dp-source profile connect failed ... Protocol not available
```

This does not mean the Bluetooth address is wrong. It means BlueZ attempted to connect an audio profile before the system-side A2DP media endpoint was available.

## Timeline Investigation

This issue cannot be diagnosed by checking whether one unit is active. The important part is the relative order of these events:

- when BlueZ starts;
- when auto-connect first calls BlueZ `Device1.Connect()`;
- what BlueZ reports for that failed connect attempt;
- when tuigreet/greetd enters PAM authentication;
- whether the trusted user's user manager exists before login;
- whether PipeWire/WirePlumber sockets are merely listening or the services are actually running;
- when WirePlumber registers A2DP endpoints with BlueZ.

### 1. Start with a coarse boot timeline

First filter the boot journal into one approximate timeline:

```sh
journalctl -b --no-pager -o short-precise \
  | rg 'bluetooth-auth-auto-connect|bluetooth-auth-greetd-auth|bluetoothd|greetd|user@[0-9]+\.service|linger-users|pipewire|wireplumber|Endpoint registered|a2dp-source|Protocol not available'
```

This command is not meant to be the final proof. It tells whether the failure happens before or after login. A problematic timeline looks like this:

```text
bluetoothd: Starting SDP server
bluetooth-auth-auto-connect: starting ...
bluetooth-auth-auto-connect: device is disconnected; requesting BlueZ connection
bluetoothd: ... a2dp-source profile connect failed ... Protocol not available
bluetooth-auth-auto-connect: BlueZ or D-Bus error; retrying ...
greetd: ...
bluetooth-auth-greetd-auth: trusted device is not connected; falling back ...
...
pipewire: ...
wireplumber: ...
bluetoothd: Endpoint registered: ... /MediaEndpoint/A2DPSource/...
```

If auto-connect and greetd authentication both happen before `Endpoint registered`, then the host side is trying to connect before the A2DP endpoint exists.

### 2. Confirm the auto-connect failure point

Check the auto-connect unit:

```sh
journalctl -b -u bluetooth-auth-auto-connect.service --no-pager -o short-precise
```

Look for:

- `starting ...`: when the service starts;
- `device is disconnected; requesting BlueZ connection`: when it calls BlueZ `Device1.Connect()`;
- `BlueZ or D-Bus error; retrying ...`: when the failed call enters retry delay.

Then compare it with the BlueZ daemon log:

```sh
journalctl -b -u bluetooth.service --no-pager -o short-precise \
  | rg 'AA:BB|connect|failed|a2dp|br-|Protocol not available|Endpoint registered'
```

The key line in this case was:

```text
bluetoothd: src/service.c:btd_service_connect() a2dp-source profile connect failed for AA:BB:CC:DD:EE:FF: Protocol not available
```

This proves BlueZ received the connect request, but the system-side protocol endpoint for the A2DP profile was unavailable.

### 3. Confirm the greetd authentication point

The greetd unit shows when the login service is active:

```sh
journalctl -b -u greetd.service --no-pager -o short-precise
```

The Bluetooth PAM helper logs under its syslog identifier:

```sh
journalctl -b -t bluetooth-auth-greetd-auth --no-pager -o short-precise
```

Look for:

```text
bluetooth-auth-greetd-auth: starting mode=greetd ...
bluetooth-auth-greetd-auth: trusted device is not connected; falling back to the next auth rule
```

If these lines appear before `Endpoint registered`, the problem is not the PAM rule ordering. The auth check is simply happening before the BlueZ device is connected.

### 4. Confirm the user manager and lingering state

To start user-level PipeWire/WirePlumber before login, the trusted user's systemd user manager must exist before login. First find the UID:

```sh
id -u alice
```

Assuming the UID is `1000`, check:

```sh
journalctl -b -u user@1000.service -u linger-users.service --no-pager -o short-precise
```

This confirms:

- whether `linger-users.service` started;
- whether `user@1000.service` existed before login.

This step only proves the user manager exists. It does not prove PipeWire/WirePlumber services are actually running.

### 5. Distinguish PipeWire sockets from services

The misleading part in the original investigation was that PipeWire sockets appeared early, but the service bodies did not start.

Check the user-unit logs:

```sh
journalctl -b --user \
  -u pipewire.socket \
  -u pipewire-pulse.socket \
  -u pipewire.service \
  -u wireplumber.service \
  --no-pager -o short-precise
```

These lines only mean sockets are listening:

```text
Listening on PipeWire Multimedia System Sockets.
Listening on PipeWire PulseAudio.
```

They do not mean PipeWire/WirePlumber are running.

The meaningful lines are:

```text
Started PipeWire Multimedia Service.
Started Multimedia Service Session Manager.
```

If these appear only after the user typed the password and entered the desktop, the media services were not actually running before login.

### 6. Confirm A2DP endpoint registration

Finally, check when BlueZ received media endpoint registrations from WirePlumber:

```sh
journalctl -b -u bluetooth.service --no-pager -o short-precise \
  | rg 'Endpoint registered|MediaEndpoint|A2DP'
```

Typical output:

```text
bluetoothd: Endpoint registered: sender=:... path=/MediaEndpoint/A2DPSource/...
```

The conclusion is direct:

- `a2dp-source profile connect failed ... Protocol not available` appears first;
- `bluetooth-auth-greetd-auth: trusted device is not connected` appears next;
- `Endpoint registered` appears only after entering the desktop;

therefore A2DP endpoints were not registered during the tuigreet stage, so host-side auto-connect failed for the audio device.

### 7. Final observed order

The investigation established this order:

1. `bluetooth.service` started;
2. `bluetooth-auth-auto-connect.service` started;
3. auto-connect called BlueZ `Device1.Connect()`;
4. BlueZ attempted the A2DP profile and failed with `Protocol not available`;
5. tuigreet/greetd entered PAM authentication;
6. `bluetooth-auth-greetd-auth` saw the device disconnected and fell back;
7. the user typed the password and entered the desktop;
8. the actual PipeWire/WirePlumber services started;
9. WirePlumber registered A2DP endpoints with BlueZ;
10. the device became connected later, either from a device-side connect or an auto-connect retry.

## Root Cause

BlueZ `Device1.Connect()` is not just an abstract link operation. For audio devices it may connect profiles such as A2DP. A2DP requires PipeWire/WirePlumber to register media endpoints with BlueZ at runtime.

The easy traps are:

- `pipewire.socket` being active does not mean `pipewire.service` is running;
- `pipewire.service` running does not by itself prove WirePlumber has registered BlueZ A2DP endpoints;
- A2DP endpoint registration is a runtime D-Bus registration by WirePlumber, not a stable systemd readiness state;
- an early user manager does not necessarily start PipeWire/WirePlumber unless those services are wanted by the user `default.target`.

Therefore, ordering `bluetooth-auth-auto-connect.service` after `bluetooth.service` is not enough. BlueZ is available, but the user-space media stack that provides A2DP endpoints may still be inactive.

## Fix When greetd Auth Is Locally Enabled

The module contains an option for this behavior:

```nix
my.security.bluetoothAuth.greetdAuth.earlyMediaEndpoints.enable = true;
```

In the shipped module this option is gated by `greetdAuth.enable`, and
`greetdAuth.enable` is forced to `false` by the root bluetooth-auth module.
Therefore the option only applies if that force-disable is changed locally.

When active, it expands to the relevant behavior:

```nix
users = lib.mkIf earlyMediaEndpoints {
  manageLingering = lib.mkDefault true;
  users.${trustedUser}.linger = lib.mkDefault true;
};

systemd.user.services = lib.mkIf earlyMediaEndpoints {
  pipewire.wantedBy = [ "default.target" ];
  wireplumber.wantedBy = [ "default.target" ];
};
```

This means:

- enable lingering for the trusted user, so the user systemd manager can run before login;
- attach `pipewire.service` and `wireplumber.service` to the user `default.target`;
- when the user manager is started before login, PipeWire and WirePlumber start as real services instead of leaving only sockets active;
- WirePlumber can register A2DP endpoints with BlueZ before tuigreet authentication.

If locally enabled, the option belongs under the NixOS `greetdAuth` module, not under the generated JSON `settings`:

```nix
my.security.bluetoothAuth = {
  enable = true;
  user = "alice";
  bluetoothAddressFile = config.sops.secrets.trusted_bluetooth_address.path;

  autoConnect = {
    enable = true;
    checkIntervalSeconds = 5;
    deviceUnvailableGraceSeconds = 10;
    exceptionGraceSeconds = 10;
  };

  # Requires locally allowing greetdAuth.enable. The shipped module forces it
  # to false.
  greetdAuth = {
    enable = true;
    earlyMediaEndpoints.enable = true;
  };
};
```

## Verification When Locally Enabled

After rebuilding and rebooting, verify that the user services are wanted by `default.target`:

```sh
ls -l /etc/systemd/user/default.target.wants
systemctl --user show pipewire.service wireplumber.service -p WantedBy -p UnitFileState
```

Verify that PipeWire/WirePlumber start before login:

```sh
journalctl -b --user -u pipewire.service -u wireplumber.service --no-pager -o short-precise
```

Verify that BlueZ endpoints appear before the greetd auth attempt:

```sh
journalctl -b -u bluetooth.service --no-pager -o short-precise | rg 'Endpoint registered'
journalctl -b -u bluetooth-auth-auto-connect.service -u greetd.service --no-pager -o short-precise
```

The expected order after the fix is:

1. the trusted user's user manager starts;
2. `pipewire.service` and `wireplumber.service` start;
3. BlueZ logs `Endpoint registered`;
4. a later auto-connect retry can connect successfully;
5. if greetd Bluetooth authentication is locally enabled, pressing Enter at
   tuigreet can pass Bluetooth authentication.

## Limitations

When enabled locally, this starts part of the trusted user's user-service graph before login. That is acceptable for this project's target environment, but it should not be treated as a universal security default.

This also does not provide a hard readiness signal for “A2DP endpoints are registered”. Endpoint registration is still a runtime event, so auto-connect still needs retry logic. The goal is to make endpoint registration happen early enough for tuigreet.

## Passwordless Login and the Login Keyring

When Bluetooth authentication is locally enabled and succeeds at the tuigreet stage, the PAM stack is effectively accepted early by `bluetooth-auth-greetd-auth` as a `sufficient` rule. The user did not type the login password, so later components that depend on that password do not receive the required secret material.

The most visible example is GNOME Keyring's login keyring:

```text
The login keyring did not get unlocked when you logged into your computer.
```

This is not a VS Code issue and not a polkit issue. The login keyring is normally unlocked by `pam_gnome_keyring`, which uses the password typed during login. With Bluetooth passwordless login, the PAM flow has no plaintext login password, so `pam_gnome_keyring` cannot decrypt the login keyring. After entering the desktop, applications using Secret Service may show a keyring unlock prompt.

This makes passwordless tuigreet login more expensive than sudo or polkit passwordless authorization:

- sudo/polkit passwordless auth affects one authorization request;
- tuigreet passwordless auth changes the login semantics of the whole desktop session;
- components that derive keys from the login password do not automatically work;
- login keyring, browser secret storage, and libsecret applications may be affected.

The available choices all have tradeoffs:

- keep the login keyring password: type it once after entering the desktop; this keeps the strongest at-rest protection;
- set an empty login keyring password: no prompt, but the keyring file loses its own password encryption and relies on file permissions and disk encryption;
- use a separate login keyring password stored in sops and unlock it with a user-session helper: better isolation than an empty password, but if the machine can automatically decrypt sops, it can also automatically unlock the keyring;
- store the real login password in sops: not recommended, because the login password has too many other uses and should not gain a larger exposure surface.

So Bluetooth passwordless login at tuigreet can work in a locally enabled setup, but it is not merely “typing one less password”. It removes the automatic unlock condition for desktop secret storage that expects the login password, and that cost must be accepted or handled with an explicit keyring unlock strategy.
