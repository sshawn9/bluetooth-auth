# 无 HID 的手机 GATT 服务保持实验

验证：复用已有 LE 配对，在电脑没有注册 HID、没有发布 LE 广播的情况下，使用 iPhone 的 Current Time Service（CTS）或 Battery Service，能否在手机长时间锁屏时保持同一条加密 LE 连接。

本轮不验证首次配对。已有连接直接复用；没有连接时，只调用一次 `org.bluez.Bearer.LE1.Connect`，不回退通用 Connect 或 HID，也不更改 PreferredBearer、Trusted 和配对。主动连接需要 BlueZ 提供该 experimental 方法。

此前 CTS/ANCS 实验卡在“电脑广播、等待手机连回”，并未进入手机服务使用阶段；旧 CTS 代码只读取一次时间。本脚本测试的是新的连接入口和持续通知订阅，不能把旧实验或离线测试当作本轮成功证据。

## 设计背景与取舍

本节记录截至 2026-09-27 的依据和待验证问题，尚未决定用手机 GATT 服务替换 HID。

### 为什么重新考虑连接后的服务生命周期

用户反馈：iPhone 长时间锁屏后，电脑免密解锁有时失败，点亮手机后连接恢复。当前正式实现的 `query_or_connect` 在确认目标加密 LE 后就释放本轮 HID 服务和广播，只留下底层连接。服务注销可能影响 iOS 后续维持连接的行为，但目前没有证据证明它就是本次问题的原因。

[HID 退出实验](HID_RELEASE.md)中的 BZ-HID-05、BZ-HID-07 已证明：撤销 HID 和广播后，同一加密 LE 分别保持了 60.1 秒和 60.0 秒。这只能证明注销服务不必然立即断线，不能证明手机长时间锁屏后也稳定，更不能据此排除常驻 HID 的价值。旧实验还保留了连接后 5 秒基线，不等同于正式实现建连后立即释放的全部行为。

### 两个方向的电量服务

HID 是 Human Interface Device（人机接口设备），通过 BLE GATT 承载 HID 的规范称为 HOGP。HOGP 配件提供 HID、Battery、Device Information 服务；这里的 Battery 是配件自己的电量，不是要求配件订阅手机电量或 CTS（Current Time Service，当前时间服务）。参见 [Nordic 的 HOGP 实现说明](https://nrfconnectdocs.nordicsemi.com/ncs/latest/nrf/applications/nrf_desktop/bluetooth.html#bluetooth-peripheral)。

| 路径                   | 服务提供方 | 服务使用方 | 当前行为                                                                                                                              |
| ---------------------- | ---------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| 现有 HID 配件电量      | 电脑       | iPhone     | `register_hid` 提供 Battery Service `0x180F`、Battery Level `0x2A19`，固定返回 100%；声明通知能力，但没有更新电量或发送电量通知的逻辑 |
| 本实验的手机电量或时间 | iPhone     | 电脑       | 对所选服务读取一次，再保持通知订阅；不周期性读取，不向手机发送虚构的电量或输入事件                                                    |

现有电脑端电量服务与 HID 同属一个 GATT 应用，连接成功后随应用一起释放，并未长期提供。代码见 [`register_hid` 与 `query_or_connect`](../../../src/lib.rs)。因此，现有失败现象不能证明“持续提供 HID 和配件电量服务无助于保持连接”。提供电量服务也不等于持续主动发送电量。

### Apple 文档明确支持哪条路径

[Apple《Accessory Design Guidelines》](https://developer.apple.com/accessories/Accessory-Design-Guidelines.pdf)（核对版本：2026-09-21）有两处不同说明：

- **§58.12.4，第 355 页**：iPhone 向配件提供电量、当前时间和 Apple 通知中心服务；已配对且使用其中一个服务时，手机会维持与配件的连接。这里不限于 HID 配件，也没有要求同时使用电量和时间。原文说的是“使用服务”，没有明确把一次 `StartNotify` 定义成永久连接保证。
- **§34，第 250 页**：介绍配件向手机报告自身电量及电源状态的规则，没有承诺仅靠提供或周期性发送配件电量就能保持连接。不能把上一条关于使用手机服务的说明反向套用到这里。

本实验用“一次读取＋持续订阅”验证前一种使用方式能否满足实际需求。仅有文档依据或短时订阅成功，还不能代替手机长时间锁屏测试。

### 常驻服务、广播和访问限制的区别

本地 HID 注册在电脑适配器的 GATT 服务数据库中，不是目标手机独享的一份服务。当前 `register_hid(Some(target))` 对特征读写检查目标身份，这属于访问限制，不能隐藏服务及其 UUID。

- 持续广播 HID 时，附近扫描设备可能直接发现电脑的 HID 能力。
- 目标手机连接后可以停止 HID 广播，继续保留 GATT 服务，让手机通过已有连接访问它。**常驻 HID 不要求一直广播 HID。**
- 停止这条广播会减少通过它被发现的机会，但不会让 HID 服务变成手机私有；其他能够连接并发现服务的设备仍可能看到它。对方如何显示电脑类型取决于其系统，不能保证完全不受影响。

服务注册与广播分别由 [BlueZ GATT API](https://bluez.readthedocs.io/en/latest/gatt-api/) 和 [Advertising API](https://bluez.readthedocs.io/en/latest/advertising-api/) 管理。保留本项目注册的 GATT 服务，需要提供进程及其 D-Bus 连接继续存活；保留手机通知订阅同样需要客户端继续运行。

| 方案                   | 连接建立后保留什么                                       | 对外 HID 影响                                        | 当前依据与代价                                                           |
| ---------------------- | -------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------ |
| 现有临时 HID           | 只保留底层加密 LE，释放服务和广播                        | 不再由本轮应用提供 HID；其他设备的历史缓存不由此清除 | 已有 60 秒短测，长时间锁屏表现仍有问题待查；不需要本项目的 HID 常驻进程  |
| 常驻 HID，连接后停广播 | 保留 HID、电量等服务，停止本轮广播                       | 不持续广播 HID，但服务仍可能被其他连接设备发现       | 需要常驻提供进程；是否改善长期保持尚无对照结果                           |
| 使用手机 CTS 或电量    | 保留电脑对手机服务的订阅，不注册本地 HID、不发布本轮广播 | 本方案不向电脑添加 HID 能力                          | Apple 有对应连接保持说明；需要常驻订阅客户端，长期保持及黑屏重连仍待验证 |

### 已完成的手机服务短测

以下依据用户于 2026-09-27 提供的终端输出整理。两轮均为 `connection_setup=existing`，本机无 HID、广播实例为 0；复用已有已配对、已保存且加密的 LE 连接。

| 服务    | 实际访问                   | 观察结果                                             | 结论范围                             |
| ------- | -------------------------- | ---------------------------------------------------- | ------------------------------------ |
| CTS     | 读取 10 字节成功，订阅成功 | 同一加密 LE 保持 15 秒，`passed`，特征值更新次数为 0 | 手机时间服务可实际使用，短时订阅通过 |
| Battery | 读取 1 字节成功，订阅成功  | 同一加密 LE 保持 15 秒，`passed`，特征值更新次数为 0 | 手机电量服务可实际使用，短时订阅通过 |

这两轮没有证明订阅改善了连接保持，也没有证明无 HID 主动建链、长时间锁屏后的快速重连或不依赖 HID 的首次配对。更新次数为 0 不影响读取、订阅和本轮连接保持结果。

### 作出选择前需要的对照

- **确认服务注销是否有关**：比较“连接后释放 HID”和“连接后保留同一 HID 应用、只停止广播”。保留 HID 的对照轮不额外订阅手机 CTS/电量，不发送 HID 输入或人工保活数据，避免同时改变多个因素。保持手机锁屏时长、距离、充电状态及电脑不挂起等条件一致，记录断线与加密状态，重复观察。
- **确认手机服务的实际收益**：比较 `none`、`cts`、`battery` 三轮，覆盖此前容易失败的长时间锁屏场景。`none` 只表示本脚本不额外使用服务，仍需留意系统或其他程序的 GATT 使用。若各轮都不断线，只能记录这些条件下均通过，不能归因于订阅。
- **区分保持与重连**：即使保持测试通过，仍需单独验证手机已经长时间黑屏时的主动 LE 建链，以及电脑重启后的恢复时延；持续保持的成功不能代替这些结果。

`gatt_keepalive_test.py` 会拒绝本机存在 HID 的实验环境，不能直接用于常驻 HID 对照。上面的 HID 对照是待补充的实验，不是现有脚本已支持或已经通过的项目。

若常驻 HID 明显更稳定，可以考虑保留 HID、连接后停广播，但须接受服务可能被其他设备发现；若手机服务方案稳定且恢复时延也满足要求，则可以在日常保持连接期间不提供 HID。首次登记是否仍需 HID 另行验证，不在本轮短测结论内。

## 准备

使用已有的 `experiments/iphone_ble/.venv`，不需要新增依赖。把 `BLUETOOTH_AUTH_ADDRESS_FILE` 设置为现有手机身份地址文件的绝对路径，地址内容只在本机读取。

先结束其他 HID 配对/实验程序。部署了本项目的电脑还需暂停以下服务，避免自动锁屏干扰观察、自动连接偷偷注册 HID。只在测试结束后恢复原本启用的服务：

```sh
systemctl --user stop bluetooth-auth-auto-lock.service
sudo systemctl stop bluetooth-auth-connect.socket bluetooth-auth-connect.service bluetooth-auth-power-monitor.service
```

保持 `bluetooth.service` 运行，保留已有 LE 配对。手机锁屏并放在电脑附近。无需先断开现有连接；脚本会明确记录起点是 `existing` 还是 `active`。

脚本在存在 `/run/bluetooth-auth/hciN.lock` 时持有项目连接锁，退出时自动释放。仍会检查本机 HID、广播和目标经典连接；发现这些干扰则结束为 `inconclusive`。

## 单独测试一个服务

默认只测试 CTS，先读一次确认可访问，再调用 `StartNotify` 持续订阅。以下命令观察一小时，终端每分钟显示一次进度：

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/gatt_keepalive_test.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE" \
  --adapter hci0 --service cts \
  --wait-seconds 30 --observe-seconds 3600
```

保持其他条件相同，下一轮将 `--service cts` 改为 `--service battery` 测手机电量服务；改为 `--service none` 则是脚本不额外读取、订阅服务的对照轮。`none` 不会关闭 BlueZ 自身或其他程序对手机服务的使用，不能称为“系统完全不使用 GATT”。每轮分别保存结果，不同时测试两个服务。

`--wait-seconds` 分别限制连接、等待服务发现两个阶段；ReadValue/StartNotify 等 D-Bus 调用另有 5 秒上限。`--observe-seconds` 最长支持 86400 秒，Ctrl+C 可提前结束。观察期间只读取本机 BlueZ/HCI 状态，不定时读取手机特征，也不发送 HID 输入或人工保活数据。

如需保存输出，在命令末尾加 `| tee /tmp/gatt-cts.jsonl`。日志包含时间、阶段、连接状态和特征值变化次数，不包含地址、手机名称、密钥、实际时间值或电量值。`value_changed` 表示 BlueZ 特征缓存变化，不将其次数解释为手机发送通知的精确次数。

## 如何判断

- `initial_state.connection_setup=existing`：只测已有连接的使用和保持，不证明无 HID 主动建链成功。
- `initial_state.connection_setup=active`：本轮会请求一次 LE 连接。是否最终连接并加密，以后续 `encrypted_link` 为准；接口调用返回不等于连接已满足要求。
- `read_complete`、`subscribed`：所选手机服务已实际读取、订阅。只在 D-Bus 里发现 UUID 不算通过；没有通知产生也不自动判失败，电量/时间可能暂时没有需要通知的变化。
- `result.verdict=passed`：完成规定观察时间，同一加密 LE 保持，所选服务订阅仍有效，期间未发现 HID/广播干扰。只证明本轮条件和时长，不证明首次配对、所有重连场景或永久稳定。
- `failed`：建立基线后发生断线、加密失效、句柄变化或订阅丢失。即使系统立即以相同句柄重连，也通过 MGMT 断线事件识别，不能算持续保持。
- `inconclusive`：连接/服务阶段超时、权限或接口错误、其他程序干扰等，无法完成本项验证。查看 `phase`、`reason`；不会换另一个服务或 HID 重试。
- `cancelled`：手动提前结束，不计作完整观察通过。退出码分别为 0、1、2、130。

记录手机从何时开始锁屏，是否充电、是否走远，以及亮屏时间。若失败，先保持手机黑屏，保留完整日志，避免亮屏改变重连条件。

脚本不主动断开目标连接，结束时释放本轮通知订阅、D-Bus/MGMT 接口和文件锁。Connect 客户端超时不保证 BlueZ 已取消底层连接任务；测试结束后的迟到连接不属于本轮成功。

## 恢复正常服务

确认测试进程已退出，恢复先前运行的服务：

```sh
sudo systemctl start bluetooth-auth-power-monitor.service bluetooth-auth-connect.socket
systemctl --user start bluetooth-auth-auto-lock.service
```

如测试后仍未连接，原本使用本项目自动连接的环境可再执行一次 `sudo systemctl start bluetooth-auth-connect.service`；这是实验结束后的正常 HID 恢复，不计入本轮测试。
