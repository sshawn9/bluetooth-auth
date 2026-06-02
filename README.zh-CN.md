# bluetooth-auth

[![CI](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml/badge.svg)](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml)
[![Renovate](https://img.shields.io/badge/renovate-enabled-brightgreen.svg)](./renovate.json)
[![English](https://img.shields.io/badge/lang-English-blue)](./README.md)
[![简体中文](https://img.shields.io/badge/lang-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-red)](./README.zh-CN.md)

用于 NixOS 蓝牙认证工作流的小型 BlueZ D-Bus 工具集。

`bluetooth-auth` 会监控一个受信任的蓝牙设备，并把它的连接状态作为本地桌面安全工作流的便利信号。它可以保持设备连接、在设备断开时锁定会话，也可以在设备已连接时允许指定的受信任用户通过 `sudo` 认证。

> 蓝牙接近性是一种便利因素，而不是强认证。它适合用在可信的个人机器上，并且应当始终保留普通密码认证作为后备路径。

## 功能

- 为已配对且受信任的设备维护 BlueZ `org.bluez.Device1` 连接。
- 当设备不再连接时，锁定当前活跃的本地 Wayland 会话。
- 可选添加一条 `sudo` PAM 规则：当受信任的蓝牙设备已连接时，让指定用户通过认证。
- 以 Nix flake 形式提供，包含 NixOS 模块和 Python 包。
- 通过 `dbus-fast` 使用系统 D-Bus 上的 BlueZ。

## 要求

- 带 BlueZ 的 Linux 系统。
- 使用内置模块时需要 NixOS。
- 如果在 Nix 包之外运行命令行工具，需要 Python 3.13。
- 一个已经在 BlueZ 中配对并设为受信任的蓝牙设备。
- 内置锁屏集成需要 Noctalia。

当前设备路径假设蓝牙适配器是 `hci0`。

## 配对并信任设备

启用服务前，先使用 `bluetoothctl` 配对并信任设备：

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

请把 `AA:BB:CC:DD:EE:FF` 替换为你想作为认证信号的设备地址。

## NixOS 安装

添加 flake input：

```nix
{
  inputs.bluetooth-auth.url = "github:sshawn9/bluetooth-auth";
}
```

导入 NixOS 模块，并配置受信任用户和设备地址：

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

设置 `my.security.bluetoothAuth.enable = true` 后，以下功能模块会默认启用：

- `autoConnect.enable`
- `autoLock.enable`
- `sudoAuth.enable`

当启用 `autoLock` 或 `sudoAuth` 时，必须设置 `my.security.bluetoothAuth.user`。如果你只想自动重连设备，可以禁用其他集成：

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

## NixOS 选项

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `my.security.bluetoothAuth.enable` | `false` | 启用蓝牙认证服务。 |
| `my.security.bluetoothAuth.package` | flake package | 提供命令行工具的包。 |
| `my.security.bluetoothAuth.user` | `null` | 被 sudo auth 信任、并作为 auto-lock 目标的用户。 |
| `my.security.bluetoothAuth.bluetoothAddress` | `""` | 要通过 BlueZ 监控的蓝牙设备地址。 |
| `my.security.bluetoothAuth.autoConnect.enable` | `true` | 保持受信任设备连接。 |
| `my.security.bluetoothAuth.autoLock.enable` | `true` | 当设备断开连接时锁定会话。 |
| `my.security.bluetoothAuth.autoLock.checkIntervalSeconds` | `30` | 连接状态和锁屏检查的轮询间隔。CLI 会把低于 30 秒的值视为 30 秒。 |
| `my.security.bluetoothAuth.sudoAuth.enable` | `true` | 当设备已连接时，允许受信任用户通过 `sudo` 认证。 |

## 服务

NixOS 模块可能创建以下 systemd unit：

| Unit | 类型 | 用途 |
| --- | --- | --- |
| `bluetooth-auth-auto-connect.service` | system | 当配置的设备已配对、受信任且未连接时，定期调用 BlueZ `Device1.Connect`。 |
| `bluetooth-auth-auto-lock.service` | system | 检查配置的设备；当设备断开且活跃 Wayland 会话尚未锁定时，启动 `noctalia-lock.service`。 |
| `noctalia-lock.service` | system | 使用正确的运行时和 D-Bus 环境运行配置用户的 Noctalia 用户服务。 |
| `noctalia-lock.service` | user | 调用 `noctalia-shell ipc call lockScreen lock`。 |

查看服务状态：

```console
systemctl status bluetooth-auth-auto-connect.service
systemctl status bluetooth-auth-auto-lock.service
systemctl status noctalia-lock.service
systemctl --user status noctalia-lock.service
```

## 命令行工具

这个包提供三个命令：

```console
bluetooth-auth-auto-connect AA:BB:CC:DD:EE:FF
```

保持一个已配对且受信任的 BlueZ 设备连接。命令会一直运行，直到被中断。

```console
bluetooth-auth-auto-lock alice AA:BB:CC:DD:EE:FF 30
```

轮询设备连接状态；当设备断开且用户的活跃本地 Wayland 会话未锁定时，启动 `noctalia-lock.service`。

```console
bluetooth-auth-sudo-auth alice AA:BB:CC:DD:EE:FF
```

检查当前 PAM 请求是否属于 `alice`，以及设备是否已连接。退出状态 `0` 表示检查成功；退出状态 `1` 表示认证应继续交给下一条 PAM 规则。

## 构建与开发

构建包：

```console
nix build .
```

运行 flake 检查：

```console
nix flake check
```

进入开发 shell：

```console
nix develop
```

在本地环境安装 Python 依赖：

```console
uv sync
```

## 安全模型

`bluetooth-auth` 把“配置的设备已连接到 BlueZ”视为本地信任信号。这个信号很方便，但它弱于密码、硬件安全密钥或加密挑战响应协议。

重要影响：

- 只配对并信任你自己控制的设备。
- 保留普通 `sudo` 密码路径。
- 如果你只需要自动连接和自动锁屏，可以考虑禁用 `sudoAuth`。
- 在共享机器或高风险机器上使用前，请先审阅 PAM 规则。
- 蓝牙设备处于连接状态，并不能证明设备持有人就在旁边或正在注意这台机器。

## 排障

检查 BlueZ 是否认识该设备，以及设备是否受信任：

```console
bluetoothctl info AA:BB:CC:DD:EE:FF
```

查看服务日志：

```console
journalctl -u bluetooth-auth-auto-connect.service -f
journalctl -u bluetooth-auth-auto-lock.service -f
journalctl -u noctalia-lock.service -f
journalctl --user -u noctalia-lock.service -f
```

如果 auto-lock 没有触发，请确认 `loginctl` 能为配置用户报告一个活跃的本地 Wayland 会话：

```console
loginctl list-sessions
loginctl show-session <session-id>
```

如果 Noctalia 没有锁屏，请确认配置用户可以在以下路径使用 `noctalia-shell`：

```text
/etc/profiles/per-user/<user>/bin/noctalia-shell
```

## 许可证

当前未包含 license 文件。
