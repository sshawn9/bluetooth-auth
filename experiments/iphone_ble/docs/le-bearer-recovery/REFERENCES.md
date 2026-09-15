# LE bearer 恢复：源码与资料索引

本文归档 2026-09-15 双 bearer 排查所依据的源码、工具边界和结论。
它服务于后续设计审阅，不是操作步骤。实验时间线与观测值见[专题报告](../../results/LE_BEARER_RECOVERY.zh-CN.md)；本文不包含地址、密钥或本机路径。

## 已确认的范围

* 双模设备可同时保有加密 LE 与 BR/EDR 链路，故通用
  `Device1.Connect` 不是“只连 LE”的请求。
* 在 `last-used` 策略下，BR/EDR 成功连接会令 BlueZ 移除该设备的
  电脑端 LE 自动连接；LE 成功连接则反向处理。这解释了受控复现中的
  恢复链，**不能**倒推原始历史 LE 丢失的发起时间或发起方。
* 通过实验性 setter 设置 `PreferredBearer=le` 会把目标加入 LE
  自动连接路径。随后 daemon 仅加载已保存偏好时，恢复的是选择器状态，
  不会单独重新加入自动连接；具有 `auto_connect` 和 `accept` 的已匹配
  profile 可独立触发加入。
* 本轮用户批准的 BR-only unpair 移除了旧 BR/EDR link key，保留了
  LE 的 LTK/IRK 路径；它不是拒绝未来 BR/EDR 重新配对的规则。
* 已验证的修复是“BR-only key removal + 固定 `le` 偏好”，且执行时既有
  加密 LE 链路仍在。它不覆盖全部 iOS 版本、射频条件或休眠/恢复情形。

## 主要源码对照

| 组件 | 固定参考与审阅函数 | 能支持的结论 | 不能支持的结论 |
| --- | --- | --- | --- |
| BlueZ 5.87：设备策略 | [device.c](https://github.com/bluez/bluez/blob/5.87/src/device.c)：`dev_property_set_prefer_bearer`、`device_set_auto_connect`、`device_update_last_used`、`select_conn_bearer`、`dev_connect`、`load_info`、`probe_service`、`device_add_connection`、`device_set_unpaired` | 偏好持久化/选择、setter 加入内核自动连接、last-used 转换，及通用 Connect 的 BR/LE 分支 | 公共的“仅拒绝这个设备 BR/EDR”策略；仅加载 `PreferredBearer=le` 就自动恢复 auto-connect |
| BlueZ 5.87：适配器与 bearer API | [adapter.c](https://github.com/bluez/bluez/blob/5.87/src/adapter.c)：`adapter_auto_connect_add`、`remove_keys`、`unpaired_callback`；[bearer.c](https://github.com/bluez/bluez/blob/5.87/src/bearer.c)：`bearer_connect`、`bearer_disconnect` | `MGMT_OP_ADD_DEVICE` 的 LE 自动连接登记；按地址类型清理持久密钥；bearer `Disconnect` 只拆当前链路 | `Bearer.BREDR1.Disconnect` 会阻止下一次 BR/EDR 连接 |
| BlueZ 5.87：D-Bus 与 GATT | [Device API](https://github.com/bluez/bluez/blob/5.87/doc/org.bluez.Device.rst)、[gatt-database.c](https://github.com/bluez/bluez/blob/5.87/src/gatt-database.c)、[GattCharacteristic API](https://github.com/bluez/bluez/blob/5.87/doc/org.bluez.GattCharacteristic.rst) | `Trusted` 是持久设备属性；GATT 应用登记与 ATT 授权独立于 bearer 链路 | 改 `Trusted`、登记 HID 或释放 GATT handle 会直接断开既有 LE 链路 |
| Linux 蓝牙管理层 | [Linux v7.2 `mgmt.c`](https://github.com/torvalds/linux/blob/v7.2/net/bluetooth/mgmt.c)：`unpair_device`、`unpair_device_sync`、`load_irks`、`add_device`；[连接路径](https://github.com/torvalds/linux/blob/v7.2/net/bluetooth/hci_conn.c) `hci_connect_le`；[事件路径](https://github.com/torvalds/linux/blob/v7.2/net/bluetooth/hci_event.c) `check_pending_le_conn` | type 0 unpair 调 `hci_remove_link_key`，LE type 才移除 LTK/IRK；`disconnect=0` 不终止链路；后台连接可用解析后的当前 RPA | 下游 CachyOS 必然没有 Bluetooth patch；v7.2 是上游对照源 |
| 实际内核的限定 | 实测内核版本为 `7.2.4-cachyos-lto`；匹配的 Nix dev 输出确认版本与头文件树，C 实现以 Linux v7.2 对照 | 目标操作在上游实现中的 type 分支明确，且之后由密钥/链路状态实测验证 | 这是逐字节的 CachyOS 源码审计 |
| Noctalia 5.1.0 | 上游 revision [`c7b9197af77ff22bfb9a83c52a95643a1d90ca86`](https://github.com/noctalia-dev/noctalia/tree/c7b9197af77ff22bfb9a83c52a95643a1d90ca86)，release ref `v5.1.0`；`src/dbus/bluetooth/bluetooth_service.cpp`、`src/shell/control_center/tabs/bluetooth_tab.cpp` | 控制中心的 Auto reconnect 开关调用 `setTrusted`；该行不分派 Connect、Disconnect 或 Forget；重新连接调度另行筛选 paired/trusted/unconnected | 点击该 UI 开关直接造成历史断链或直接发起连接 |
| bluer 0.17.4 | [crate source](https://github.com/bluez/bluer/tree/v0.17.4)：`src/adv.rs` 的 `AdvertisementHandle`、`src/gatt/local.rs` 的 `ApplicationHandle`、`src/adapter.rs` 的 `advertise`/本地 GATT 登记 | handle 必须存活；drop advertisement handle 会注销广告，drop local GATT application handle 会取消发布 | 这些对象生命周期决定已建立控制器 LE 链路的生命周期 |
| WirePlumber | [Bluetooth 配置示例](https://github.com/PipeWire/wireplumber/blob/master/src/config/wireplumber.conf.d.examples/bluetooth.conf) | `bluez5.auto-connect` 是 A2DP/HFP/HSP 等音频 profile 策略，作用于 BlueZ/PipeWire 已暴露的音频设备 | 针对单台设备拒绝 BR ACL/HID，或实现 bearer-level block |

## 被否定或必须限定的假设

| 假设或调查线索 | 结论与依据 |
| --- | --- |
| `MGMT Block Device(type=0)` 可作为安全的“只屏蔽目标 BR/EDR”方案 | **不成立（在 bluetoothd 管理设备时）。** Linux MGMT 的地址/type 记录可按 type 区分，但 BlueZ 5.87 接收 Device Blocked event 后，以同一路径的双 bearer `Device1` 调 `device_block(TRUE)`，会把 LE 一并作为整个 Device1 阻断。它不能替代 BR-only key removal。 |
| WirePlumber 可以按设备禁用 BR/EDR 自动连接 | **不成立。** 它的 BlueZ monitor 配置处理音频 profile；没有接收/拒绝 BR ACL 或 HID 链路的接口。即使禁用音频 profile，也不能阻止手机先建立经典链路。 |
| 默认 `Privacy=off` 会关闭对端 RPA/IRK 解析 | **不成立。** `Privacy` 管的是本机地址与本地 privacy policy；BlueZ 在控制器支持相应 MGMT setting 时加载 IRK，内核广告处理仍以 IRK 匹配对端 RPA。它不是本轮显式 LE 与后台自动连接差异的已证实原因。 |
| 本地 HID GATT 服务只能通过 LE 访问 | **不能这样假定。** BlueZ 本地 GATT/ATT 架构可涉及 BR/EDR ATT bearer；本轮没有证据显示 iPhone 曾把 HOGP 访问迁到 BR/EDR，也没有把“无 LE”以外的服务路径视为认证成功。目标判定始终要求 HCI/MGMT 的加密 LE。 |
| 出现 `org.bluez.Bearer.LE1` 就说明 `LE1.Connect` 可用 | **不成立。** Bearer 的非实验 `Disconnected` signal 可使接口出现在 introspection；Connect/Disconnect 和属性是实验性成员。需要 `-E` 或 `General.Experimental=true` 才暴露；未找到已审阅的运行中 reload 接口。 |
| 同一路径的 `PropertiesChanged(Connected=false)` 就是 aggregate `Device1` 断开 | **不能这样归因。** `Device1` 与 bearer 接口共享对象路径；未记录 interface name 的监听器不能区分 `Device1`、`Bearer.LE1`、`Bearer.BREDR1`。 |
| 移除 Linux LE connect/auto-connect list 会阻止 iPhone central 连本机广告 | **没有支持证据。** 该 list 控制 Linux 作为 central 的扫描和主动连接；没有找到入站 LE Connection Complete 后按该 list 拒绝本机可连接广告的代码。实际入站 LE 重连也已观测成功。 |
| 广告/GATT 登记成功等于手机已连接；或 handle 释放等于链路断开 | **不成立。** 登记与广播仅说明本机提供服务；链路结论需 HCI/MGMT 连接与加密状态。 |
| 第三方 tether 文档中的入站阻断说法 | 这是早期调查线索，来源为[该文档](https://raw.githubusercontent.com/zackb/tether/main/docs/BLUETOOTH.md)。它是可变的 latest URL，未作为版本固定证据；本轮自己的 BlueZ/内核源码与受控观测**不支持**“移除 connect list 会阻断 iPhone central 连本机广告”的主张。 |

## 本地源码的可复核指纹

安装的 Noctalia 源码自报 5.1.0，并与上表锁定 revision 匹配。两个审阅文件的
SHA-256 如下，供不公开本机路径的复核：

| 文件 | SHA-256 |
| --- | --- |
| `src/dbus/bluetooth/bluetooth_service.cpp` | `71392383c42210df2c9823d2fe1cf0e43959bd00ecc53bee8282d7985f86cdd7` |
| `src/shell/control_center/tabs/bluetooth_tab.cpp` | `2461bf74771b8707ef64acf2beecff18d110c77d5abc228234c2ac88daeac960` |

项目固定 `bluer = 0.17.4`；lockfile 中 crates.io checksum 为
`af68112f5c60196495c8b0eea68349817855f565df5b04b2477916d09fb1a901`。

## 工具与证据来源

归档工具的协议布局对照 [Linux v7.2 HCI 头文件](https://github.com/torvalds/linux/blob/v7.2/include/net/bluetooth/hci.h)、[BlueZ 5.87 HCI 头文件](https://github.com/bluez/bluez/blob/5.87/lib/bluetooth/hci.h)和 [MGMT 协议](https://bluez.readthedocs.io/en/latest/mgmt-api/)。明确区分 ACL/SCO/LE 类型、传统与增强 LE 建链事件、扩展建链命令的地址字段，以及 MGMT Command Complete/Status；离线测试使用这些布局，不调用真实控制器。

本轮的结论来源包括：版本固定的公开 BlueZ/Linux/Noctalia/bluer 源码阅读、
本地源文件 SHA-256 复核，以及 HCI/MGMT 的连接和加密状态。运行期结论仅以
[`../../results/evidence/LE_BEARER_RECOVERY.jsonl`](../../results/evidence/LE_BEARER_RECOVERY.jsonl)
及其配套结果记录为准；桌面/UI 的“已连接”状态没有替代 HCI/MGMT 链路判定。

未在本参考整理过程中运行蓝牙、认证、服务或硬件控制命令；资料抓取只读取公开
源码与说明页面。
