# iPhone BLE 实验实施说明

本文说明 `experiments/iphone_ble` 当前实现的边界、复现入口和结论解释方式。它服务于实验复现与结果审阅，**不是**产品接入或认证方案。真实蓝牙动作只能由操作者在本机执行；这里的离线测试、假后端和内存控制器测试均不能替代手机与控制器的实测。

## 结论范围与已有记录

实验采用 Bumble 独占控制器或系统 BlueZ 两种方式。BlueZ 下分别提供共存验证、HID 退出后的链路保持，以及无 HID 时的只读观察入口，每次运行一个场景。README 是索引；用户运行记录与失败经验见 [RESULTS.md](../RESULTS.md)，实际复现步骤见 [REPRODUCE.md](REPRODUCE.md) 和 [HID_RELEASE.md](HID_RELEASE.md)。本文只解释实现与证据边界，不重复运行历史。

正式结果应始终区分“用户实测”“代码行为”和“离线测试”。已有成功或失败都只适用于其记录的配置，不能推出所有 iOS、控制器、长期连接、音频配置文件或共存场景的结论；`inconclusive` 也不能解释为协议不支持。

## 概念与两种机制

### 物理控制器、身份、名称和手机条目

- **物理控制器**是明确指定的 `hciN`。独占路径用 HCI user channel 暂时接管它；共存路径不接管，仍由 `bluetoothd` 管理。
- **蓝牙身份**由身份地址和地址类型标识，配对记录保存与身份关联的密钥。Bumble 路径为每个实验模式建立独立随机静态身份和私有密钥；BlueZ 共存路径使用原适配器身份，不复制或导入 Bumble 身份。密钥更新不表示增加了物理控制器。
- **名称**是广告数据或系统别名中的可见文本，不等于蓝牙身份。独占路径广播 `BT-Auth-*` 名称；共存路径把调用方传入的原 `Adapter1.Alias` 作为广告 `LocalName`，不改 Alias。
- **iPhone 列表项**由 iOS 自己维护，不是电脑侧 BlueZ `Device1` 对象。电脑侧的 `Device1`/RPA 对象只表示 BlueZ 看见或管理的远端设备。共享身份不能靠名称保证 iPhone 显示几个条目：Bumble 三个独立身份可分别形成条目；BlueZ 共存沿用原电脑身份，不创建另一套测试身份，也不承诺 iOS 立即合并或清除已有缓存条目。
- **电脑侧手机对象**也不等同于稳定物理地址。LE 隐私下，一个连接可使用 RPA；BlueZ 可能以 IRK 将其映射到身份 Device。修复模式因此记录候选路径及本轮地址，并要求后续身份收敛，不能仅按旧地址猜测。

BlueZ 5.87 的 `Gatt`/广告对象属于本进程导出的 D-Bus 服务，物理广播、连接和安全仍由 `bluetoothd` 与控制器执行。Linux MGMT `Get Connections` 直接返回内核连接对象的地址，不经过 BlueZ 的用户态 IRK 映射；RPA 与身份地址都必须按会话上下文处理。[Linux `mgmt.c`](https://github.com/torvalds/linux/blob/master/net/bluetooth/mgmt.c)；[BlueZ `device.c`](https://github.com/bluez/bluez/blob/5.87/src/device.c)。

### GATT、HOGP 与 BLE 角色

三种候选的设计中，电脑均作为 LE peripheral，手机发起链路并作为 central。就被测业务服务而言，**HID** 模式由电脑作为 GATT server、手机读取电脑导出的 HOGP 属性；HID 服务是标准 0x1812，包含 HID Information、Consumer Control Report Map、HID Control Point、单字节 Input Report 和 Report Reference Descriptor；Input Report 固定为 `0x00`，实验不发送输入报告。[coexist_gatt.py](../coexist_gatt.py)

**ANCS 与 CTS** 的被测业务中电脑是 GATT client：ANCS 订阅手机提供的 ANCS，CTS 读取手机提供的 Current Time；这不妨碍电脑同时提供基础 GAP/GATT 属性。BLE 的 central/peripheral 与 GATT 的 client/server 是两组不同角色。

HID 的 Report Map 或 Report 读取会产生 `target_gatt_access`，其中带 BlueZ 传入的 device path、link 和属性名。服务的 `encrypt-read`/`encrypt-write` flags 由 BlueZ 在 ATT 层执行；代码还要求允许的设备路径和 `link.lower() == "le"`。[coexist_gatt.py](../coexist_gatt.py)

`StartNotify` 是 `GattCharacteristic1` 方法，且没有设备参数，故代码只记录 `gatt_subscription` 的 `scope: global`。它不能证明订阅者是目标手机，成功判定不能把任意全局订阅解释为目标手机 HID 订阅。

### 非音频与音频隔离的边界

实验提供的 HID 均不注册 A2DP、HFP 或 LE Audio，也不发送媒体控制输入。`bluez_hid_lab.py` 会检查目标手机是否出现新的 `MediaTransport1`，若出现即失败；它还要求至少有一个预先存在的其他蓝牙连接在测试期间保持。[bluez_hid_lab.py](../bluez_hid_lab.py)

新增的 `hid_release_test.py` 保护原有其他连接，但不要求一定存在陪测设备，也不进行上述音频传输专项判定；`observe_le_link.py` 只观察目标 LE。二者的 passed 不增加音频共存或路由隔离的成功次数。

这些是有限的隔离检查，不能证明所有音频配置文件、路由选择、耳机体验或输入法行为均未受影响。实际音频输出与鼠标/耳机可用性必须由操作者在保持期观察并记录。

## 文件与职责

| 文件 | 职责 | 是否产生真实无线动作 |
| --- | --- | --- |
| [ble_lab.py](../ble_lab.py) | Bumble 独占入口、状态目录命令、适配器交接/恢复编排。 | `run` 会；`plan`、`report`、`prepare` 不会。 |
| [radio.py](../radio.py) | Bumble 服务构造、传统广播、连接/加密/服务就绪周期。 | 由独占 `run` 调用时会。 |
| [adapter.py](../adapter.py) | BlueZ D-Bus 后端、独占适配器交接与写前恢复记录；共存入口复用其 `BlueZBackend`。 | 仅运行/恢复调用时会。 |
| [state.py](../state.py) | 私有状态、原子日志与路径/权限检查。 | 不直接操作无线。 |
| [bluez_hid_lab.py](../bluez_hid_lab.py) | BlueZ 共存短测、D-Bus 注册、候选配对、清理和恢复。 | `run`/有记录的 `restore` 会。 |
| [hid_release_test.py](../hid_release_test.py) | 临时 HID 提供子进程与独立观察父进程；确认服务退出后同一加密 LE 是否保持。 | 运行时注册服务和广播；`--help` 不访问蓝牙。 |
| [observe_le_link.py](../observe_le_link.py) | 无 HID/广播时，只读等待并观察目标加密 LE。 | 读取系统 D-Bus、MGMT、HCI 状态，不扫描、广播或发起连接。 |
| [link.py](../../../src/bluetooth_auth_hid/link.py) | 新增观察入口复用的内核 LE 句柄、连接状态和加密位读取；归档清单包含该依赖。 | 由观察入口调用时读取 HCI 连接信息。 |
| [coexist_gatt.py](../coexist_gatt.py) | 纯 D-Bus HOGP、Battery、DIS 和广告对象；可导出/撤销导出。 | 不连接或注册 D-Bus。 |
| [coexist_pairing.py](../coexist_pairing.py) | 纯 `Agent1` Numeric Comparison 对象；不注册 Agent。 | 不连接或配对。 |
| [coexist_link.py](../coexist_link.py) | MGMT Get Connections 与受基线约束的目标 LE 断开。 | 由共存运行/恢复调用时会。 |
| [coexist_trace.py](../coexist_trace.py) | 可选的控制器广告命令只读摘要。 | 由 `--trace-advertising` 调用时打开 `HCI_CHANNEL_MONITOR`（2），不是 MGMT control channel（3）。 |
| [check_offline.py](../check_offline.py) | 在临时目录运行测试，拦截 Python 蓝牙 socket 与真实 socket 连接。 | 不运行真实实验；不替代操作系统沙箱。 |
| [check_public_privacy.py](../check_public_privacy.py) | 静态检查仓库文件中的地址、个人路径与密钥特征。 | 否，只读取文件和 Git。 |
| [diagnostics/monitor_rssi.py](../diagnostics/monitor_rssi.py) | 早期 BR/EDR RSSI 辅助诊断，不被正式认证或 HID 实验入口调用。 | 由操作者单独运行时会扫描并查询已有连接。 |
| [tests/](../tests/) | 离线逻辑测试；另有独立临时 D-Bus 上的真实子进程退出回归。 | 不连接系统 D-Bus 或真实控制器。 |

## Bumble 独占实验

### 工作方式

独占入口在交接前记录原 BlueZ 设置和连接基线，然后关闭该适配器的 BlueZ 使用权并打开 HCI user channel。该设计刻意使原有连接可能中断，因而不能用于共存验证。每个 `ancs`、`hid`、`cts` 模式拥有固定随机静态地址、IRK、独立密钥文件；Classic、Classic SMP 与 CTKD 被关闭。[radio.py](../radio.py)；[adapter.py](../adapter.py)

广播为传统可连接、可扫描 LE 广播，配置 20 ms 间隔。HID 的完整名称和 Appearance 可进入主广告；ANCS 128 位征询 UUID 可能使名称进入 scan response。能否出现在 iPhone 设置页仍是实测问题，不能由广播构造成功推断。

| `ble_lab.py` 命令 | 关键参数 | 输出/退出语义 |
| --- | --- | --- |
| `plan` | 无 | 只显示计划；成功为 0。 |
| `setup` | 无 | 创建本目录 `.venv` 所需依赖；不访问蓝牙。 |
| `prepare` | `--state-dir` | 创建固定实验身份和私有状态；不开始广播。 |
| `run MODE` | `--adapter hciN`、`--enroll`、`--initial-timeout`、`--reconnect-timeout`、`--hold-seconds`、`--cycles` | 真实独占实验。0 表示配置的所有周期通过；1 为实验失败；2 为配置/恢复错误；130 为中断。 |
| `report` | `--state-dir` | 只读取已有结果。 |
| `restore` | `--state-dir` | 仅按恢复记录恢复适配器。 |
| `clean` / `uninstall` | `--state-dir` | 仅在无待恢复记录时移除受管理状态/工具；属于破坏性操作。 |

运行事件按阶段输出：`adapter_handoff`、`radio_ready`、`advertising`、`link`、`security_request`、`encryption`、`bond_saved`、`service_ready`、`hold_complete`、`cycle_disconnect`、`result` 与 `restore`。只有到达 `service_ready` 并完成配置的保持与周期，才应称该轮通过；注册广播、列表可见或建立 ACL 链路都不足以单独证明成功。[radio.py](../radio.py)

### 服务差异

- **ANCS**：在手机发起并已建立的 BLE 链路上，电脑以 ANCS client 身份订阅必要属性；不读取通知正文或执行通知操作。
- **HID**：电脑提供消费控制 HOGP server；报告值为零且不通知，避免把实验当作真实键盘/媒体控制器。
- **CTS**：电脑读取手机的 Current Time，不伪造 CTS server。

上述行为是候选机制比较，尚未形成正式认证或身份验证流程。

## BlueZ HID 共存短测

### 工作方式

共存入口不关闭 `bluetoothd`、不占用 HCI user channel、不改 Adapter Alias/Appearance，也不主动扫描、连接、配对或删除设备。它在当前系统 D-Bus 连接上导出应用对象，再调用 `GattManager1.RegisterApplication`；导出广告对象后调用 `LEAdvertisingManager1.RegisterAdvertisement`。[bluez_hid_lab.py](../bluez_hid_lab.py)

广告对象为 peripheral、HID UUID 0x1812、Appearance 0x03C0、每广告 `Discoverable=true`、20 ms min/max interval。BlueZ 5.87 的常规 legacy 路径将 `LocalName` 放到 scan response，而 Appearance 进入主广告；不能把名称位于 scan response 解释为注册失败。[BlueZ `advertising.c`](https://github.com/bluez/bluez/blob/5.87/src/advertising.c)。`Data` 不能作为把完整名称强塞入主包的替代：BlueZ 的 AD 数据实现拒绝短/完整名称及 Appearance 等保留类型。

应用加入 HID，并按需补充 DIS 和 Battery；若原 Adapter UUIDs 已有 DIS (0x180A) 或 Battery (0x180F)，`HidApplication(include_dis=False/include_battery=False)` 不再注册第二份服务。共享 GAP/GATT 由 BlueZ 保持，不导出第二份 0x1800/0x1801。[coexist_gatt.py](../coexist_gatt.py)

| `bluez_hid_lab.py` 命令 | 关键参数 | 退出码与结论 |
| --- | --- | --- |
| `plan` | 无 | 仅显示边界，0。 |
| `run` | `--adapter hciN`、`--phone` 或 `--phone-file`、`--wait-seconds`、`--hold-seconds`、`--repair-phone-pairing`、`--trace-advertising` | 0：保持期通过；1：测试失败/原连接缺失；2：配置或恢复错误；3：证据不足；130：中断。 |
| `restore` | `--state-dir` | 处理遗留记录、恢复 Pairable、撤销新增目标 LE 链路并核对临时资源。 |

成功标准同时要求：预先存在的其他连接未中断、目标会话的身份/安全证据成立、出现目标加密 LE HID ReadValue 访问，并完成保持期。`phone_hid_subscription_verified` 保持为 false；全局订阅仅是诊断信息。目标 LE 连接存在但没有归属读取、身份未收敛或修复模式未出现确认，均应为 `inconclusive`，而非“手机不支持”。[bluez_hid_lab.py](../bluez_hid_lab.py)

### 复用配对与修复配对

默认模式要求配置地址对应的旧 Device 未 Blocked，且 Paired/Bonded/Trusted；脚本临时将 Pairable 关闭，拒绝新增配对。它不会读取 Bumble 私钥、不会导入密钥，也不会调用 `Device1.Connect`、`Device1.Pair` 或 `Adapter1.RemoveDevice`。

地址来自操作者指定的私有文件：`--phone-file` 优先于 `BLUETOOTH_AUTH_ADDRESS_FILE` 环境变量中的路径，源码不内置个人地址或文件位置；无配置或文件无效时，在打开蓝牙后端前失败。原始运行日志仍属于私有资料，公开归档使用角色占位符。

`--repair-phone-pairing` 面向手机已忽略旧配对、电脑仍留条目的情况：

1. 仅临时注册本进程的 `DisplayYesNo` 默认 Agent，并仅在该模式将 Pairable 设为 true。
2. `PairingAgent` 只接受一个同适配器、未阻止、当前 LE 已连接的候选 Numeric Comparison；PIN、Passkey、Just Works、并发确认、其他设备及非 HID 服务授权全部拒绝。[coexist_pairing.py](../coexist_pairing.py)
3. 操作者必须在实际 iPhone 上核对六位数字。RPA 不是自动身份断言；候选地址/路径只用来约束本轮连接。
4. 通过数字确认后仍须等待候选映射到配置身份、Paired/Bonded 成立以及加密 HID 读取。旧 Device 的 `Paired=true` 不满足该条件。

修复模式让 BlueZ 正常保存新配对记录或密钥；清理与 `restore` **不会**删除、覆盖或回滚这些记录。输出中的 `pairing_records: retained` 是这个行为的描述，`settings_restored` 不表示旧密钥被恢复。

### 事件与诊断

共存入口输出 `advertising`、可选 `advertising_trace`、`waiting`、`service_ready`、`hold_complete`、`probe_error`、`restore` 和 `result`。配对模式额外输出 `pairing_confirmation_requested`、`pairing_confirmation_accepted`、`pairing_agent_rejected`、`pairing_agent_cancel` 等。日志把 Device path 归类为角色，避免记录地址文件内容、报告内容或密钥。

`--trace-advertising` 是对本机控制器广告命令/回复的受限摘要：它能辅助判断 BlueZ 是否下发了 HID UUID、Appearance、Flags 和名称位置，但不能证明 iPhone 实际收到、扫描或展示了广告。

摘要中的 `closed` 是取得摘要时的状态；当前入口先取摘要，再在 `finally` 中关闭 monitor，因此历史最终输出中的 `closed=false` 不能单独作为资源泄漏证据。清理异常会另行记录；不要用摘要采集时刻代替退出流程判断。

## HID 退出与只读观察

两个新增入口均接受 `--phone-file`、`--adapter`、`--wait-seconds`、`--observe-seconds`，默认等待 20 秒、观察 60 秒；每个时限必须大于零且不超过 120 秒。地址文件显式传入，环境变量只是复现命令中的路径引用。具体命令见 [HID_RELEASE.md](HID_RELEASE.md)。

| 入口 | 提供进程与观察流程 | 通过所需的证据 |
| --- | --- | --- |
| `hid_release_test.py` | 子进程注册 HID/广播；父进程等待目标加密 LE，保持 5 秒基线，再让子进程关闭 D-Bus 并退出，确认资源撤销后观察连接 | 子进程退出码 0、D-Bus 关闭确认、总线身份消失、HID UUID 移除且广播实例为 0；随后同一加密 LE 保持 |
| `observe_le_link.py` | 不创建 HID 提供进程，仅在 HID UUID 缺失、广播实例为 0 的条件下等待和观察 | 同一加密 LE 保持，整个观察期间可核实无 HID/广播；无法读取资源状态时不能判通过 |

两者按指定适配器和身份地址匹配目标，联合读取 MGMT 目标 LE 与唯一 HCI LE 链路，核对加密位，跟踪连接句柄和 MGMT 断线事件；断线后重连不能抵消连续性失败。身份不能对应或读取结果不足时，不猜测 RPA 归属，不以 BlueZ 的通用 Connected 属性代替目标加密 LE。

`Paired/Bonded/Trusted` 用于记录并检查本轮是否发生变化，不作为本项链路保持的启动门槛；Blocked 则会拒绝。两个入口均不设置 Trusted，不调用 Connect、Pair 或 Disconnect。HID 的加密读取只是辅助事件，`encrypted_hid_read_seen=false` 不影响链路保持通过，也不能被写成读取或订阅已验证。

`hid_release_test.py` 以 `connection_setup=existing/automatic/manual` 区分起点。已有连接直接复用；`--manual-connect` 只改变操作者的连接步骤与记录，不是超时后的自动回退。通过为退出码 0，链路丢失为 1，观测不足/配置错误为 2，中断为 130。父观察进程在提供进程退出后继续运行，系统 `bluetoothd` 始终管理链路；服务对象生命周期与底层连接生命周期不能混为一谈。

## 清理、恢复与配对记录

`ble_lab.py` 与 `bluez_hid_lab.py` 各有独立锁与恢复记录：独占路径使用 `.runtime/adapter-restore.json`，共存路径使用 `.coexist/restore.json`。新运行会拒绝覆盖待恢复记录。恢复按照适配器物理地址重新定位，避免 `hciN` 改号时修改其他控制器。

共存清理的顺序是：注销广告和 GATT 应用、撤销导出对象；修复模式取消待确认、先关闭 Pairable 窗口、注销/撤销临时 Agent；关闭控制通道和 D-Bus；最后执行恢复核对。恢复只断开启动时不存在且属于记录目标/候选集合的 LE 链路；拒绝断开基线连接，绝不对所有设备执行通用连接或断开。[bluez_hid_lab.py](../bluez_hid_lab.py)；[coexist_link.py](../coexist_link.py)

若恢复失败、出现新增 Classic 手机连接、原连接缺失或临时资源仍在，记录保留并报告原因。不要手动删除恢复文件来掩盖未验证的状态。独占 `clean`/`uninstall` 的删除范围只覆盖该实验受管理状态；手机上的测试记录、系统日志、缓存和外部服务状态不在可保证的清理范围内。

`hid_release_test.py` 与 `observe_le_link.py` 不写恢复记录，也没有 `restore` 子命令。它们结束时只关闭本轮提供/观察资源，保留仍在的目标连接和配对；不应套用共存入口主动断开新增 LE 的恢复步骤。

## 离线验证与复现纪律

从项目根目录可运行：

```sh
experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/check_offline.py
```

离线测试覆盖对象导出、D-Bus 签名、offset、授权拒绝、广告属性、导出回滚、MGMT 报文解析、配对代理的批准/拒绝/取消/并发处理，以及恢复分支。它们使用 fake 后端、临时目录或内存控制器；不应被描述为手机发现、配对、RPA 解析、音频路由或无线兼容性的实测。

HID 退出/只读观察的逻辑回归也包含在 `check_offline.py` 中。独立的 [check_hid_release_dbus.py](../tests/check_hid_release_dbus.py) 则在私有 Unix D-Bus 上运行三个真实提供子进程，读取 GATT 后通过 EOF 触发关闭，核对退出码、关闭确认及总线身份消失。它不连接系统 BlueZ，不能证明无线连接行为；运行条件和命令见 [HID_RELEASE.md](HID_RELEASE.md#离线验证)。

真实复现前应明确指定 `hciN`。运行 `bluez_hid_lab.py` 时，保留一台已连接的其他蓝牙设备作为共存基线，并在结束后核对 `result`、`restore`、`connections_restored` 与 `original_connections_present`；HID 退出/只读观察按各自的资源与链路判据核对。对失败仅记录可观察事实，不把单次失败扩展为协议或平台的普遍结论。

## 已知限制与后续边界

1. 广告被 BlueZ 接受、控制器下发数据或手机设置页没有名称，分别是不同证据层级；scan response 名称尤其依赖手机主动扫描。
2. GATT `StartNotify` 缺少 Device 参数，不能从代码取得目标手机专属订阅证据。
3. 手机可能复用缓存而不重新读取 Report Map/Report；`bluez_hid_lab.py` 的服务访问判据会给出 `inconclusive`，HID 退出/只读观察的链路判据不要求重新读取。不同入口的 passed 不能互换。
4. RPA、身份地址与内核 MGMT 连接地址可能不同；修复模式必须保留会话候选地址并等待 BlueZ 身份收敛，不能只依赖 `Device1.Address`。
5. 配对代理只限制本进程 Agent 的响应；它不是长期策略管理、设备准入系统或正式认证协议。
6. 此目录没有实现正式认证接入、持久化授权策略、生产级密钥轮换、产品 UI、认证文档或跨设备兼容性保证。

## 外部资料与适用边界

资料用于解释协议和 API，不能替代本次实测；BlueZ 引用固定为 5.87，Apple 档案资料不作为当前全部 iOS 行为的保证。

| 资料 | 保留用途 |
| --- | --- |
| [BlueZ LEAdvertisement API](https://github.com/bluez/bluez/blob/5.87/doc/org.bluez.LEAdvertisement.rst) | 区分 LocalName、ServiceUUIDs、SolicitUUIDs、Discoverable 与广播间隔 |
| [BlueZ Agent API](https://github.com/bluez/bluez/blob/5.87/doc/org.bluez.Agent.rst) | 数字确认、拒绝及取消代理请求 |
| [BlueZ GattCharacteristic API](https://github.com/bluez/bluez/blob/5.87/doc/org.bluez.GattCharacteristic.rst) | 加密访问标记、ReadValue 与全局 StartNotify 的边界 |
| [BlueZ Device API](https://github.com/bluez/bluez/blob/5.87/doc/org.bluez.Device.rst) | 设备与配对属性；通用 Connect 不能保证仅连接非音频配置文件 |
| [BlueZ MGMT protocol](https://github.com/bluez/bluez/blob/5.87/doc/mgmt-protocol.rst) | 连接列表、地址类型与指定链路断开 |
| [Apple ANCS 规范](https://developer.apple.com/library/archive/documentation/CoreBluetooth/Reference/AppleNotificationCenterServiceSpecification/Specification/Specification.html) | iPhone 提供通知服务的候选依据 |
| [TI ANCS 示例](https://github.com/TexasInstruments/ble-sdk-210-extra/blob/master/Projects/ble/ancs/README.md) | 服务征询、配对和授权流程的参考实现，不是本机成功证据 |
| [Apple QA1931](https://developer.apple.com/library/archive/qa/qa1931/_index.html) | 初始 20 ms 广播间隔建议的来源，不保证设置列表可见性 |
| [旧版 Apple 蓝牙配件指南](https://www.bluetooth.com/wp-content/uploads/attachments/BluetoothDesignGuidelines.pdf) | CTS 等 iOS 服务及维持连接的历史资料，不能替代当前 iPhone 实测 |
| [Bumble Linux 文档](https://google.github.io/bumble/platforms/linux.html) | HCI user channel 独占方式；本目录依赖固定为 Bumble 0.0.234 |
| [HID over GATT Profile](https://www.bluetooth.com/specifications/specs/hid-over-gatt-profile-hogp/) | 标准 HOGP 服务与报告机制 |
