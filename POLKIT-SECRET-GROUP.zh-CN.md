# polkit Helper 读取 sops Secret 的 Group 权限问题

本文记录 polkit 蓝牙认证失败的问题。现象是 sudo 可以免密通过，但 polkit 一直失败，并且日志里出现：

```text
bluetooth-auth-polkit-auth: failed to load settings or auth context: PermissionError: [Errno 13] Permission denied: '/run/secrets/trusted_bluetooth_address'
```

## 现象

`bluetooth-auth` 的 polkit rule 会先检查 subject，再调用 helper：

```js
polkit.spawn([
  "/nix/store/.../bin/bluetooth-auth-oneshot-auth",
  "/nix/store/...-bluetooth-auth-settings.json",
  "polkit"
]);
```

helper 随后读取 JSON settings 里的运行时文件：

```python
Path(settings["bluetoothAddressFile"]).read_text(encoding="utf-8").strip()
```

如果 `bluetoothAddressFile` 指向 sops-nix secret，例如：

```nix
config.sops.secrets.trusted_bluetooth_address.path
```

那么实际路径通常是：

```text
/run/secrets/trusted_bluetooth_address
```

问题是 polkit helper 并不是以 root 的普通 systemd service 形态运行，它是由 `polkitd` 调用的。

## 根因

`polkitd` 通常运行在 `polkituser` 用户下：

```sh
systemctl show polkit.service -p User
```

可以看到类似：

```text
User=polkituser
```

`polkit.spawn()` 调用 helper 时，helper 继承的是 polkit daemon 的执行上下文。因此 helper 需要 `polkituser` 能读取 `/run/secrets/trusted_bluetooth_address`。

而 sops-nix secret 如果默认是类似下面的权限：

```text
-r-------- nobody nogroup /run/secrets/trusted_bluetooth_address
```

那么 `polkituser` 没有读权限。于是 helper 还没开始检查 BlueZ 连接状态，就在读取蓝牙地址文件时失败。

## 修复方式

为 bluetooth-auth 创建专用 group，并让需要读取 secret 的运行身份加入这个 group：

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

含义是：

- secret owner 仍不需要暴露给普通用户；
- group 成员可以读取该 secret；
- `polkituser` 可以读取蓝牙地址文件，因此 polkit helper 可以正常运行；
- 受信用户也加入 group，可以避免 PAM helper 在不同 `pam_exec` / `seteuid` 组合下出现权限不一致。

不要把该 secret 改成 world-readable。蓝牙地址不是高熵密码，但它仍然是受信设备标识，应该只暴露给明确需要读取它的本地身份。

## 验证

检查文件权限：

```sh
ls -l /run/secrets/trusted_bluetooth_address
```

期望看到类似：

```text
-r--r----- ... bluetooth-auth ... /run/secrets/trusted_bluetooth_address
```

检查 `polkituser` 是否能读取：

```sh
sudo -u polkituser test -r /run/secrets/trusted_bluetooth_address && echo ok
```

检查日志里是否还存在 PermissionError：

```sh
journalctl -b | rg 'bluetooth-auth-polkit-auth|PermissionError'
```

修复后，polkit helper 应该能进入正常认证流程：

```text
bluetooth-auth-polkit-auth: starting mode=polkit ...
bluetooth-auth-polkit-auth: trusted device is connected; accepting authentication
```

此时类似下面的操作可以由 polkit rule 放行：

```sh
systemctl reload systemd-journald
```

前提是对应 action id 已包含在 `polkitAuth.allowedActions` 中，并且 subject user 是配置里的受信用户、会话是本地 active session。

## 为什么 sudo 正常但 polkit 失败

sudo PAM 和 polkit 走的是不同认证路径：

- sudo 走 `/etc/pam.d/sudo`，由 PAM 调用 helper；
- polkit 走 `polkitd` 的 JavaScript rule，由 `polkit.spawn()` 调用 helper。

两者运行身份不同，所以同一个 `/run/secrets/...` 文件可能在 sudo 场景可读，在 polkit 场景不可读。

因此，polkit 是否能免密通过，不只取决于蓝牙设备是否连接，还取决于 polkit helper 能否读取它需要的运行时 settings 和 secret。
