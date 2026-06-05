# Group Access for polkit Helpers Reading sops Secrets

This note documents a polkit Bluetooth-auth failure. sudo authentication worked, but polkit kept failing with:

```text
bluetooth-auth-polkit-auth: failed to load settings or auth context: PermissionError: [Errno 13] Permission denied: '/run/secrets/trusted_bluetooth_address'
```

## Symptom

The `bluetooth-auth` polkit rule checks the subject and then calls the helper:

```js
polkit.spawn([
  "/nix/store/.../bin/bluetooth-auth-oneshot-auth",
  "/nix/store/...-bluetooth-auth-settings.json",
  "polkit"
]);
```

The helper reads the runtime file referenced by the generated JSON settings:

```python
Path(settings["bluetoothAddressFile"]).read_text(encoding="utf-8").strip()
```

When `bluetoothAddressFile` points to a sops-nix secret:

```nix
config.sops.secrets.trusted_bluetooth_address.path
```

the runtime path is usually:

```text
/run/secrets/trusted_bluetooth_address
```

The key detail is that the polkit helper is not a normal root-owned systemd service. It is spawned by `polkitd`.

## Root Cause

`polkitd` normally runs as `polkituser`:

```sh
systemctl show polkit.service -p User
```

Typical output:

```text
User=polkituser
```

When `polkit.spawn()` runs the helper, the helper inherits the polkit daemon execution context. Therefore `polkituser` must be able to read `/run/secrets/trusted_bluetooth_address`.

If the sops-nix secret has restrictive permissions like:

```text
-r-------- nobody nogroup /run/secrets/trusted_bluetooth_address
```

then `polkituser` cannot read it. The helper fails before it can check the BlueZ connection state.

## Fix

Create a dedicated group for bluetooth-auth and add every runtime identity that needs to read the secret:

```nix
users.groups.bluetooth-auth.members = [
  "alice"
  "polkituser"
];

sops.secrets.trusted_bluetooth_address = {
  group = "bluetooth-auth";
  mode = "0440";
};
```

This means:

- the secret is not exposed to all users;
- group members can read the secret;
- `polkituser` can read the Bluetooth address file, so the polkit helper can run;
- the trusted user is also included to avoid permission mismatches across PAM helper execution modes such as different `pam_exec` / `seteuid` combinations.

Do not make this secret world-readable. A Bluetooth address is not a high-entropy password, but it is still the trusted-device identifier and should only be readable by local identities that need it.

## Verification

Check the file mode and group:

```sh
ls -l /run/secrets/trusted_bluetooth_address
```

Expected shape:

```text
-r--r----- ... bluetooth-auth ... /run/secrets/trusted_bluetooth_address
```

Check that `polkituser` can read it:

```sh
sudo -u polkituser test -r /run/secrets/trusted_bluetooth_address && echo ok
```

Check whether the PermissionError is gone:

```sh
journalctl -b | rg 'bluetooth-auth-polkit-auth|PermissionError'
```

After the fix, the helper should reach the normal auth path:

```text
bluetooth-auth-polkit-auth: starting mode=polkit ...
bluetooth-auth-polkit-auth: trusted device is connected; accepting authentication
```

Then actions such as this can be authorized by the polkit rule:

```sh
systemctl reload systemd-journald
```

This still requires the action id to be listed in `polkitAuth.allowedActions`, the subject user to match the configured trusted user, and the session to be local and active.

## Why sudo Worked but polkit Failed

sudo PAM and polkit use different authentication paths:

- sudo uses `/etc/pam.d/sudo`, where PAM calls the helper;
- polkit uses the `polkitd` JavaScript rule, where `polkit.spawn()` calls the helper.

Those paths run under different identities. As a result, the same `/run/secrets/...` file can be readable in the sudo path but unreadable in the polkit path.

So polkit passwordless authentication depends on two things: the trusted Bluetooth device must be connected, and the polkit helper must be able to read its runtime settings and secret.
