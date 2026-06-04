# bluetooth-auth

[![CI](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml/badge.svg)](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml)
[![Renovate](https://img.shields.io/badge/renovate-enabled-brightgreen.svg)](https://github.com/sshawn9/bluetooth-auth/issues/2)
[![English](https://img.shields.io/badge/lang-English-blue)](./README.md)
[![简体中文](https://img.shields.io/badge/lang-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-red)](./README.zh-CN.md)

用于 NixOS 蓝牙认证工作流的小型 BlueZ D-Bus 工具集。

`bluetooth-auth` 会监控一个受信任的蓝牙设备，并把它的连接状态作为本地桌面安全工作流的便利信号。它可以保持设备连接、在设备断开时锁定会话，也可以在设备已连接时允许指定的受信任用户通过 `sudo` 认证。

> 蓝牙接近性是一种便利因素，而不是强认证。它适合用在可信的个人机器上，并且应当始终保留普通密码认证作为后备路径。

## 项目适用范围

这个项目是围绕作者本人的 NixOS 桌面环境开发的，并没有刻意做成通用认证框架。仓库里的 NixOS 模块是一套已经跑通的实现样例，但如果你要在自己的系统上使用，请先充分阅读源码，并根据自己的 PAM、polkit、locker、greetd 和会话环境做适配。

这套基于蓝牙连接状态的方案已经由作者本人测试可用。与此同时，蓝牙信任确实有风险：设备处于连接状态只是一种便利信号，不是强身份认证。如果蓝牙不适合你的环境，也可以把这个项目作为思路参考，替换成其他受信设备或本地信任信号方案。

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

## 设备状态

`bluetooth-auth` 不负责管理配对。配置中的设备只需要已经在 BlueZ 中配对并设为受信任即可。你可以用自己系统里惯用的蓝牙管理方式完成这一步，然后把设备地址写入 `config.bluetoothAddressFile` 指向的运行时文件。

## NixOS 安装

添加 flake input：

```nix
{
  inputs.bluetooth-auth.url = "github:sshawn9/bluetooth-auth";
}
```

导入 NixOS 模块，配置受信任用户和设备地址，并显式启用你需要的集成：

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

          # 所有集成都默认关闭。只启用和你系统匹配的部分。
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

如果你用 `sops-nix` 这类运行时 secret 管理器保存设备地址，请改用
`config.bluetoothAddressFile` 指向生成的 secret 路径：

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

`my.security.bluetoothAuth.enable = true` 只启用模块命名空间。每个集成都有自己的 `enable` 选项，并且全部默认关闭。

当启用 `autoLock`、`sudoAuth`、`polkitAuth`、`lockerAuth` 或 `greetdAuth` 时，必须设置 `my.security.bluetoothAuth.config.user`。如果你只想自动重连设备，只启用 auto-connect 即可：

```nix
{
  my.security.bluetoothAuth = {
    enable = true;
    config.bluetoothAddressFile = "/run/secrets/auth_bluetooth_address";
    autoConnect.enable = true;
  };
}
```

## NixOS 选项

下面的选项路径都相对于 `my.security.bluetoothAuth`。`config.*` 下的字段会写入生成的 JSON 配置，并传给 Python 命令。

**核心**

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable` | `false` | 启用模块命名空间。具体集成仍然需要单独显式启用。 |
| `package` | flake package | 提供命令行工具的包。 |
| `config` | `{}` | 会转换成 JSON 配置并传给命令的 attrset。 |
| `config.user` | `""` | 被 sudo/polkit/PAM 认证信任，并作为 auto-lock 目标的用户。 |
| `config.bluetoothAddressFile` | `""` | 包含蓝牙设备地址的运行时文件。 |

**Auto Connect**

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `autoConnect.enable` | `false` | 保持受信任设备连接。系统需要已经提供 `bluetooth.service`。 |
| `config.autoConnect.checkIntervalSeconds` | `30` | 设备已连接时的轮询间隔。CLI 会把低于 5 秒的值视为 5 秒。 |
| `config.autoConnect.deviceUnvailableGraceSeconds` | `300` | 适配器关闭或设备未配对/未信任时的等待时间。CLI 会把低于 10 秒的值视为 10 秒。 |
| `config.autoConnect.exceptionGraceSeconds` | `300` | BlueZ 或 D-Bus 异常后的重试等待时间。CLI 会把低于 10 秒的值视为 10 秒。 |

**Auto Lock**

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `autoLock.enable` | `false` | 当设备断开连接时锁定会话。 |
| `config.autoLock.checkIntervalSeconds` | `40` | 连接状态和锁屏状态检查的轮询间隔。CLI 会把低于 5 秒的值视为 5 秒。 |
| `config.autoLock.sleepAfterLockSeconds` | `3` | 成功启动锁屏服务后的等待时间。CLI 会把低于 0 秒的值视为 0 秒。 |
| `config.autoLock.exceptionGraceSeconds` | `300` | BlueZ 或 D-Bus 异常后的重试等待时间。CLI 会把低于 10 秒的值视为 10 秒。 |

**sudo PAM**

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `sudoAuth.enable` | `false` | 当设备已连接时，允许受信任用户通过 `sudo` 认证。 |
| `config.sudoAuth.timeoutSeconds` | `2` | sudo PAM 蓝牙检查超时时间。CLI 会把高于 5 秒的值视为 5 秒。 |

**polkit**

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `polkitAuth.enable` | `false` | 添加调用蓝牙认证 helper 的 polkit rule。 |
| `config.polkitAuth.timeoutSeconds` | `2` | polkit 蓝牙检查超时时间。CLI 会把高于 5 秒的值视为 5 秒。 |
| `config.polkitAuth.allowedActions` | 常用桌面 actions | 允许由蓝牙认证放行的 polkit action id。 |

默认 polkit actions 包含 login1 电源/会话操作、systemd unit 管理、NetworkManager、UDisks 挂载/解锁/弹出，以及电源配置切换。设为 `config.polkitAuth.allowedActions = []` 表示不放行任何 polkit action。

**Locker PAM**

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `lockerAuth.enable` | `false` | 给配置的 locker PAM service 添加 PAM 规则。 |
| `lockerAuth.pamService` | `"login"` | locker 使用的 PAM service 名。 |
| `config.lockerAuth.timeoutSeconds` | `2` | locker PAM 蓝牙检查超时时间。CLI 会把高于 5 秒的值视为 5 秒。 |

默认 locker PAM service 匹配 `NOCTALIA_PAM_SERVICE` 未设置时的 Noctalia v4.7.7。启用前请审阅，因为 `/etc/pam.d/login` 的影响范围比 Noctalia 本身更宽。

**greetd PAM**

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `greetdAuth.enable` | `false` | 给配置的 greetd PAM service 添加 PAM 规则。 |
| `greetdAuth.pamService` | `"greetd"` | greetd 使用的 PAM service 名。 |
| `config.greetdAuth.timeoutSeconds` | `2` | greetd PAM 蓝牙检查超时时间。CLI 会把高于 5 秒的值视为 5 秒。 |

## 服务

NixOS 模块可能创建以下 systemd unit：

| Unit | 类型 | 用途 |
| --- | --- | --- |
| `bluetooth-auth-auto-connect.service` | system | 在 `multi-user.target` 启动，要求并排序在 `bluetooth.service` 之后；如果存在 `greetd.service`/`display-manager.service` 启动作业，则排序在它们之前。随后在配置设备已配对、受信任且未连接时，定期调用 BlueZ `Device1.Connect`。 |
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

这个包提供以下命令：

每个命令都接受一个可选的 JSON 配置文件路径。没有传路径时，会读取包内置的
`config.json`。

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

保持配置中已配对且受信任的 BlueZ 设备连接。命令会一直运行，直到被中断。`autoConnect.checkIntervalSeconds` 控制设备已连接时的普通轮询间隔，`deviceUnvailableGraceSeconds` 控制适配器或设备不可用时的等待时间，`exceptionGraceSeconds` 控制 BlueZ 或 D-Bus 异常后的重试等待时间。

```console
bluetooth-auth-auto-lock /path/to/config.json
```

轮询配置中的设备连接状态；当设备断开且配置用户的活跃本地 Wayland 会话未锁定时，启动 `noctalia-lock.service`。`autoLock.checkIntervalSeconds` 控制普通轮询间隔，`sleepAfterLockSeconds` 控制启动锁屏服务后的等待时间，`exceptionGraceSeconds` 控制 BlueZ 或 D-Bus 异常后的重试等待时间。

```console
bluetooth-auth-oneshot-auth /path/to/config.json sudo
```

检查当前 PAM 请求是否属于配置用户，以及配置中的设备是否已连接。退出状态 `0` 表示检查成功；退出状态 `1` 表示认证应继续交给下一条 PAM 规则。`sudoAuth.timeoutSeconds` 控制蓝牙检查超时时间。

```console
bluetooth-auth-oneshot-auth /path/to/config.json polkit
```

检查配置设备是否已连接。NixOS polkit rule 会先检查 subject user、活跃本地会话状态，以及 `polkitAuth.allowedActions`。

```console
bluetooth-auth-oneshot-auth /path/to/config.json locker
bluetooth-auth-oneshot-auth /path/to/config.json greetd
```

locker 和 greetd 集成使用的 PAM helper。它们会检查 `PAM_USER` 是否等于配置用户，然后检查配置设备是否已连接。

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

- 只使用你自己控制、且已经配对并设为受信任的设备。
- 保留普通 `sudo` 密码路径。
- 如果你只需要自动连接和自动锁屏，可以考虑禁用 `sudoAuth`。
- 在共享机器或高风险机器上使用前，请先审阅 PAM 规则。
- 对 secret 管理的设备地址，优先使用 `config.bluetoothAddressFile`，让地址在运行时读取，而不是嵌入生成出的 Nix 配置产物。
- 蓝牙设备处于连接状态，并不能证明设备持有人就在旁边或正在注意这台机器。

使用 `config.bluetoothAddressFile` 时，请确认生成的 secret 文件能被所有已启用集成读取。systemd 服务通常以 root 运行。`sudoAuth` 的 PAM helper 通过 `pam_exec.so seteuid` 调用，所以根据你的 PAM 设置，它的有效用户也可能需要 secret 文件的读取权限。

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

MIT。见 [LICENSE](./LICENSE)。
