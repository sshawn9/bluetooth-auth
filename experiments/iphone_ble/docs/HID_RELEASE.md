# HID 退出与重启后连接实验

**已验证的短测路径是：临时提供 HID 和广播，先建立加密 LE 连接，再撤销服务并退出提供进程；原连接仍可保持。** 因此，不能以“维持已建立连接”为理由认定本项目 HID 程序必须常驻。系统 `bluetoothd` 始终继续管理底层蓝牙链路。

## 三轮测试与结论

| 记录 | 起点与操作 | 电脑端 Trusted | 实际结果 | 能支持的结论 |
| --- | --- | --- | --- | --- |
| [BZ-HID-05](../results/evidence/BZ-HID-05.json) | 已有加密 LE；提供 HID 后再退出 | false | HID/广播撤销后，同一加密连接保持 60.1 秒，passed | 已建立连接的短时保持不要求 HID 提供程序常驻；本轮没有新建连接 |
| [BZ-HID-06](../results/evidence/BZ-HID-06.json) | 按重启后步骤，仅运行只读观察器，无 HID/广播 | false | 20 秒内没有目标 LE，inconclusive；未开始保持观察 | 本轮单纯等待没有恢复连接，不能据此推导必须常驻 HID |
| [BZ-HID-07](../results/evidence/BZ-HID-07.json) | 无目标 LE；临时提供 HID，建连后退出 | true | automatic；确认资源撤销后，同一加密连接保持 60.0 秒，passed | 按需临时提供 HID、建连后退出的路径通过短测 |

三份文件分别保留用户该轮完整输出的 20、8、19 个事件，属于对话转录，不是直接采集的原始 stdout 文件。重启、手机锁屏及无点击连接是操作条件，脚本不能独立核实。前期失败和修正保留在本文的[运行记录](#运行记录与修正)，不计入成功次数。

## 已验证的操作顺序

1. 注册本轮 HID 服务和可连接广播，保持提供进程运行。
2. 等待目标手机建立加密 LE 连接，注册完成后的等待上限为 20 秒。
3. 连接建立后保持 5 秒基线。
4. 关闭提供进程的 D-Bus 并退出，独立确认 HID 服务和广播已撤销。
5. 观察进程继续检查同一加密 LE 60 秒，随后关闭观察接口；仍在的连接保留。

本轮验证的是**先连接，再退出 HID**。没有验证“先退出 HID 再连接”，也没有验证省略 5 秒基线、建连后立即退出的表现。“一次性提供”指一次按需调用的生命周期；以后需要重新建立连接时，可以再次临时提供服务，不表示设置一次便永久具备 HID 服务。

## 设备身份与 Trusted

`Trusted` 是**电脑对手机的信任标记**；注册 HID 则是**电脑向手机提供服务**。BlueZ 将 Trusted 保存为设备属性，服务注册处理的是适配器的 GATT 应用，两者不是同一项设置。[信任属性实现](https://github.com/bluez/bluez/blob/5.87/src/device.c#L6544)、[服务注册实现](https://github.com/bluez/bluez/blob/5.87/src/gatt-database.c#L3679)。

当前两个入口沿用系统 BlueZ 身份，不创建 Bumble 独立身份、不改适配器地址或 Alias。它们按指定适配器和地址文件查找手机记录，不按 HID 是否存在选择另一台设备。增减 HID 改变提供的服务，不能据此认定手机将电脑识别成另一个身份；iPhone 的显示和连接选择仍需单独观察。

BZ-HID-06 的 Trusted 为 false，BZ-HID-07 的顺序则是 `initial_resources` 无 HID/广播、`target_state` 已为 true，然后才 `preparing_hid`。因此，本次注册 HID 不能解释此前已发生的信任变化。更早由谁、何时修改没有日志，两轮也不是只改变 HID 的对照；不能把恢复原因全部归于 HID。这不影响 BZ-HID-07 本轮建连与退出后保持的成功结果。

## 复现前提

使用[复现手册中的实验环境](REPRODUCE.md#2-环境和目录)，从仓库根目录执行。地址文件仅含目标手机身份地址一行；先将下方路径替换为已有文件的实际位置：

```sh
export BLUETOOTH_AUTH_ADDRESS_FILE=/path/to/private/bluetooth-address
```

前提：地址文件对应所选适配器下的目标手机记录，目标未被系统阻止，能够建立或已具有加密 LE 连接。已有加密连接时直接做保持测试；需要建立连接但只有旧音频配对时，可按下方流程重新配对。手机列表中的“已配对”及电脑的 `Paired/Bonded=true` 均不能证明 LE 已就绪。BlueZ 将经典蓝牙与 LE 的配对状态合并为这些属性，见 [BlueZ 5.87 实现](https://github.com/bluez/bluez/blob/5.87/src/device.c#L1129)。

脚本启动时用 `target_state` 分别显示 `Paired/Bonded/Trusted/Blocked`，缺失值为 `null`。**本轮只测链路保持，Paired/Bonded/Trusted 不作为启动门槛，也不会被脚本改成 true。** 通过仍要求指定地址对应的唯一 LE 链路、实际加密、同一连接句柄及无断线事件；这不是认证授权测试。`Blocked=true` 则明确报告系统阻止状态。实验期间配对或信任属性发生变化时，会列出具体字段和前后值，作为观测受干扰处理。

退出其他 HID 程序、广播实验及会自动连接手机的程序。**目标 LE 已连接时直接运行，不需要先断开或抢时间启动脚本。** 原耳机、鼠标等其他连接可正常保留。

## 只有旧音频配对：先重新配对一次

采用已通过实测的 `bluez_hid_lab.py --repair-phone-pairing`。手机忽略原电脑条目后，原手机侧配对失效；新配对会保留，原配对密钥不回滚。电脑保留原手机记录，供修复入口确认目标身份与信任设置。

1. 保持电脑连着至少一台原有蓝牙鼠标、耳机或音箱；现有共存修复入口要求这项前提。
2. iPhone“设置 → 蓝牙 → 原电脑条目旁的 ⓘ → 忽略此设备”，然后停留在蓝牙设置页。
3. 在仓库根目录执行下面的配对命令。地址文件沿用此前的配置。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/bluez_hid_lab.py run \
  --adapter hci0 --phone-file "$BLUETOOTH_AUTH_ADDRESS_FILE" \
  --repair-phone-pairing \
  --wait-seconds 45 --hold-seconds 5
```

在“其他设备”选择电脑原名称，核对手机与终端的六位数字，一致时在手机确认并在终端输入 `y`。看到 `service_ready` 后再等 5 秒，脚本会退出。确认 `result` 中 `passed=true`、`pairing_confirmation_accepted=true`、`pairing_identity_verified=true`，以及 `restore` 中 `settings_restored=true`，再继续注销测试。

配对入口结束时撤销临时 HID、广播和配对代理，恢复 Pairable，并断开该阶段新建的目标 LE；新配对保留。这一步发生在注销观察之前，不是注销后保持的测试结果。若配对未通过或恢复未完成，先处理该结果，不重复运行配对命令或接着运行注销测试。

## 已完成 HID 配对：运行注销测试

手机锁屏并放在电脑附近，不再点击连接。后续运行不带 `--repair-phone-pairing` 或 `--manual-connect`。

沿用上方地址文件配置，在仓库根目录执行：

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/hid_release_test.py \
  --phone-file "$BLUETOOTH_AUTH_ADDRESS_FILE" \
  --adapter hci0 --wait-seconds 20 --observe-seconds 60
```

整个过程只需这一条测试命令，手机保持锁屏，不点击连接。先用最多 10 秒确认 HID 服务和广播注册完成，再用最多 20 秒确认目标加密 LE 已就绪；已有连接满足条件就立即进入 5 秒基线，不会等满 20 秒。随后退出提供进程并观察 60 秒，另有短暂的资源撤销确认时间。若连接提前丢失，立即结束。

启动时已有目标 LE 会出现 `existing_connection`，并在后续日志及结果中标明 `connection_setup=existing`。本轮先在已有链路上注册 HID/广播，再注销它们，观察同一加密 LE 链路是否保留；这个结果不能证明本轮 HID 广播触发了连接。手机原有连接无需为了本测试重新配对。

最先输出的 `initial_resources` 是注册任何本轮服务之前的实际读取。如果它显示 `hid_uuid_present=false`、`advertising_instances=0`，随后 `existing_connection` 显示 `encrypted=true`，则当时已存在“没有本地 HID 服务和 BlueZ 注册广播，目标加密 LE 仍在”的证据。手机保存的 HID 配对或名称缓存不代表电脑当前仍有 HID 服务。

使用此前共存实验的同一 HID 服务、消费控制 Report Map 和广播定义，复用电脑的原 BlueZ 身份及名称。`advertising` 事件会列出 HID UUID、Appearance、广播间隔及本机注册状态；这表示 BlueZ 已接受注册，不代表手机已连接。随后 `connection_state` 每 5 秒分别报告目标 LE、内核连接和加密状态。

本轮观察对象是底层加密 LE 链路，**不要求手机每次重新读取 HID Report Map**。`encrypted_hid_read_seen` 仅是辅助记录，不能把它为假直接解释为未连接。进入观察前仍须确认 HID/广播注册成功、目标配对身份的 LE 连接存在，且内核能唯一对应其句柄并确认加密。

## 电脑重启后：不启动 HID，只观察连接

重启会结束原链路。这一项验证的是**电脑重启后，没有重新运行 HID 程序时，目标加密 LE 能否恢复并保持 60 秒**，不能用会重新注册 HID 的 `hid_release_test.py` 来代替。

1. 保留现有配对，只重启电脑。手机保持开机、锁屏并放在附近，不点击蓝牙连接，也不同时重启手机。
2. 重启后保留正常的系统蓝牙服务，不启动 HID 实验程序或自动连接手机的程序。若相关程序会随开机启动，需要先处理其自启动，否则不能把恢复归因于系统自身。
3. 在仓库根目录重新设置原地址文件路径，再执行下方命令。已有连接直接观察，无需先断开。

```sh
export BLUETOOTH_AUTH_ADDRESS_FILE=/path/to/private/bluetooth-address
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/observe_le_link.py \
  --phone-file "$BLUETOOTH_AUTH_ADDRESS_FILE" \
  --adapter hci0 \
  --wait-seconds 20 \
  --observe-seconds 60
```

`observe_le_link.py` 只读取 BlueZ、MGMT 和 HCI 状态：不注册 HID、不广播、不扫描、不发起连接、不改设置和配对。启动及观察期间均检查没有本地 HID UUID、BlueZ 广播实例为 0，并核对精确目标的 LE、加密和连续性。无法读取这些条件或存在其他 HID/广播时，返回 `inconclusive`，不自行清理或接管。

已有加密 LE 时约观察 60 秒；否则先等待最多 20 秒，成功后再观察 60 秒。等待失败不会重试或自动运行 HID。`passed` 表示本轮观察中同一目标加密 LE 保持，`failed` 表示链路丢失或加密失效，`inconclusive` 表示未建立观察前提或无法可靠读取状态。脚本不能独立核实此前的重启、手机操作或开机过程中是否运行过其他程序，归档时需由操作者注明这些条件。

结束只关闭观察接口，仍存在的连接继续保留。BZ-HID-06 的实际输出为：无 HID、广播实例为 0；电脑端 `paired=true`、`bonded=true`、`trusted=false`，四次状态均无目标 LE。20 秒后 `verdict=inconclusive`、`phase=connect`，没有进入 60 秒保持阶段，观察接口正常关闭。

这项结果表示本轮等待期间没有恢复连接，不表示必须常驻 HID，也不能把 `Trusted=false` 直接认定为原因。BZ-HID-05 已证明既有连接可以在 HID 撤销后短时保持；这两项分别对应建立连接与保持连接。

## 重启后：临时启动 HID，连接后退出

沿用当前重启后的状态和配对，手机保持锁屏并放在附近。在仓库根目录运行下方命令，地址文件沿用前面的配置：

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/hid_release_test.py \
  --phone-file "$BLUETOOTH_AUTH_ADDRESS_FILE" \
  --adapter hci0 \
  --wait-seconds 20 \
  --observe-seconds 60
```

脚本临时注册原 BlueZ 身份下的 HID 服务和广播；注册完成后最多等待 20 秒确认加密 LE，连接后保持 5 秒，退出 HID 提供进程并确认服务/广播撤销，再观察 60 秒。最后自动结束；仍在的连接继续保留。

本轮应关注 `connection_setup`：`automatic` 且最终 `passed` 才支持这次从未连接状态经临时 HID 恢复、退出后继续保持；如果变为 `existing`，结果只算已有链路保持。手机锁屏、无点击连接的条件仍由操作者记录。超时或失败按原结果结束，不重试、不自动进入重新配对或手动连接流程。

BZ-HID-07 已完成这条路径：注册后的初始快照没有目标 LE，随后 `connection_setup=automatic`，唯一 LE 已加密。提供进程以 0 退出、D-Bus 关闭，确认 HID 和广播撤销后，同一连接保持 60.0 秒，最终 `passed`。本轮没有观察到重新读取 HID（`encrypted_hid_read_seen=false`），不能计作新的 HID 读取或订阅成功；它不影响本项底层加密链路保持的判据。

复现时保留两轮 Trusted 的实际值及变化来源，按前文[设备身份与 Trusted](#设备身份与-trusted)解释，不能将这两轮当作只改变 HID 的对照。

这些短测支持按需流程：实时核对目标加密 LE；未连接时临时提供 HID，等待一次有界结果，然后撤销本次服务和广播并退出，保留仍在的连接。它们不要求凭借“保持连接”这一理由让 HID 提供程序常驻，也不证明长期保持或所有断线恢复场景。正式认证代码尚未按这一结论调整。

## 手动建立起始连接，单独测试注销后的保持

当前自动连回未复现时，可以明确选择本方式建立观察起点。它只检验服务退出后原连接能否保留，**不用于证明自动重连**，也不会在默认模式超时后自动回退到此方式。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/hid_release_test.py \
  --phone-file "$BLUETOOTH_AUTH_ADDRESS_FILE" \
  --adapter hci0 --manual-connect \
  --wait-seconds 30 --observe-seconds 60
```

1. 保留已有配对，手机打开“设置 → 蓝牙”，运行命令。
2. 出现 `waiting_for_connection` 后，在“我的设备”点击电脑原配对条目建立连接。
3. 连接后立即锁屏。脚本核对 5 秒基线，再退出 HID 提供进程，自动观察 60 秒。

日志的 `connection_setup=manual` 表示选择了手动起步条件，由用户按步骤确认实际操作；脚本不能自行判断手机上的点击或锁屏动作。若启动时已连接，则直接复用并标为 `existing`，无需再点击。`connected` 仍须满足与默认模式相同的身份和加密检查；没有连接就超时退出，不重新配对。

## 观察与结果

程序使用两个进程：子进程复用已实测的共存 HID 服务定义；父进程独立读取 BlueZ 和内核连接信息。父进程在首次连接快照之前开始记录 MGMT 断线事件，跟踪目标 LE 句柄；基线与注销后的观察都要求持续加密。已有目标 LE 的断线单独判定，其他原连接及目标手机的原 BR/EDR 连接继续受保护。子进程关闭自己的 D-Bus 并等待关闭完成后退出，不在已关闭的连接上再调用 `unexport()`。父进程确认退出码为 0、关闭确认已收到，再重新读取并确认总线身份消失、HID UUID 移除、广播实例归零，才开始 60 秒观察。

`provider_exit` 记录真实退出码和子进程的 D-Bus 关闭确认；`provider_exited` 才表示父进程已经确认服务和广播撤销。退出错误使用 `provider_error` 输出阶段、异常类型、错误号、标准 D-Bus 错误名及出错函数/行号。进入退出阶段即丢弃之前的状态快照，避免旧的“已连接”数据被误当成注销后的证据。

查看最后的 `result`：

| `verdict` | 含义 |
| --- | --- |
| `passed` | 提供进程退出且资源已撤销，原句柄对应的加密 LE 连接在本轮观察中保持；没有发现目标断线事件。只属于短测证据。 |
| `failed` | 注销后原连接消失、句柄变化、加密失效，或收到目标 LE 断线事件；重连不能抵消这个结果。 |
| `inconclusive` | 初始连接未建立、前提不满足、身份无法对应、观察接口失败、资源撤销无法确认，或其他设置/原连接发生变化。本轮不能回答问题。 |

此脚本不需要新 daemon 的 D-Bus 服务名权限，直接使用实验已有的 BlueZ 注册接口。它会读取 `src/bluetooth_auth_hid/link.py`，用于核对当前 LE 句柄及加密状态。

正常结束或 Ctrl+C 都只关闭本轮进程、D-Bus 和观察接口。**不调用蓝牙 Disconnect、不切换电源或 Pairable、不删除配对。** 如果目标连接仍在，会继续保留；不要执行旧共存脚本的 `restore` 来结束本轮，因为旧恢复流程包含主动断开新增 LE 的动作。

运行日志只输出事件、结果和原因，不输出真实地址、电脑名称或密钥。后续归档应保留完整输出和手机锁屏条件，不能把脚本编写或离线检查算作实机结果。

## 运行记录与修正

首次用户输出为 `waiting_for_connection` 后 20 秒超时，`result` 为 `inconclusive`、`phase=connect`，结束时没有主动断开连接。旧版没有输出子进程注册结果及各项连接状态，还将重新读取 HID 作为进入条件，因此该日志无法区分未连接、未加密、身份未对应或没有重新读取 HID；不能据此确认本次根因，也不能推导注销后的连接表现。

修正版分别记录注册与连接状态，注册完成后才开始连接计时，并移除重新读取 HID 的额外门槛。子进程错误会输出阶段、错误类型和标准 D-Bus 错误名，不输出可能含地址的异常正文。

第二轮用户输出确认 `gatt_registered=true`、`advertisement_registered=true`、`hid_uuid_present=true`、`advertising_instances=1`；约 20 秒内的各次记录均为 `bluez_connected=false`、`phone_le_connected=false`、`hci_le_links=0`。结果仍为 `inconclusive`、`phase=connect`，没有执行注销后的观察，也没有主动断开连接。本轮阻塞点是未观察到目标连接，不能归因于没有重新读取 HID。具体原因尚未确定，不能推翻此前自动连回成功的记录，也不能将注册成功等同于手机已收到广播。

第三轮采用 `--manual-connect --wait-seconds 30 --observe-seconds 60`；各次记录仍无目标 LE，最终 `inconclusive`、`phase=connect`，未进入注销观察。用户随后确认手机保存的是旧版音频配对。此前脚本只检查通用 `Paired/Bonded/Trusted`，没有确认 LE 配对可用；这是前提检查不足，但尚不能把“缺 LE 密钥”写成已核实的唯一根因。

随后给出重新配对与注销测试的两步命令。用户提供的下一次注销脚本输出为 `inconclusive`、`phase=prepare`，原因是已存在目标 LE；这条日志确认了该次检测到目标 LE，但没有给出其加密状态或持续时长，也不能补造未提供的配对阶段结果。

用户进一步反馈，关开蓝牙后会自动连接，单独断开后也很快连回，无法制造“断开后立即启动”的操作窗口。随后修正为自动复用已有目标 LE，标为 `connection_setup=existing`；已有连接在注册准备期间若断线或更换已记录的句柄，本轮不判通过，注销后断线则判失败。当时仍未取得注销后保持的实机结果。

复用修正后的下一次用户运行在 `prepare` 阶段被旧的 Paired/Bonded/Trusted 合并检查拒绝，尚未启动 HID。该日志没有记录具体字段值，不能声称已经确定是 Trusted 为假或配对丢失。随后移除这三个与本轮链路保持判定无关的启动条件，打印实际属性；精确目标、未被阻止、实际加密及连续性检查保留。

下一轮实机日志给出了 `Paired=true`、`Bonded=true`、`Trusted=false`、`Blocked=false`，已有一条加密 LE，HID 注册及目标 HID 读取成功，5 秒基线后进入 `release`。子进程非零退出，程序没有进入 `provider_exited/observing`。末尾连接和注册状态来自退出前的旧快照，不能当作注销后连接保持的结果。

对退出代码的离线复现使用同一实验环境的真实 dbus-fast、独立 D-Bus 进程和真实 HID 子进程。旧顺序先关闭 D-Bus，再调用 `unexport()`，复现了 `OSError`、`errno=9`；修复前连续三个子进程在完成 HID 读取后均以退出码 2 结束。修复改为关闭 D-Bus、等待完成并直接退出，同样三个子进程均以退出码 0 结束，私有总线确认其所有者消失。原实机日志没有保存底层异常，不补造其异常堆栈；离线结果证明了这个具体代码缺陷及其修复。

随后 BZ-HID-05 的实机输出完整通过。启动前 `hid_uuid_present=false`、`advertising_instances=0`，已有一条加密 LE；注册 HID 后观察到目标加密读取。提供进程以 0 退出且 D-Bus 关闭，父进程确认 `hid_removed=true`、广播为 0，随后同一加密 LE 连续保持 60.1 秒。最终没有主动断开连接或修改配对。`connection_setup=existing` 明确表示复用既有链路，本轮不计作一次新建连接或重连成功。

这项证据否定了“为了维持这条已建立的连接，HID 提供程序必须常驻”的必要性判断。用户此前提到的断开后很快自动连接另作人工观察保留，不混入这 60.1 秒的连续性结果；也不能据此推导一次设置 HID 后永久有效。

随后按电脑重启后的只读步骤运行，形成 BZ-HID-06。启动时没有 HID/广播，电脑配对记录仍在，但整个 20 秒等待期都没有目标 LE；因此未开始保持观察，结果为 `inconclusive`。这轮保留为无 HID 条件下未观察到恢复的有界结果，不能因没有进入保持阶段而丢弃；后续临时 HID 的恢复单独登记为 BZ-HID-07。

BZ-HID-07 的 19 个事件记录了从无目标 LE 到唯一加密 LE，再到提供进程正常退出、服务和广播撤销、同一连接保持 60 秒的完整过程；没有主动断开连接或修改本轮配对属性。与 BZ-HID-05 的 `existing` 不同，本轮为 `automatic`。两轮间 Trusted 的变化单独保留，不补造变更操作，也不把进度输出间隔写成精确建连耗时。

## 离线验证

常规逻辑回归使用假蓝牙后端，禁止蓝牙和网络连接：

```sh
experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/check_offline.py
```

真实 D-Bus 退出回归需要本机有 `dbus-daemon`，且环境允许绑定临时 Unix socket。该命令只在 `/tmp` 创建自己的总线，只允许连接该 socket；Bluetooth 查询/注册是固定夹具，HID 对象、D-Bus I/O、子进程和 EOF 均为真实实现，不访问系统 BlueZ 或蓝牙硬件：

```sh
experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/tests/check_hid_release_dbus.py
```

该回归连续运行三个 HID 子进程，实际读取其 GATT 对象和 Report Map，再通过 EOF 让进程退出，检查退出码、关闭确认及总线所有者消失。加 `--reproduce-old` 可在本次固定依赖版本中复现旧退出顺序的 `errno=9`；它的通过表示旧故障复现成功，不是蓝牙测试通过。临时总线、子进程和文件由测试自行清理。这些离线结果均不增加真实手机的连接保持成功次数。
