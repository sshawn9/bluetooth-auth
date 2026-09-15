# LE 恢复专题复核手册

先读[背景与结论](../../results/LE_BEARER_RECOVERY.zh-CN.md)，再选单个场景复核。本手册提供归档后参数化的工具，**没有用这些新工具重跑历史实机**。历史原脚本中途修改过，早期部分已经删除；修正后的工具不冒充其逐字快照。

## 工具与输入

| 工具 | 用途 | 会改变什么 |
| --- | --- | --- |
| `diagnostics/le_bearer_probe.py` | 目标连接/资源快照；HCI 元数据、D-Bus 方法与属性监听 | 不主动连接、断开、扫描、配对或注册服务 |
| `diagnostics/le_bearer_control.py` | 明确选择通用连接、LE 连接、偏好、Trusted、按类型断开；可选移除经典配对 | 只有指定子命令的动作；没有自动配对、重启或回退 |
| `diagnostics/hid-holder/` | 保持与生产相同的目标限制 HID/广播，Ctrl+C 退出 | 临时注册 HID/广播；不修改适配器配对设置，不发送输入报告 |
| `bluetooth-auth-link` | 本项目实际按需连接程序 | `--connect 0` 纯查询；正数为毫秒预算内的 HID 连接尝试 |
| `check_public_privacy.py` | 检查待归档工作树文本 | 只读文件/Git；不是实机检查 |

从项目根目录运行。准备 Python 3.10+、系统 `busctl`、正在运行的 BlueZ，以及本项目 Rust 程序/连接锁文件。Python 观察和控制工具只用标准库；打开 HCI monitor/MGMT 或读配对元数据通常需要 root。首次编译 Rust 工具需要 Cargo 依赖和系统 D-Bus 编译依赖，项目 Nix 开发环境已提供后者。

真实地址放在个人运行时文件中，使用已配置的 `BLUETOOTH_AUTH_ADDRESS_FILE` 指向它。地址必须是登记的目标身份地址；不要使用偶然看到的临时 RPA，也不要写入源码/文档。所有示例都只传文件路径。

```sh
test -n "$BLUETOOTH_AUTH_ADDRESS_FILE"
export BLE_REVIEW_DIR=$(mktemp -d)
python3 -B experiments/iphone_ble/diagnostics/le_bearer_probe.py --help
python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py --help
```

`BLE_REVIEW_DIR` 用于本轮本地材料，不是项目归档位置。不要公开原始 btmon 数据包、BlueZ info 文件、密钥备份或未经筛选的系统日志。工具错误只输出经过筛选的信息，完整私有 BlueZ journal 可在本机辅助排查。

## 先记录条件，再做动作

```sh
systemctl is-active bluetooth.service
systemctl --user is-active bluetooth-auth-auto-lock.service
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_probe.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" --adapter hci0 snapshot
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" --adapter hci0 bond-info
```

`bond-info` 只输出经典密钥/LE 密钥/IRK 是否存在、允许的偏好值和 Trusted，不输出密钥。复核经典与 LE 共存场景需要原本就有双模配对；**本轮修复后的目标已删除经典密钥，不能在它上面无条件重放 R07/R21。** 不为了复核而自动恢复已经删除的配对；需要另行批准的测试配对。

记录本机 BlueZ/内核/Noctalia/程序版本，手机是否锁屏、是否有其他蓝牙连接、原 PreferredBearer/Trusted、原服务启停状态、实验模式是否启用。关闭自动锁屏前记下原状态；本次历史案例用户明确要求保持停止，不能在清理时擅自重新启用。

```sh
systemctl --user stop bluetooth-auth-auto-lock.service
```

观察器在一个终端启动，另一个终端执行控制命令：

```sh
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_probe.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" --adapter hci0 \
  monitor --seconds 120 --output "$BLE_REVIEW_DIR/trace.jsonl"
```

观察输出关注：target links 的 transport/connected/encrypted、连接完成时电脑的角色、主机连接/断开命令、带 interface 的属性变化、方法调用的 caller_pid/caller_comm，以及日志覆盖错误。PID/程序名只是发送该 D-Bus 消息的进程，不等于内核所有断开事件的起因。工具不输出完整命令行或 D-Bus unique sender。

HCI 事件使用 RPA 而无法匹配登记身份时，新工具会缺少该事件的归属，不能强行归给目标。初始/最终目标内核快照提供另一层证据；需要时增加中间 snapshot。监控结束或报错后应先检查覆盖情况，不能把遗漏当作没有发生。Ctrl+C 结束观察不会主动断开蓝牙。

新工具支持传统及 Enhanced Connection Complete v1；v2（子事件 0x29）不解析。ATT 只记录完整未分片 PDU 的操作码、方向和长度，不重组分片、不记录值。HCI 与 D-Bus 独立读取，D-Bus 调用者查询不会阻塞 HCI；时间戳是本机读取时间，不是射频时间，进程调度和 D-Bus 调用者查询仍可能使跨来源记录延后。`accept_inbound` 表示电脑接受对端经典请求，不能误算为电脑主动发起。

## 实验 API 与隔离条件

`prefer`、`connect-le` 需要实际可用的 experimental 属性/方法。仅看到 Bearer.LE1 接口不够。`bond-info` 可在未启用 experimental 时读取已保存的偏好，不能用它证明动态 API 存在。

NixOS 可在专门测试配置中临时设置：

```nix
hardware.bluetooth.settings.General.Experimental = true;
```

按测试机自己的部署流程应用并记录原值；这需要 BlueZ 重启，不能当成无副作用的只读步骤。其他环境可在保存原 ExecStart 后使用 systemd runtime override，完整保留原 BlueZ 参数并追加 `--experimental`；不要复制历史机器的可执行文件路径。本专题不自动修改服务启动参数。

要判断“没有 HID 时原生 LE 是否能恢复”，必须隔离本项目自动连接来源：`bluetooth-auth-connect.socket`、连接服务、电源监听，以及 Noctalia 的普通自动连接。先记状态；Trusted 设 false 可抑制本轮 Noctalia 版本对目标的自动重连，但它也改变设备信任/服务授权条件，不是通道选择。

```sh
sudo systemctl stop bluetooth-auth-connect.socket \
  bluetooth-auth-connect.service bluetooth-auth-power-monitor.service
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" trusted --value false
```

若隔离期间还要重启 BlueZ，单纯 stop 不够：其依赖会重新拉起服务。本轮 runtime mask 也没有有效替代 NixOS 原单元。可使用只影响这两个服务的临时条件覆盖，先确认条件文件不存在：

```sh
test ! -e /run/bluetooth-auth/review-allow-start
for unit in bluetooth-auth-connect.service bluetooth-auth-power-monitor.service; do
  sudo mkdir -p "/run/systemd/system/$unit.d"
  printf '[Unit]\nConditionPathExists=/run/bluetooth-auth/review-allow-start\n' |
    sudo tee "/run/systemd/system/$unit.d/zz-le-review.conf" >/dev/null
done
sudo systemctl daemon-reload
```

重启之后核对两个服务确实未运行、HID UUID/广播实例确实为零，才开始对照。不要设置一个会在对照中途自行重启 BlueZ 的清理计时器；历史 R17 正因如此失效。

## 场景 A：普通连接会选择什么

前提：获准使用的双模配对，已有目标加密 LE，已开始监听。

```sh
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" connect
```

观察是否新增经典链路、原 LE 是否保留。工具的 `ok=true` 只表示 D-Bus 方法返回成功；加密 LE 判据仍须看 snapshot/实际认证程序。

另作一轮手机手动连接对照：先记录链路状态，再在 HID 广播窗口内点击手机原电脑条目，记录用户操作时刻及 HCI 谁发起何种连接。本轮 R04 得到经典连接；不要预先把手机的“已连接”当 LE。

## 场景 B：last-used 故障与 LE 偏好的反向对照

前提：双模配对、experimental API 可用，自动触发已隔离，手机状态不在轮次间随意改变。先得到加密 LE，设置默认策略：

```sh
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" prefer --value last-used
```

需要在此策略下**发生一次新的经典建链**，不能只看此前已经存在的经典连接。若经典已连，先按类型断开它，再用场景 A 的通用 Connect 新建。确认两条链路后，仅断 LE：

```sh
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" disconnect --transport le
sudo bluetooth-auth-link --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" --connect 15000
```

记录程序退出码、广播实际存在的窗口，以及加密 LE 有没有建立。经典尚在而 LE 没回来，是目标故障状态。没有失败就如实记录未复现，不继续补造相同结果。

若失败，保持手机、配对及其他条件不变，改偏好为 le：

```sh
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" prefer --value le
```

观察是否出现电脑发起 LE、何时加密，是否发生中途断开后第二次建立。setter 会改变原生自动连接队列，**这轮不是应用只调用一次 LE1.Connect 的测试**。R22 中第一条 LE 没完成加密就断开，第二条才成功，必须保留这个过程。

保持 le 偏好、经典连接存在，再仅断 LE观察，可检验经典是否必然阻止 LE。另一个对照是 bredr 偏好下运行 HID 广播；本轮仍有成功，不能声称该偏好禁止手机入站 LE。

显式 LE 方法单独测试用：

```sh
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" connect-le
```

本轮该调用有过超时，不能把 setter 的成功算给这个方法。控制工具给 D-Bus 调用有限等待；超时后仍需观察，调用方超时不证明 BlueZ/内核的待连接动作已取消。控制工具不会为此自动断开或重新配对。

## 场景 C：HID 持有、退出、静置

归档持有器复用项目 register_hid/advertise，绑定 hci0。它不修改 Pairable、Discoverable 或 Trusted；新版本改用地址文件参数和非阻塞锁，避免个人路径及无期限等锁。源码与历史持有器的差异已记录在 provenance。

持有器的 Cargo.lock 固定外部依赖，项目库仍通过相对路径引用当前源码。要复核本轮行为，应先比对 provenance 中的仓库提交和库文件哈希；以后修改了库，不能只凭这个工具名称就认为行为仍与本轮相同。新 checkout 的编译不要求修改现有工作树或暂存区。

```sh
nix develop --offline --no-write-lock-file --command \
  cargo build --offline --manifest-path \
  experiments/iphone_ble/diagnostics/hid-holder/Cargo.toml
sudo experiments/iphone_ble/diagnostics/hid-holder/target/debug/hid-lifetime-probe \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE"
```

离线构建前提是依赖已缓存；缺依赖时按本机开发环境准备，不为此改变蓝牙。锁文件由已接入的项目模块准备，不能删除/重建正在使用的锁文件。

出现 HID_READY 后，在另一终端仅断 LE，观察重连。再 Ctrl+C 退出持有器，看到 HID_CLOSED，snapshot 确认 HID UUID 不在、广播实例0，再确认原 LE 是否保留。

重做短命流程时，先确保持有器已退出并释放锁，再断 LE并运行原 `bluetooth-auth-link`。静置对照要连续监听：如果没有 HID 时系统已经自动连回，后面程序的成功只能算查询成功，不能算 HID 恢复成功。

## 可选处理：只移除目标经典配对

**这是对现有配对数据的删除，不是普通复核前提。** 仅在设备所有者明确批准、目标经典已经断开且目标加密 LE 存在时执行；本轮批准范围不自动授权日后另一份配对。全设备 Forget/RemoveDevice/Blocked 不等价，不能替换此步骤。

```sh
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" bond-info
sudo python3 -B experiments/iphone_ble/diagnostics/le_bearer_control.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" remove-classic-pairing \
  --backup-file "$BLE_REVIEW_DIR/pairing-backup.info"
```

工具以排他创建、0600 权限保存私有备份，发送 BR/EDR type0 的 Unpair Device、Disconnect=0，等待持久状态更新，比较全部已存在 LE 密钥组。验证通过后才删除备份；失败保留备份并报告，不擅自覆写 BlueZ 状态。备份含密钥，不能归档、贴到终端或提交；需要恢复时由设备所有者在受控的 BlueZ 停止/重载流程中处理，而不是运行中的随意覆盖。

这项操作只移除电脑上的旧经典绑定，手机仍可能保有旧记录。后续不得把普通手机连接失败解释为 LE 密钥必然丢失；先查实际 LE。将来重新配对仍可能产生经典密钥。

## 恢复与结束判据

逐项恢复实际改过的内容，不调用一个未知范围的“全部重置”：

1. 退出本轮 HID 持有器和监听；保留需要核查的公开元数据。
2. 如果在试验中改了偏好/Trusted，按开始记录恢复；只有经批准的实际修复保留新值。读取保存值和 API 实际可用性是两个问题。
3. 移除**本轮创建**的 zz-le-review.conf 和 experimental 测试配置；不要删除原有单元或其他覆盖文件。执行 daemon-reload，按测试机原部署恢复 BlueZ 参数。
4. 恢复原先启用的 socket、连接任务和电源监听。**先让 socket 进入 listening，再启动关联的 oneshot**；本轮曾反过来启动而清理失败。不同环境还需核对其他触发关系。
5. 按原参数重启 BlueZ 后观察真实角色、LE 加密及经典是否存在；这是服务重启，不是整机重启试验。
6. 再仅断 LE，运行原程序 `--connect 7000`；记录从程序开始至成功的耗时，不把等待间隔算进去。
7. 使用 `--connect 0` 做纯查询；在实际用户 Noctalia 锁屏界面按回车验证。用户要求保持停止的自动锁屏不能擅自重开。本轮未把停止状态算作自动锁屏长期验证。
8. 确认无本轮 HID/广播、无私有密钥备份遗留、无诊断进程、无测试覆盖。预期修复后的持久记录是经典 Key 不存在、LE Key 保留、偏好 le。

PAM 的独立自动回归可参考项目 `tests/pam_integration.py`，它使用私有配置和替身，不代替实际用户解锁。本轮真实 PAM 检查是提供空密码后调用 pam_authenticate/pam_acct_mgmt、不打开 session；没有把测试脚本自身退出0当作真实界面通过。

## 离线验证和提交材料

从项目根目录执行；这些命令不操作蓝牙：

```sh
python3 -B -m unittest discover -s experiments/iphone_ble/tests -p 'test_le_bearer_*.py'
python3 -B experiments/iphone_ble/diagnostics/verify_bearer_archive.py
```

`verify_bearer_archive.py` 同时检查本专题的公开文本隐私模式、事件编号和产物哈希。另可运行 `python3 -B experiments/iphone_ble/check_public_privacy.py --root .` 扫描整个仓库；本次扫描还会报告旧源码中的合成测试地址与文档示例路径，这些未在本专题中修改，不能把整仓结果写成零发现。

归档新运行使用新文件/ID，保留失败、未复现、日志缺口与人工动作。不要覆写 A/B 历史记录或将新版工具输出冒充旧记录。公开材料只保留角色、协议元数据、时间、状态、软件来源与公开产物哈希；私有原始输入的哈希也不作为公开关联标识。
