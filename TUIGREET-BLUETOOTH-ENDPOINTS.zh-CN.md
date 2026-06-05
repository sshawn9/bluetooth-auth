# tuigreet 阶段 BlueZ A2DP Endpoint 未注册问题

本文记录一次启动阶段蓝牙自动连接失败的排查过程。问题表现为：

- `bluetooth-auth-auto-connect.service` 在开机后立即启动；
- tuigreet 登录界面已经出现，但受信蓝牙设备还没有从电脑侧自动连接；
- `bluetooth-auth-oneshot-auth ... greetd` 因设备未连接而 fallback；
- 最后需要手动输入密码，或者从蓝牙设备侧主动连接电脑后再回车通过认证。

当前模块状态：发布的 NixOS 模块会强制关闭 `greetdAuth.enable`。下面的排查记录说明为什么曾经加入 early media endpoints，以及如果在本地变体中启用 greetd 认证，需要满足什么条件；但在当前发布模块里，单独设置 `greetdAuth.earlyMediaEndpoints.enable` 不会生效。

## 现象

启动日志里可以看到 auto-connect 很早就尝试连接设备：

```text
bluetooth-auth-auto-connect: device is disconnected; requesting BlueZ connection
bluetoothd: src/service.c:btd_service_connect() a2dp-source profile connect failed for AA:BB:CC:DD:EE:FF: Protocol not available
bluetooth-auth-auto-connect: BlueZ or D-Bus error; retrying ...: DBusError: br-connection-unknown
```

关键错误是：

```text
a2dp-source profile connect failed ... Protocol not available
```

这不是蓝牙地址错误，也不是设备一定不在附近，而是 BlueZ 在发起 profile 连接时，系统侧还没有准备好对应的 A2DP media endpoint。

## 时序排查

这个问题不能只看某一个服务是否启动，而是要确认几个事件的相对顺序：

- BlueZ 什么时候启动；
- auto-connect 什么时候第一次调用 BlueZ `Device1.Connect()`；
- 这次连接失败时 BlueZ 报了什么；
- tuigreet/greetd 什么时候进入 PAM 认证；
- 目标用户的 user manager 是否已经在登录前启动；
- PipeWire/WirePlumber 是只启动了 socket，还是 service 本体已经运行；
- WirePlumber 什么时候向 BlueZ 注册 A2DP endpoint。

### 1. 先拉一条粗略启动线

先用一条命令把相关日志按时间过滤出来：

```sh
journalctl -b --no-pager -o short-precise \
  | rg 'bluetooth-auth-auto-connect|bluetooth-auth-greetd-auth|bluetoothd|greetd|user@[0-9]+\.service|linger-users|pipewire|wireplumber|Endpoint registered|a2dp-source|Protocol not available'
```

这条命令的作用不是给出最终结论，而是快速判断“失败发生在登录前还是登录后”。如果看到类似顺序：

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

就说明第一次 host-side auto-connect 和 greetd 蓝牙认证都发生在 A2DP endpoint 注册之前。

### 2. 确认 auto-connect 失败点

查看 auto-connect 自己的日志：

```sh
journalctl -b -u bluetooth-auth-auto-connect.service --no-pager -o short-precise
```

重点看三类信息：

- `starting ...`：auto-connect service 的启动时间；
- `device is disconnected; requesting BlueZ connection`：调用 BlueZ `Device1.Connect()` 的时间；
- `BlueZ or D-Bus error; retrying ...`：这次调用失败后进入重试的时间。

然后对照 BlueZ 日志：

```sh
journalctl -b -u bluetooth.service --no-pager -o short-precise \
  | rg 'AA:BB|connect|failed|a2dp|br-|Protocol not available|Endpoint registered'
```

当时的关键日志是：

```text
bluetoothd: src/service.c:btd_service_connect() a2dp-source profile connect failed for AA:BB:CC:DD:EE:FF: Protocol not available
```

这说明 BlueZ 已经收到了连接请求，但连接 A2DP profile 时系统侧协议端点不可用。

### 3. 确认 greetd 认证点

greetd 本身的 unit 日志可以看登录服务什么时候开始工作：

```sh
journalctl -b -u greetd.service --no-pager -o short-precise
```

蓝牙 PAM helper 的日志走 syslog ident，可以单独查：

```sh
journalctl -b -t bluetooth-auth-greetd-auth --no-pager -o short-precise
```

重点看：

```text
bluetooth-auth-greetd-auth: starting mode=greetd ...
bluetooth-auth-greetd-auth: trusted device is not connected; falling back to the next auth rule
```

如果这两行出现在 `Endpoint registered` 之前，那么 tuigreet 阶段认证失败的原因就不是 PAM rule 顺序，而是认证发生时 BlueZ 设备还没有连上。

### 4. 确认 user manager 和 lingering

如果要让用户态 PipeWire/WirePlumber 在登录前运行，目标用户的 systemd user manager 必须提前存在。先查目标用户 UID：

```sh
id -u alice
```

假设输出是 `1000`，再查：

```sh
journalctl -b -u user@1000.service -u linger-users.service --no-pager -o short-precise
```

这里要确认的是：

- `linger-users.service` 是否启动；
- `user@1000.service` 是否在登录前已经启动。

但这一步只能证明 user manager 存在。它不能证明 PipeWire/WirePlumber 已经真正运行。

### 5. 区分 PipeWire socket 和 service

当时最容易误判的是：日志里很早出现了 PipeWire socket，但 service 本体并没有启动。

查看用户服务日志：

```sh
journalctl -b --user \
  -u pipewire.socket \
  -u pipewire-pulse.socket \
  -u pipewire.service \
  -u wireplumber.service \
  --no-pager -o short-precise
```

需要区分这两类日志：

```text
Listening on PipeWire Multimedia System Sockets.
Listening on PipeWire PulseAudio.
```

这只说明 socket 已经监听。它不等于 PipeWire/WirePlumber 已经运行。

真正有意义的是：

```text
Started PipeWire Multimedia Service.
Started Multimedia Service Session Manager.
```

如果这两行在用户输入密码进入桌面之后才出现，那么登录前并没有真正启动媒体服务。

### 6. 确认 A2DP endpoint 注册点

最后确认 BlueZ 什么时候收到 WirePlumber 注册的 media endpoint：

```sh
journalctl -b -u bluetooth.service --no-pager -o short-precise \
  | rg 'Endpoint registered|MediaEndpoint|A2DP'
```

典型日志类似：

```text
bluetoothd: Endpoint registered: sender=:... path=/MediaEndpoint/A2DPSource/...
```

判断方式很直接：

- 如果 `a2dp-source profile connect failed ... Protocol not available` 在前；
- `bluetooth-auth-greetd-auth: trusted device is not connected` 在中间；
- `Endpoint registered` 在登录进入桌面后才出现；

那么根因就是：tuigreet 阶段 A2DP endpoint 尚未注册，电脑侧自动连接音频设备失败。

### 7. 最终时序结论

这次排查得到的实际顺序是：

1. `bluetooth.service` 启动；
2. `bluetooth-auth-auto-connect.service` 启动；
3. auto-connect 调用 BlueZ `Device1.Connect()`；
4. BlueZ 尝试 A2DP profile，失败为 `Protocol not available`；
5. tuigreet/greetd 进入 PAM 认证；
6. `bluetooth-auth-greetd-auth` 检查到设备未连接，于是 fallback；
7. 用户输入密码进入桌面；
8. PipeWire/WirePlumber service 本体启动；
9. WirePlumber 向 BlueZ 注册 A2DP endpoint；
10. 后续由设备侧主动连接或 auto-connect 重试后，蓝牙才真正变成 connected。

## 根因

BlueZ 的 `Device1.Connect()` 并不只是建立一个抽象的蓝牙连接。对于音频类设备，它会尝试连接相关 profile，例如 A2DP。A2DP profile 需要 PipeWire/WirePlumber 在运行时向 BlueZ 注册 media endpoint。

这里有几个容易混淆的点：

- `pipewire.socket` 启动不等于 `pipewire.service` 已经运行；
- `pipewire.service` 运行不等于 WirePlumber 已经向 BlueZ 注册 A2DP endpoint；
- A2DP endpoint 注册是 WirePlumber 对 BlueZ 的运行时 D-Bus 注册，不是一个稳定的 systemd readiness 状态；
- user manager 提前启动也不保证 PipeWire/WirePlumber 服务体会提前启动，除非这些 user service 被挂到 `default.target`。

因此，单纯让 `bluetooth-auth-auto-connect.service` 在 `bluetooth.service` 之后启动是不够的。BlueZ 已经启动，但负责注册 A2DP endpoint 的用户态媒体服务还没有启动。

## 本地启用 greetd Auth 时的修复方式

模块中保留了这个选项：

```nix
my.security.bluetoothAuth.greetdAuth.earlyMediaEndpoints.enable = true;
```

在当前发布的模块里，该选项受 `greetdAuth.enable` 控制，而根 bluetooth-auth 模块会把 `greetdAuth.enable` 强制为 `false`。因此，只有在本地修改这个强制关闭逻辑后，该选项才会实际应用。

生效时，它会做两件事：

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

含义是：

- 为受信用户启用 lingering，让该用户的 systemd user manager 可以在登录前运行；
- 把 `pipewire.service` 和 `wireplumber.service` 挂到用户 `default.target`；
- 这样在登录前 user manager 被拉起时，PipeWire/WirePlumber 会实际启动，而不是只启动 socket；
- WirePlumber 便有机会在 tuigreet 认证前向 BlueZ 注册 A2DP endpoint。

如果本地启用，正确配置位置是 `greetdAuth` 子模块，不是 JSON `settings`：

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

  # 需要本地允许 greetdAuth.enable；当前发布模块会把它强制为 false。
  greetdAuth = {
    enable = true;
    earlyMediaEndpoints.enable = true;
  };
};
```

## 本地启用后的验证

重建并重启后，检查 user service 是否挂到 `default.target`：

```sh
ls -l /etc/systemd/user/default.target.wants
systemctl --user show pipewire.service wireplumber.service -p WantedBy -p UnitFileState
```

检查 PipeWire/WirePlumber 是否在登录前启动：

```sh
journalctl -b --user -u pipewire.service -u wireplumber.service --no-pager -o short-precise
```

检查 BlueZ endpoint 是否在 greetd 认证前注册：

```sh
journalctl -b -u bluetooth.service --no-pager -o short-precise | rg 'Endpoint registered'
journalctl -b -u bluetooth-auth-auto-connect.service -u greetd.service --no-pager -o short-precise
```

修复后的期望顺序是：

1. 目标用户 user manager 启动；
2. `pipewire.service` / `wireplumber.service` 启动；
3. BlueZ 出现 `Endpoint registered`；
4. auto-connect 后续重试时可以成功连接；
5. 如果本地启用了 greetd 蓝牙认证，tuigreet 阶段回车即可由蓝牙认证通过。

## 限制

本地启用后，这个修复会让受信用户的一部分 user services 在登录前运行，会扩大登录前的用户态服务面。它适合当前项目的使用场景，但不应该无脑视为通用安全默认值。

另外，这个配置并不提供严格的“endpoint 已注册”ready 信号。BlueZ endpoint 注册仍然是运行时事件，所以 auto-connect 仍然需要重试逻辑。该修复的目标是让注册尽可能早发生，至少在 tuigreet 阶段具备可用条件。

## 无密码登录与 Login Keyring

本地启用后，如果 tuigreet 阶段蓝牙认证通过，本质上是 PAM 认证链路被 `bluetooth-auth-greetd-auth` 以 `sufficient` 方式提前放行。用户没有输入登录密码，因此后续依赖登录密码的组件拿不到密码材料。

最典型的问题是 GNOME Keyring 的 login keyring：

```text
The login keyring did not get unlocked when you logged into your computer.
```

这不是 VS Code 的问题，也不是 polkit 的问题。login keyring 通常由 `pam_gnome_keyring` 在登录时使用用户输入的登录密码自动解锁。蓝牙免密登录时，PAM 流程没有得到明文登录密码，所以 `pam_gnome_keyring` 无法解密 login keyring。进入桌面后，使用 Secret Service 的应用就可能弹出 keyring 解锁窗口。

这说明“无密码登录”的代价比 sudo/polkit 免密更大：

- sudo/polkit 免密只影响某一次授权请求；
- tuigreet 免密会改变整个桌面会话的登录语义；
- 依赖登录密码派生密钥的组件不会自动工作；
- login keyring、浏览器密钥存储、libsecret 应用都可能受到影响。

可以选择的处理方式各有代价：

- 保留 login keyring 密码：进入桌面后手动输入一次 keyring 密码，静态安全性最好；
- 把 login keyring 改为空密码：不会弹窗，但 keyring 文件失去自身密码加密，只依赖文件权限和磁盘加密；
- 给 login keyring 设置独立密码，并用 sops 保存后由用户会话 helper 自动解锁：比空密码更有边界，但只要机器能自动解密 sops，它也能自动解锁 keyring；
- 把真实登录密码放进 sops：不推荐，登录密码用途太广，泄漏面不应该扩大。

因此，在本地启用的场景下，tuigreet 蓝牙免密登录虽然可以工作，但它不是“只少输一次密码”这么简单。它会让原本依赖登录密码的桌面 secret 体系失去自动解锁条件，需要明确接受这个代价，或者额外设计 keyring 解锁策略。
