# iPhone 原生 BLE 实验归档

本目录保存使用 iPhone 系统功能比较 ANCS、消费控制类 HID over GATT、CTS 的实验代码、结果和复现材料。归档截止于实验验证阶段，尚未接入正式认证流程。

## 已得到的结论

**本机 HID 路径已通过短测，BlueZ 可以在同一适配器上提供 HID 手机连接，同时保留原有蓝牙设备连接。**

| 路径 | 实测结论 | 证据 |
| --- | --- | --- |
| Bumble 独占 HID | 首配、加密、订阅通过；锁屏初始连接加三次重连，每轮保持 30 秒；另一次 60 秒中音乐和屏幕键盘正常 | EX-HID-01/03/04 |
| BlueZ 共存 HID | 复用配对建立目标 LE 连接、加密 HID 读取、15 秒保持、原有 BR/EDR 连接保持和退出恢复全部通过；用户确认音频未断 | BZ-HID-04 |
| HID 服务撤销后保持 | 提供进程退出、确认 HID 和广播撤销后，同一条既有加密 LE 连接保持 60.1 秒；本轮保持不要求 HID 程序常驻 | BZ-HID-05 |
| 重启后无 HID 的只读观察 | 电脑配对记录仍在，但 20 秒内没有目标 LE 连接，未进入保持观察 | BZ-HID-06 |
| 重启后临时提供 HID | 从未连接状态建立加密 LE；提供进程退出、HID/广播撤销后，同一连接保持 60 秒 | BZ-HID-07 |
| ANCS / CTS 单独征询 | 当前配置未通过发现/连接；保留失败结果，不推断所有实现或 iOS 设备都不可用 | EX-ANCS-01、EX-CTS-01 |

连接实验采用**电脑提供广播，由 iPhone 连回**；BZ-HID-05 复用既有连接，BZ-HID-07 从未连接状态开始，两者均通过服务撤销后的短时保持。按需临时提供 HID 有实测依据；结论不覆盖长期稳定性、所有恢复场景、所有音频服务隔离或正式认证安全。BZ-HID-06/07 的 Trusted 条件也发生变化，不能将两轮对比当作 HID 是唯一恢复原因的证明。

新增三轮的对照、已验证的“先连接、保持 5 秒、再退出 HID”顺序，以及身份和 Trusted 的解释，集中见 [HID 退出与重启后连接实验](docs/HID_RELEASE.md)。

## 阅读顺序

| 文档或材料 | 用途 |
| --- | --- |
| [实验报告 RESULTS.md](RESULTS.md) | 明确结论、16 项成功/失败/恢复记录、证据等级、失误经验及未覆盖范围 |
| [复现手册](docs/REPRODUCE.md) | 环境准备、各场景前提、可直接执行的命令、手机操作、成功判据和恢复方法 |
| [HID 注销与重启观察](docs/HID_RELEASE.md) | 服务撤销后保持、重启后只读观察及临时 HID 恢复的结果和复现命令 |
| [实现说明](docs/IMPLEMENTATION.md) | 文件职责、控制器/身份/名称、BLE 与 GATT 角色、各入口的判据与恢复边界 |
| [Python HID 原型](python/README.md) | Rust 迁移前的常驻方案、独立基础函数、注册示例及离线测试 |
| [运行记录模板](docs/RUN_RECORD_TEMPLATE.md) | 后续复现时记录环境、条件、原始输出、人工观察和恢复结果 |
| [证据目录说明](results/README.md) | 完整脱敏附件、对话摘录、机器可读记录与校验清单的来源说明 |

独占和共存入口的 `run`、HID 退出测试及只读观察入口都会访问本机蓝牙接口；只读观察不改变蓝牙状态，独占模式会中断原有连接。请按复现手册选择场景并核对相应结果；离线回归不能替代实机验证。

## 代码入口

BLE 实验入口位于本目录，早期 RSSI 辅助诊断归入 `diagnostics/`，Python HID 原型及其测试归入 `python/`。

```text
iphone_ble/
├── ble_lab.py                  # Bumble 独占入口：ANCS / HID / CTS
├── radio.py                    # 独占无线栈、服务、配对和重连周期
├── adapter.py                  # BlueZ 访问、独占交接和恢复
├── state.py                    # 实验身份、密钥与恢复记录管理
├── bluez_hid_lab.py             # BlueZ HID 共存入口
├── hid_release_test.py          # 退出 HID 提供进程，观察原加密 LE 是否保持
├── observe_le_link.py           # 不注册 HID，仅观察目标加密 LE；用于重启后短测
├── coexist_gatt.py             # 临时 HOGP / Battery / DIS / 广告对象
├── coexist_pairing.py          # 有限配对窗口中的数字确认代理
├── coexist_link.py             # MGMT 连接观测及新增目标 LE 清理
├── coexist_trace.py            # 被动广播命令诊断，不抓取密钥或原始流量
├── check_offline.py            # 带 socket 拦截的离线回归入口
├── check_public_privacy.py     # 仓库文件的地址、个人路径与密钥特征检查
├── requirements.txt            # 固定实验环境依赖版本
├── diagnostics/
│   └── monitor_rssi.py         # 早期 BR/EDR RSSI 辅助诊断
├── python/
│   ├── README.md               # Python 原型用途、依赖与测试命令
│   └── bluetooth_auth_hid/     # 三种原型、共享模块及包内离线测试
├── tests/                     # fake 后端、内存控制器、恢复和私有 D-Bus 退出测试
├── docs/                      # 正式中文复现及实现说明
└── results/                   # 结果、证据、来源与归档哈希
```

`.venv/`、`.runtime/`、`.coexist/` 和 `.environment.json` 是本地运行材料，由 `.gitignore` 排除。状态目录含配对数据与恢复依据，使用方法见复现手册。

## 不访问蓝牙的入口

前提：从项目根目录运行；`plan` 只显示方案，离线回归需要已有实验 `.venv`。

```sh
python3 -B experiments/iphone_ble/ble_lab.py plan
python3 -B experiments/iphone_ble/bluez_hid_lab.py plan
experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/check_offline.py
experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/python/bluetooth_auth_hid/tests/run_offline.py
```

两个离线入口分别运行实验流程测试和 Python 原型测试。

首次依赖安装见[环境准备](docs/REPRODUCE.md#2-环境和目录)。准备实机复现时，先按手册区分“首次/修复配对”和“复用配对”，不要仅复制历史命令中的参数。

## 归档原则

- 保留失败、前提错误和观测不足，不能只留下最后的成功。
- 两份附件保留完整提供结构，地址和原设备名替换为角色标记；私人附件标识不发布，其他阶段明确标为对话摘录或用户确认。
- 没有的旧源码、原始日志、设备型号和版本信息不补造；当前源码复现与历史旧版本重放是不同范围。
- 当前结果仅覆盖已记录的短测；未来环境复核使用新记录，不覆盖已有结论。
