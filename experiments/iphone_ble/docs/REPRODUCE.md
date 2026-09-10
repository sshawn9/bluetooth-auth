# iPhone BLE 实验复现手册

本手册复现 [实验报告](../RESULTS.md) 的短测场景。`run` 会访问真实蓝牙，独占模式会中断适配器上的原连接；执行前按下表选择场景，并了解对应恢复方法。

## 1. 先选择场景

| 要复现的内容 | 入口与场景 | 必须具备的前提 |
| --- | --- | --- |
| 已通过的系统蓝牙与 HID 共存 | `bluez_hid_lab.py`，BZ-HID-04 | 两端保留有效原配对，电脑有另一个已连接蓝牙设备 |
| 已通过的 HID 注销后连接保持 | `hid_release_test.py`，BZ-HID-05；见 [专用步骤](HID_RELEASE.md) | 已有或可建立目标加密 LE；无需先断开已有连接 |
| 重启后不启动 HID 的连接观察 | `observe_le_link.py`，BZ-HID-06，20 秒未观察到连接；见 [专用步骤](HID_RELEASE.md#电脑重启后不启动-hid只观察连接) | 重启电脑、保留配对，不运行 HID 或其他主动连接程序 |
| 重启后临时 HID 恢复、退出后保持 | `hid_release_test.py`，BZ-HID-07；见 [复现命令](HID_RELEASE.md#重启后临时启动-hid连接后退出) | 沿用重启后的配对，记录目标属性；本次 Trusted=true，手机锁屏，保留完整建立与退出日志 |
| 手机已忘记电脑、电脑仍留配对记录 | `bluez_hid_lab.py --repair-phone-pairing`，BZ-HID-02 | 电脑有目标手机的 Trusted、未 Blocked 记录；手机允许首次操作 |
| 比较独立 ANCS/HID/CTS 身份 | `ble_lab.py`，EX 系列 | 接受临时独占适配器、原连接中断；使用实验自己的配对 |
| 看结论或验证代码 | `plan`、`check_offline.py`、JSON 记录 | 无需开启或接触蓝牙 |

已有结果不要求重跑。ANCS/CTS 命令保留供未来复核当前配置的失败阶段，不作为本轮继续试错的待办。

## 2. 环境和目录

复现基准是 Linux、Python 3.13、BlueZ 5.87、可用的系统 D-Bus，以及现有物理蓝牙适配器。整理时实验 Python 为 3.13.14；[requirements.txt](../requirements.txt) 固定本环境的 18 个依赖版本。iPhone 不安装应用，使用“设置 → 蓝牙”。需要 `uv` 创建独立 Python 环境。

以下命令在仓库根目录（包含 `pyproject.toml` 的目录）的终端执行，全部使用相对路径。

换一台电脑时，把目录、控制器编号和手机地址换成该环境的实际值；`hci0` 是本次实测编号，不是设备名称。未记录的控制器型号、iOS 版本和内核版本无法据本归档还原，复现者应补填 [运行记录模板](RUN_RECORD_TEMPLATE.md)。

`ble_lab.py` 与 `bluez_hid_lab.py` 的 `--state-dir` 都是全局参数，必须放在 `run`、`report` 或 `restore` 等子命令之前；建立状态后，后续始终使用同一路径，不移动已有身份或恢复记录。下文使用默认 `.runtime` 和 `.coexist`。`hid_release_test.py` 与 `observe_le_link.py` 没有这些子命令或状态目录参数，按 [HID_RELEASE.md](HID_RELEASE.md) 直接运行。

### 2.1 准备独立依赖环境

前提：`python3` 是预期的 Python 3.13，且已安装 `uv`；这些命令不访问蓝牙。

```sh
python3 --version
uv --version
python3 -B experiments/iphone_ble/ble_lab.py plan
python3 -B experiments/iphone_ble/ble_lab.py setup
experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/bluez_hid_lab.py plan
```

`setup` 只安装到 `experiments/iphone_ble/.venv`，不使用项目根 `.venv`，也不运行项目依赖同步。已有受管理实验环境可复用；若存在未登记的 `.venv`，脚本会拒绝覆盖，不应直接删除未知目录。

本目录代码已为缺少 `socket.AF_BLUETOOTH` 的 Python 提供 Linux 数字常量及 `ctypes` 绑定。无需为此修改 Python、BlueZ 或安装另一只适配器。

### 2.2 离线校验

前提：依赖环境已建立；此入口运行模拟测试，不使用真实手机、控制器或系统 D-Bus。

```sh
experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/check_offline.py
```

校验器在临时目录启动测试，拦截 Python 蓝牙 socket 创建及真实 socket 连接，测试使用临时文件、fake BlueZ 和 Bumble 内存控制器；`-B` 避免生成字节码缓存。失败退出码会传回调用方。它是离线回归防护，不是操作系统沙箱，也不是实机验证。RSSI 地址配置测试使用本实验目录内的 `diagnostics/monitor_rssi.py`。

### 2.3 RSSI 辅助诊断

[diagnostics/monitor_rssi.py](../diagnostics/monitor_rssi.py) 保留早期 BR/EDR RSSI 诊断实现，会启动设备发现并读取已有连接的 RSSI，不建立新连接或配对；它的读数不属于本报告的 HID 共存通过依据。

前提：地址文件含目标蓝牙地址一行，把示例路径换成实际文件位置；此命令会访问真实蓝牙。

```sh
export BLUETOOTH_AUTH_ADDRESS_FILE=/path/to/bluetooth-address
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/diagnostics/monitor_rssi.py \
  --address-file "$BLUETOOTH_AUTH_ADDRESS_FILE"
```

`--address-file` 优先于环境变量，脚本不自动加载 `.env`。按一次 Ctrl+C 结束，脚本释放自己的扫描会话。

## 3. BlueZ HID 共存复现

这条路径由 BlueZ 继续管理适配器，不关闭原有蓝牙连接，不停系统蓝牙服务。手机看到的是原电脑名称，本次为 `<COMPUTER_NAME>`；旧 `BT-Auth-HID` 属于独占实验，不能替代这个配对。

### 3.1 一次性核对前提

- 在所选适配器上保持至少一个已有鼠标、耳机或音箱连接。无其他连接时，脚本在注册广播之前退出。
- 适配器稳定开启，提供 GATT 和 LE 广播管理接口，尚无另一份本地 HID 服务；已有计时中的配对窗口会被拒绝，避免无法恢复剩余时间。
- 电脑的 BlueZ 中存在目标手机记录，`Trusted=true`、`Blocked=false`；默认模式还要求 Paired/Bonded。手机是否保留配对必须由操作者确认，不能只看电脑的 Paired 属性。
- 原有系统蓝牙和认证服务维持原状态。这些实验入口不会替你暂停或启动外部认证服务。

真实手机地址保存在仓库外的私有文件中，文件只含地址一行；源码不内置地址或个人文件路径。先设置文件位置（替换下列示例路径）：

```sh
export BLUETOOTH_AUTH_ADDRESS_FILE=/path/to/private/bluetooth-address
```

`--phone-file PATH` 优先于这个环境变量。下文把文件参数明确传给脚本，因此不依赖 sudo 保留环境；单独依靠环境变量时，只保留这一个变量即可。`--phone` 兼容已有调用，但推荐文件参数以免真实地址进入命令历史；不要复制归档中的 `<PHONE_IDENTITY>` 占位符。

**全新环境的限制：**当前共存脚本不承担“电脑上完全没有手机记录”的首次建档。应先通过原有系统蓝牙管理功能建立电脑与目标手机的配对并确认信任状态，再复现共存；这会建立持久系统配对，不由实验 `restore` 删除。若该前提无法建立，先记录为环境未就绪，不能把预检拒绝当成 HID 不兼容。

### 3.2 手机已忘记原配对：修复一次

前提：手机已忽略原电脑配对，但电脑仍保留目标手机的 Trusted、未 Blocked 记录；本轮允许操作手机，成功后的新配对会保留。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/bluez_hid_lab.py run \
  --adapter hci0 --phone-file "$BLUETOOTH_AUTH_ADDRESS_FILE" \
  --repair-phone-pairing --trace-advertising \
  --wait-seconds 45 --hold-seconds 30
```

1. 在 iPhone“设置 → 蓝牙”选择电脑原名称 `<COMPUTER_NAME>`。
2. 核对手机和终端的六位数字；匹配时在手机确认，终端输入 `y`。这一步将当前候选与实际手机绑定，不能按一个未知随机地址自动接受。
3. 若出现 `service_ready`，使用原鼠标/耳机检查共存；就绪后保持 30 秒，随后自动恢复临时设置。
4. 若始终不可见或没有连接，等待期最多 45 秒后停止并恢复，记录 `advertising_trace`、`result`、`restore`。不在同一条件下反复运行。

**完成配对后的下一次运行去掉 `--repair-phone-pairing`。** BZ-HID-03 就是已配对后仍带此参数：脚本等待新的数字确认，最终报告证据不足；它不是默认模式的 HID 失败证据。

当前修复模式也不会删除旧 BlueZ 记录来强行解决配对错误。若身份不能收敛、配对被拒绝或没有数字确认，保留实际输出，不能把旧 `Paired=true` 写成新的配对成功。

### 3.3 已有配对：复现完整通过场景 BZ-HID-04

前提：两端已保存有效配对，保持原鼠标/耳机连接，手机锁屏且本轮不操作手机。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/bluez_hid_lab.py run \
  --adapter hci0 --phone-file "$BLUETOOTH_AUTH_ADDRESS_FILE" \
  --wait-seconds 20 \
  --hold-seconds 15
```

1. 运行一次，等待手机连回；本轮不要在手机点击 `<COMPUTER_NAME>`。日志中旧的通用提示“在 iPhone 选择 <COMPUTER_NAME>”不是本场景操作步骤；自动恢复测试以本段“不操作手机”为准。
2. 出现目标 `target_gatt_access` 和 `service_ready` 后，继续使用原鼠标/耳机 15 秒，记录卡顿、断开和实际音频输出。
3. 正常完成时出现 `hold_complete`、`restore`、`result`。没有就绪则约 20 秒等待期后退出；已就绪则另计 15 秒保持，清理还需短暂时间。
4. 完整保存这一轮的命令与输出，注明手机条件和人工观察。不要把两次运行拼成一次。

判定时同时查看：

| 字段/事件 | 通过所需或含义 |
| --- | --- |
| `target_gatt_access` | `device=target_phone`、`link=LE`、`attribute=report_map` 或 `report` |
| `result.passed`、`encrypted_phone_hid_access`、`hold` | 都为 true |
| `original_links_lost` | 空列表；快照之外的短暂中断也会判失败 |
| `restore.settings_restored`、`connections_restored`、`original_connections_present` | 都为 true |
| `advertising_instances`、`test_hid_service_removed` | 无其他原有广播时应为 0；测试 HID 已移除 |
| `phone_hid_subscription_verified=false` | 正常的证据边界；全局订阅不能归属到某一手机 |
| `phone_audio_isolation_verified=false` | 没有声称全部音频服务被隔离，仍需人工反馈 |

单有 `phone_le_connected=true` 不能当作加密 HID 使用通过。如果脚本启动前手机 LE 就已连接，则这轮可以观察保持，但不能单凭它声称完成了一次断开后重连；结合开始状态及恢复中是否断开新增手机 LE 来解释。

### 3.4 共存故障恢复

前提：运行中按一次 Ctrl+C 等待退出；仅在报告恢复失败或仍有遗留恢复记录时执行：

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/bluez_hid_lab.py restore
```

它按原物理地址定位控制器，确认原实验 D-Bus 所有者已退出，断开本轮新增目标/候选 LE 链路，恢复 Pairable 并核对设置和原连接。原 BR/EDR、原目标 LE、其他设备连接不主动断开；新出现的手机经典蓝牙连接或缺失的原连接会报告并保留记录。

无记录时不会访问蓝牙。有记录且恢复失败时，不要删除 `.coexist/restore.json` 绕过检查。修复模式保存的新配对不回滚；`settings_restored=true` 不等于旧密钥已还原。

## 4. Bumble 独占候选比较

这条路径会临时独占现有适配器，原有连接会中断，原电脑 `<COMPUTER_NAME>` 暂时不能使用。适配器名称和原 BlueZ 配对库不改；每种候选使用自己固定的随机静态地址和独立密钥。蓝牙外设是唯一输入设备、或连接断开会触发自动锁屏时，应先准备其他操作方式。

### 4.1 创建实验身份

前提：不与共存脚本同时运行，且没有任何待恢复记录；此命令仅创建私有状态，不访问蓝牙。

```sh
experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/ble_lab.py prepare
```

已有 `.runtime` 时复用其身份，不删除、移动或重建来排查连接问题。如果文件此前由 sudo 创建，用对应权限读取；不要用重建身份代替恢复。三个模式身份独立，切换 ANCS/CTS 不需要先忘记已通过的 HID 配对。

### 4.2 HID 首配：EX-HID-01

前提：该独占 HID 身份尚无已保存配对；本轮允许手机操作。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/ble_lab.py run hid \
  --adapter hci0 --enroll --cycles 0 \
  --hold-seconds 30 --initial-timeout 45
```

在 iPhone 蓝牙设置选择 `BT-Auth-HID`，核对两端数字并确认。预期依次出现 `link`、`encryption`、`bond_saved`、`service_ready`、`hold_complete`，结果通过后自动退出和恢复。已有配对时再次 `--enroll` 会被拒绝。

`--cycles 0` 是“初始连接后不再额外断开重连”，不是无限运行。`--hold-seconds` 从服务就绪开始计时；连接、配对、服务操作各有等待阶段，不能把 45 秒理解为整轮硬上限。

### 4.3 HID 锁屏三次重连：EX-HID-03

前提：保留同一独占 HID 身份和两端配对；手机锁屏，本轮完全不操作手机。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/ble_lab.py run hid \
  --adapter hci0 --cycles 3 --hold-seconds 30 \
  --initial-timeout 45 --reconnect-timeout 30
```

共四轮：启动后的初始连接，加三次电脑断开、重新广播、等待 iPhone 连回；每轮保持 30 秒，保持时间合计 120 秒，另有连接/安全/清理时间。正常结果各轮 `link/encryption/service_ready/hold=true`，最终 `passed=true`。

### 4.4 HID 音乐和屏幕键盘：EX-HID-04

前提：同一独占 HID 已配对；本轮允许操作手机，专门检查使用影响。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/ble_lab.py run hid \
  --adapter hci0 --cycles 0 --hold-seconds 60 \
  --initial-timeout 45
```

运行前让手机播放音乐。连接后确认声音仍走原来的扬声器/耳机，播放和音量正常；再打开备忘录等输入框，验证屏幕键盘出现和输入正常。记录实际现象，不仅记录 `passed`。

### 4.5 ANCS 当前配置短筛选：EX-ANCS-01 的对照

前提：该候选没有已保存配对；知道本次配置未通过，未来只为复核差异才运行。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/ble_lab.py run ancs \
  --adapter hci0 --enroll --cycles 0 \
  --hold-seconds 30 --initial-timeout 45
```

手机查找 `BT-Auth-ANCS`，若能够进入配对则按提示确认，并处理 iPhone 的通知授权。电脑在已建立链路上订阅手机的 ANCS，不读取通知正文或执行通知操作。未出现名称/连接就记录发现阶段失败，不能宣称已经检验通知订阅。

此命令是**当前归档代码**的有限筛选，不是早期每次失败运行的逐字命令；早期参数不完整已在报告标明。

### 4.6 CTS 当前配置短筛选：EX-CTS-01

前提：该候选没有已保存配对；保留已有 HID 配对，未来只为复核差异才运行。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/ble_lab.py run cts \
  --adapter hci0 --enroll --cycles 0 \
  --hold-seconds 30 --initial-timeout 45
```

手机查找 `BT-Auth-CTS`。若连接与配对成功，电脑作为 GATT 客户端读取手机提供的 Current Time。本次实测 45 秒无连接，未进入该阶段；未来结果不同，应保留新记录与环境差异，不能覆盖本次失败事实。

### 4.7 独占结果读取与故障恢复

前提：`report` 仅读取保存结果；`restore` 只在存在待恢复记录时访问适配器。

```sh
sudo experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/ble_lab.py report
sudo experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/ble_lab.py restore
```

独占 HCI 必须先关闭，再由 BlueZ 接回；可能暂时出现 Busy/InProgress/NotReady。当前实现会重新读取状态、等待过渡并有界重试；失败保留 `.runtime/adapter-restore.json`。恢复依据按物理地址定位，不能把旧 `hci1` 编号机械套用到另一轮。

## 5. 结束、保留和清理的准确含义

下表针对独占与共存入口。`hid_release_test.py` 与 `observe_le_link.py` 专门观察连接保持，结束时保留目标连接、不执行主动断开；退出和恢复边界见 [专用说明](HID_RELEASE.md)。

| 项目 | 独占入口 | 共存入口 |
| --- | --- | --- |
| 进程/临时无线资源 | 关闭自己的 HCI user channel 和服务 | 注销广告/GATT/临时代理，关闭自身 D-Bus、MGMT、monitor |
| 适配器设置 | 按日志恢复；Alias 核对，不覆盖别处改名 | 恢复自身修改的 Pairable，其余原设置核对 |
| 本轮手机连接 | 关闭独占无线栈时结束 | 仅断开记录范围内新增目标/候选 LE |
| 原其他连接 | 独占时可能已断，需要原系统/用户重连 | 要求全程保持；缺失或中断明确报告 |
| 配对密钥 | `.runtime` 中实验密钥默认保留 | 原 BlueZ 配对保留；修复所得新密钥也保留 |
| 文件 | 本次归档保留源码、依赖、文档和证据 | 私有运行日志默认保留，不等于临时广播仍运行 |
| 手机缓存、系统日志、外部服务 | 不承诺回滚 | 不承诺回滚 |

原自动锁屏程序可能因独占断线照常锁屏；实验不会改认证或锁屏配置。若操作者先前手动暂停了 `bluetooth-auth-auto-connect.service`，应根据运行前记录恢复它；不要对原本未运行的服务盲目启动。仅当运行前确实 active 且由本次操作暂停时，可执行：

```sh
sudo systemctl start bluetooth-auth-auto-connect.service
systemctl is-active bluetooth-auth-auto-connect.service
```

不要停止全局 `bluetooth.service` 作为默认排障；两个入口都需要 BlueZ 的相应管理/恢复能力。脚本无法还原可发现/可配对计时器的精确剩余时间，也无法远程保证清除手机配对和名称缓存。

实验源码、文档和证据作为归档保留。`clean` 会删除独占身份、密钥和事件日志，只适用于已恢复且明确放弃旧实验身份的情况；清理后再次配对不能算重连。`uninstall` 是旧的工具自删除入口，遇到未知额外文件会拒绝，不能用于删除当前归档目录。

## 6. 保存新一轮结果

每次只运行一个入口、一个场景。完整保留终端命令至最终 `result/restore`，另填 [模板](RUN_RECORD_TEMPLATE.md)；若中断或失败，输出和恢复结果同样保存。独占日志位于 `.runtime/events.jsonl`，共存日志位于 `.coexist/events.jsonl`，权限可能需要 sudo；它们会追加多轮内容，分享前明确本轮范围。

不要归档 `.runtime/manifest.json`、`device.json`、`keys.json` 或原 BlueZ 密钥库；这些含实验或设备密钥。分享运行日志时把蓝牙地址替换为一致的角色标记，保留事件名、类型、数值、错误和周期关联；缺失日志注明缺失。

更新结果用新的 ID，不覆盖已有成功或失败。要报告“锁屏无需操作”“音频/键盘正常”，必须同时记录操作者的条件和观察，不能由日志自动补填。

## 7. 归档完整性校验

前提：从项目根目录运行；仅核对源码与材料哈希，不访问蓝牙或私有运行目录。

```sh
python3 -B - <<'PY'
import hashlib
import json
from pathlib import Path

root = Path('experiments/iphone_ble').resolve()
manifest = json.loads((root / 'results/archive-manifest.json').read_text())
failed = []
for base, entries in ((root, manifest['files']), (root.parents[1], manifest.get('repository_files', []))):
    for item in entries:
        path = (base / item['path']).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            failed.append(item['path'])
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            failed.append(item['path'])
print('归档校验通过' if not failed else '发生变化：' + ', '.join(failed))
raise SystemExit(bool(failed))
PY
```

后续有意修改文件会导致哈希不同，这是版本变化提示，不代表蓝牙状态异常。清单覆盖本目录的源码、Python 原型、测试、文档及证据；不纳入 `.venv`、私有状态、Git 索引、清单自身或无关的仓库根目录文件。
