# bluetooth-auth

[![CI](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml/badge.svg)](https://github.com/sshawn9/bluetooth-auth/actions/workflows/ci.yml)
[![Renovate](https://img.shields.io/badge/renovate-enabled-brightgreen.svg)](https://github.com/sshawn9/bluetooth-auth/issues/2)
[![English](https://img.shields.io/badge/lang-English-blue)](./README.md)
[![简体中文](https://img.shields.io/badge/lang-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-red)](./README.zh-CN.md)

使用 iPhone 系统蓝牙功能，为 NixOS 提供免密认证、按需连接和 Noctalia 自动锁屏。运行程序使用 Rust，系统接入使用 NixOS 模块；早期 Python BLE 原型保留在实验归档中。

项目围绕单个指定用户、单台手机和 `hci0` 适配器开发。蓝牙连接提供便利的认证条件；手机重新连回后恢复免密资格，解锁或提权仍由用户触发。普通密码认证保留为后备路径。

## 工作方式

连接检查读取 Linux 当前连接表，要求目标地址对应唯一一条已连接、已加密的 LE 链路。

- 已连接：直接返回成功。
- 未连接且允许等待：临时注册消费控制类 HID over GATT 服务和广播，等待已配对的 iPhone 连回并完成加密。
- 本次尝试成功、失败或超时后，释放临时 HID 和广播；不主动断开已建立的连接。

各进程复用同一把连接锁。等待其他进程释放锁、注册服务、广播和等待连接共用本次时间预算，不在一次尝试内重试连接。

HID 服务使用电脑原有蓝牙适配器和身份，可以与原有蓝牙能力共存；程序不发送按键报告，也不关闭音频服务。已完成的实机验证及其范围见 [iPhone BLE 实验归档](experiments/iphone_ble/README.md)，不能把短测结果当成所有设备和 iOS 版本的保证。

## 环境与首次配对

需要 Linux、运行中的 BlueZ，以及已开启的 `hci0`。NixOS 模块不会代替系统的蓝牙配置。自动锁屏需要 Noctalia v5；可选 Keyring 解锁需要 GNOME Keyring、SOPS 和用户可用的解密密钥。

**首次配对要在 HID 服务运行期间完成。已有的普通音频配对不能直接假定可用于这条 HID/LE 路径。**

在仓库中构建工具：

```sh
nix build .
```

先在电脑的蓝牙管理工具中允许新配对、启用配对确认，再运行：

```sh
sudo ./result/bin/bluetooth-auth-hid-server
```

在 iPhone 的“设置 → 蓝牙”中选择电脑当前名称，按两端提示完成配对。如果旧配对只能用于音频，需要手动移除目标手机的旧配对后，在 HID 服务运行时重新配对。

配对完成后按 `Ctrl+C`。这个工具没有超时，不会在配对完成后自动退出，也不会替你接受配对。退出释放临时 HID 和广播，保留两端配对记录。

从电脑的蓝牙管理工具取得手机配对后的身份地址，保存到一个只包含该身份地址的单行运行时文件中。后续配置指向这个文件；使用手机的身份地址，不使用电脑适配器地址或临时随机地址。

## NixOS 接入

将下面内容合并到自己的 flake，保留现有系统配置。示例中的 `alice` 和地址文件路径需要替换；启用认证前先完成上面的 HID 配对，并准备好地址文件。

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

            # 根据实际环境启用。
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

总开关会把包中的命令加入系统 `PATH`，各项集成仍默认关闭。仅使用系统级自动连接时可以不设置 `trustedUser`；使用认证、Noctalia 自动锁屏或 Keyring 解锁时应指定用户。

### 设备地址与 SOPS

已导入 sops-nix NixOS 模块的系统可以直接指定已有 secret 的名称，不需要额外导入本项目的 SOPS 模块：

```nix
{
  sops.secrets.bluetooth_address = { };
  my.security.bluetoothAuth.device.address.sopsSecretName = "bluetooth_address";
}
```

`device.address.sopsSecretName` 会优先使用该 secret 的运行时路径，覆盖 `device.address.file`，并设置 `group = cfg.accessGroup`、`mode = "0440"`。默认组名为 `bluetooth-auth-connect`；模块会将指定用户及启用 polkit 时的 `polkituser` 加入该组。

直接提供 `device.address.file` 同样受支持，但文件的属组、权限和创建时机由使用方管理。用户服务需要指定用户可读；polkit 集成还需要 `polkituser` 可读。地址文件应在相应程序启动前可用，文件内容不会写入命令行参数。

## 认证与自动连接

sudo、locker 和 greetd 接入 PAM；polkit 使用自己的授权规则。它们都调用 `bluetooth-auth-link --connect -1`：

| 检查结果 | 本次认证 | 后续动作 |
| --- | --- | --- |
| 目标加密 LE 已连接 | 符合用户及入口条件时通过 | 无需连接 |
| 目标未连接 | 继续正常密码认证 | 通知后台尝试连接一次，为后续认证准备 |
| 检查或通知出错 | 继续正常密码认证 | 输出运行错误 |

认证入口不等待后台连接完成。后台后来连上，也不会把已经返回失败的那次蓝牙检查改成成功。

PAM 规则限制为配置用户；sudo 也处理该用户作为请求方的情况。polkit 另外要求活跃的本地会话，以及 action 在 `auth.polkit.allowedActions` 中。默认列表覆盖部分电源、systemd、NetworkManager、UDisks 和 UPower 操作，完整列表见 [polkit 模块](modules/nixos/polkit-auth.nix)；设置 `[]` 即不通过蓝牙放行任何 action。

`auth.greetd` 可以直接启用。`auth.locker.pamService` 默认 `login`，需要与锁屏器实际使用的 PAM 服务一致；修改共享的 `login` PAM 服务也会影响使用它的其他入口。

启用 `autoConnect` 后，还会在这些时机提前尝试连接：

| 时机 | 执行方式 |
| --- | --- |
| BlueZ 开机启动或重新启动 | systemd 启动已有的单次连接服务 |
| 挂起、休眠、混合睡眠、先挂起再休眠成功恢复 | 睡眠服务的 `OnSuccess` 启动单次连接服务 |
| `hci0` 的 `Powered` 变为 `true` | `bluetooth-auth-power-monitor` 直接调用 Rust 连接函数 |

电源状态监听器空闲时阻塞等待 D-Bus 消息。连接尝试期间串行等待本次结果，随后继续监听；失败或超时不会自行重试。仅启用 `autoConnect` 不会添加周期重连，也不会添加 Noctalia 的空闲恢复钩子。

## Noctalia 自动锁屏

启用 `noctaliaAutoLock` 后，指定用户的 systemd 服务在 `graphical-session.target` 启动后常驻运行。

每轮读取 Noctalia 锁屏状态，并调用 `query_or_connect`。如果会话未锁定且本次仍无法连接手机，就调用 `noctalia msg session lock`，300 毫秒后再次查询锁屏状态。随后按动作后的状态选择休眠时间：

| 动作后状态 | 默认休眠 |
| --- | --- |
| 未锁定、已连接 | 30 秒 |
| 未锁定、未连接（尚未确认锁定） | 30 秒 |
| 已锁定、已连接 | 120 秒 |
| 已锁定、未连接 | 60 秒 |

已锁定时仍会检查并按需连接手机，连接恢复后不会自动解锁。这里使用常驻进程和动态休眠，不使用 systemd timer。

Noctalia 必须在用户环境中可用。图形会话需要在启动 `graphical-session.target` 前，把 `WAYLAND_DISPLAY` 导入用户 systemd 环境；服务同时使用该用户的 `XDG_RUNTIME_DIR`。用户服务由 NixOS 模块定义，通过 `ConditionUser` 限定为配置用户，不需要 Home Manager 模块。

## GNOME Keyring 解锁

蓝牙免密登录不会产生登录密码，原有 `pam_gnome_keyring` 因而可能无法解锁 login 钥匙串。可选的 `gnomeKeyringUnlock` 在图形会话启动后运行一次，保留原有密码登录的解锁流程。

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

SOPS 文件中的指定顶层字符串保存**现有 login 钥匙串的密码**。密码哈希不能代替解密密码。此配置建立在前面的用户和蓝牙地址配置之上；用户必须能读取加密文件及相应解密密钥。

程序先查询 login 钥匙串：已解锁就直接结束；仍锁定时，查询或尝试建立指定手机的加密 LE 连接，满足条件后才通过 SOPS 解密密码并交给 GNOME Keyring。蓝牙未连接时正常跳过，运行错误则报错退出；没有自动重试。

这里调用 SOPS 工具本身，解密输出留在进程内存并通过 D-Bus 传递，不生成一份明文密码运行时文件，也不把密码放入参数或环境变量。解锁使用 GNOME Keyring 专有接口，需要已经存在的 login 钥匙串。

## NixOS 选项

路径均相对于 `my.security.bluetoothAuth`。

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable` | `false` | 安装工具并启用模块配置；具体集成单独开启。 |
| `package` | flake 包 | 提供五个 Rust 程序的包。 |
| `trustedUser` | `""` | 允许免密认证及运行用户服务的用户。 |
| `accessGroup` | `"bluetooth-auth-connect"` | 连接 socket、锁文件和可选地址文件的访问组。 |
| `device.address.file` | `""` | 包含手机身份地址的运行时文件。 |
| `device.address.sopsSecretName` | `null` | 引用的 sops-nix secret 名称；设置后覆盖地址路径并配置组读取权限。 |
| `connection.timeoutMs` | `7000` | 后台连接、状态监听器及用户服务每次连接尝试的时间预算。 |
| `autoConnect.enable` | `false` | 开机、BlueZ 重启、睡眠恢复及蓝牙开启时提前连接。 |
| `auth.sudo.enable` | `false` | sudo PAM 集成。 |
| `auth.polkit.enable` | `false` | polkit 授权集成。 |
| `auth.polkit.allowedActions` | 模块中的桌面 action 列表 | 允许通过蓝牙放行的 polkit action。 |
| `auth.locker.enable` | `false` | 锁屏器 PAM 集成。 |
| `auth.locker.pamService` | `"login"` | 锁屏器使用的 PAM 服务。 |
| `auth.greetd.enable` | `false` | greetd PAM 集成。 |
| `auth.greetd.pamService` | `"greetd"` | greetd 使用的 PAM 服务。 |
| `noctaliaAutoLock.enable` | `false` | 启用 Noctalia 自动锁屏用户服务。 |
| `noctaliaAutoLock.sleepIntervalsMs.unlockedConnected` | `30000` | 未锁定、已连接后的休眠。 |
| `noctaliaAutoLock.sleepIntervalsMs.unlockedDisconnected` | `30000` | 未锁定、未连接后的休眠。 |
| `noctaliaAutoLock.sleepIntervalsMs.lockedConnected` | `120000` | 已锁定、已连接后的休眠。 |
| `noctaliaAutoLock.sleepIntervalsMs.lockedDisconnected` | `60000` | 已锁定、未连接后的休眠。 |
| `gnomeKeyringUnlock.enable` | `false` | 启用 GNOME login 钥匙串自动解锁。 |
| `gnomeKeyringUnlock.password.sopsFile` | 启用时必填 | 含现有钥匙串密码的 SOPS 加密文件。 |
| `gnomeKeyringUnlock.password.sopsField` | `"login_keyring_password"` | SOPS 文件中的顶层字符串字段名。 |
| `gnomeKeyringUnlock.password.ageKeyFile` | `null` | 用户 age 密钥路径；不设置时使用 SOPS 自身的密钥查找机制。 |

## 命令行工具

启用模块后，以下五个命令均可直接使用。构建目录中的对应程序位于 `./result/bin/`，Cargo 输出位于 `./target/release/`。

| 命令 | 用途 |
| --- | --- |
| `bluetooth-auth-link` | 查询目标加密 LE 连接，可选择等待连接或通知后台。 |
| `bluetooth-auth-hid-server` | 无参数手动配对辅助程序；提供 HID 和广播直到 `Ctrl+C`。 |
| `bluetooth-auth-noctalia-auto-lock` | 在 Noctalia 用户会话中持续检查、按需连接和锁屏。 |
| `bluetooth-auth-keyring-unlock` | 按需通过 SOPS 解锁 GNOME login 钥匙串。 |
| `bluetooth-auth-power-monitor` | 监听 `hci0` 蓝牙开启事件并直接尝试连接。 |

除无参数的 HID 配对工具外，其余命令均提供 `--help`。

### 查询与连接

```sh
bluetooth-auth-link --address-file /run/secrets/bluetooth_address --connect 0
bluetooth-auth-link --address-file /run/secrets/bluetooth_address --connect 7000
bluetooth-auth-link --address-file /run/secrets/bluetooth_address --connect -1
```

| `--connect` | 已连接 | 未连接 |
| --- | --- | --- |
| `0` | 退出 `0` | 退出 `1`，只查询 |
| 正数，例如 `7000` | 退出 `0` | 尝试一次并等待，正数为毫秒预算 |
| 负数，规范写法为 `-1` | 退出 `0` | 通知后台连接，本次仍退出 `1` |

不指定 `--connect` 时默认 `15000`。负数的绝对值不控制超时，后台使用 Nix 的 `connection.timeoutMs`。无连接和普通超时保持安静，运行错误写入 stderr；参数格式错误退出 `2`。

同步连接使用 `/run/bluetooth-auth/hci0.lock`。模块在启用自动连接或任一认证/用户服务集成时创建它；不要删除或替换正在使用的锁文件。异步模式另外要求 `/run/bluetooth-auth/connect.sock`，该 socket 随 sudo、polkit、locker、greetd、Noctalia 自动锁屏或 Keyring 集成启用，单独启用 `autoConnect` 不创建它。

不使用 NixOS 模块、只手动测试构建产物时，可以先准备锁文件，再以 root 运行一次连接：

```sh
sudo mkdir -p /run/bluetooth-auth
sudo touch /run/bluetooth-auth/hci0.lock
sudo ./result/bin/bluetooth-auth-link --address-file /path/to/bluetooth-address --connect 7000
```

### 用户服务与状态监听器

在当前 Noctalia 用户会话中手动运行自动锁屏时，先停止已启用的`bluetooth-auth-auto-lock.service` 用户服务，避免重复运行：

```sh
bluetooth-auth-noctalia-auto-lock --address-file /run/secrets/bluetooth_address --timeout-ms 7000
```

`--help` 列出四种状态各自的 `--*-interval-ms` 参数。手动运行也需要地址及共享锁的访问权限。

手动运行电源状态监听器前，先停止已启用的`bluetooth-auth-power-monitor.service` 系统服务，避免重复运行：

```sh
sudo bluetooth-auth-power-monitor --address-file /run/secrets/bluetooth_address --timeout-ms 7000
```

该命令需要系统 D-Bus 允许它取得 `org.bluetooth_auth.PowerMonitor` 名称；`autoConnect` 模块提供对应配置。它只响应后续的 `Powered=true` 信号，不在启动时额外执行一次连接。

### Keyring 命令

在用户会话中运行，`sops` 需要在 `PATH` 中：

```sh
bluetooth-auth-keyring-unlock --sops-file /path/to/keyring.enc.yaml --sops-key login_keyring_password
```

默认不检查蓝牙；添加 `--address-file /run/secrets/bluetooth_address --timeout-ms 7000` 后，才要求目标连接。NixOS Keyring 集成会传入地址文件。

退出 `0` 表示正常结束，也包括“蓝牙条件未满足，跳过解锁”；运行错误退出 `1`，参数格式错误退出 `2`。

## systemd 服务与排障

| Unit | 作用域 | 用途 |
| --- | --- | --- |
| `bluetooth-auth-connect.service` | 系统 | 带时间预算的单次连接任务。 |
| `bluetooth-auth-connect.socket` | 系统 | 接收异步连接通知并激活单次任务。 |
| `bluetooth-auth-power-monitor.service` | 系统 | 蓝牙开启事件监听器。 |
| `bluetooth-auth-auto-lock.service` | 用户 | Noctalia 自动锁屏循环。 |
| `bluetooth-auth-keyring-unlock.service` | 用户 | 图形会话启动后的单次 Keyring 解锁。 |

按已启用的集成查看日志：

```sh
journalctl -b -u bluetooth-auth-connect.service -u bluetooth-auth-power-monitor.service
journalctl --user -b -u bluetooth-auth-auto-lock.service -u bluetooth-auth-keyring-unlock.service
```

连接服务正常未连接时也会以非零状态结束，systemd 可能显示 `failed`；这不等于程序崩溃，需结合 stderr 判断。检查连接问题时先确认 HID/LE 配对、地址文件、`hci0` 电源和共享锁是否就绪。

Noctalia 问题可以在对应用户会话中执行 `noctalia msg status`，并检查 `systemctl --user show-environment` 中的 `WAYLAND_DISPLAY`、`XDG_RUNTIME_DIR`。Keyring 问题需要确认 login 钥匙串存在，用户 SOPS 密钥可以解密指定文件，且保存的是当前钥匙串密码。

## 构建、测试与实验

```sh
nix build .
nix flake check
```

开发环境与 Rust 测试：

```sh
nix develop
cargo build --release --locked
cargo test --locked
```

脱离 Nix 构建需要 Rust/Cargo、C 编译器、`pkg-config` 和 D-Bus 开发文件；测试还需要 `dbus-daemon`。运行时工具不需要 Python。

Rust 测试使用私有 D-Bus 和模拟 HCI，覆盖连接判据、HID 身份检查、锁竞争、超时清理、蓝牙开启事件和 Noctalia 流程。flake 另外包含认证、连接调度和 Keyring 的隔离集成检查。

[iPhone BLE 实验目录](experiments/iphone_ble/README.md) 保留 ANCS、CTS、HID 的成功与失败记录、Python 原型及复现步骤；[实验报告](experiments/iphone_ble/RESULTS.md) 说明实机验证范围。实验归档中的命令与当前 Rust 程序分别维护。

## 许可证

MIT。见 [LICENSE](LICENSE)。
