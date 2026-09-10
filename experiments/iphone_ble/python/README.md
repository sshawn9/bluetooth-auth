# Python HID 原型归档

本目录保留迁移到 Rust 之前的 Python HID 实现和离线测试，用于阅读设计演变、复现逻辑和对照实现。包名仍为 `bluetooth_auth_hid`，按源码目录使用；仓库根目录的 `pyproject.toml` 不安装此包。

## 文件用途

| 文件 | 保留的实现 |
| --- | --- |
| [daemon.py](bluetooth_auth_hid/daemon.py)、[bluez.py](bluetooth_auth_hid/bluez.py)、[client.py](bluetooth_auth_hid/client.py) | 早期常驻方案：持续提供 HID 和广播，通过 D-Bus 处理一次 `ask-or-connect` 请求。后端保留了当时要求目标 `Paired/Bonded/Trusted` 均为真的条件。 |
| [device.py](bluetooth_auth_hid/device.py) | 独立基础函数：一个数据类，分别提供注册、注销、连接和查询。注册不启动广播；连接函数在需要时启动一次限时广播，等待 iPhone 连回，结束后停止广播。 |
| [register_hid.py](bluetooth_auth_hid/register_hid.py) | 无参数的线性注册示例：在 `hci0` 注册服务，返回持有服务的 D-Bus 连接；不广播，也不限定目标手机地址。 |
| [hid.py](bluetooth_auth_hid/hid.py) | 常驻方案和注册示例复用的 GATT 服务、特征、描述符及广播对象。 |
| [link.py](bluetooth_auth_hid/link.py) | 读取 Linux HCI 连接快照。外层的 HID 注销和重启观察实验仍使用此模块核对 LE 句柄和加密状态。 |
| [tests/](bluetooth_auth_hid/tests/) | 基础函数、常驻请求、客户端、GATT 对象和 HCI 快照的离线回归。 |

三种原型保留各自的流程和接口，不作为一套流程依次运行。常驻原型还依赖系统 D-Bus 对服务名 `org.bluetooth_auth.Hid` 的授权；本归档不附带其部署配置。

## 复现离线测试

使用 Python 3.13 和 `dbus-fast==5.0.17`，可复用外层实验的 `.venv`。环境准备见[复现手册](../docs/REPRODUCE.md#2-环境和目录)。以下命令均从仓库根目录执行，无需 sudo。

```sh
experiments/iphone_ble/.venv/bin/python -B \
  experiments/iphone_ble/python/bluetooth_auth_hid/tests/run_offline.py
```

该入口自动设置包的导入路径，并拦截真实蓝牙 socket 和 socket 连接。归档时 63 项测试通过，覆盖假 D-Bus 后端和 HCI 快照；不包含对 `register_hid.py` 示例的独立实机验证。

外层实验另有一套测试，核对实验流程及两个观察入口对 `link.py` 的引用：

```sh
experiments/iphone_ble/.venv/bin/python -B experiments/iphone_ble/check_offline.py
```

这两个入口分别收集各自的测试目录。真实私有 D-Bus 的退出回归另见 [HID_RELEASE.md](../docs/HID_RELEASE.md#离线验证)。

## 与实机记录的关系

既有手机实测结果对应外层的 [BLE 实验入口](../README.md)及其[结果记录](../RESULTS.md)。其中 `hid_release_test.py` 和 `observe_le_link.py` 复用了本目录的 `link.py`；不能将这些实测结果扩展为整套 Python daemon 或所有原型都已在手机上验证。

实际复现手机连接时，使用[复现手册](../docs/REPRODUCE.md)和 [HID 注销、重启观察步骤](../docs/HID_RELEASE.md)中的实验入口。归档保留原型行为及历史测试边界，Rust 的后续改动不会自动同步到这里。
