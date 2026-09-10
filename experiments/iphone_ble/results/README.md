# 实验结果与证据目录

本目录收录实测日志、结构化结果和代码校验清单。设备地址和电脑名称已替换为角色占位符，具体证据来源和完整性分别注明。

| 文件 | 内容与证据等级 |
| --- | --- |
| [observations.json](observations.json) | 16 项实测、失败、前提不成立、观测不足、恢复及历史记录；保留字段、参数、人工观察和限制 |
| [sources.json](sources.json) | 两份脱敏附件的归档校验值、来源类型、内容完整性与变换说明 |
| [EX-HID-03.log](evidence/EX-HID-03.log) | 用户提供的独占 HID 锁屏三次重连附件，保留完整提供内容，地址脱敏 |
| [BZ-HID-03.log](evidence/BZ-HID-03.log) | 已配对后仍使用修复模式的第二次运行附件，保留其 inconclusive 和完整提供内容 |
| [BZ-HID-04.json](evidence/BZ-HID-04.json) | 用户在对话中粘贴的共存成功输出的关键事件摘录；**不是原始 stdout 文件** |
| [BZ-HID-05.json](evidence/BZ-HID-05.json) | HID 提供进程退出后，同一加密 LE 保持 60.1 秒；用户该轮完整输出的 20 个事件转录，**不是直接采集的原始 stdout 文件** |
| [BZ-HID-06.json](evidence/BZ-HID-06.json) | 重启后无 HID/广播，20 秒内没有目标 LE，未进入保持观察；用户该轮完整输出的 8 个事件转录，**不是直接采集的原始 stdout 文件** |
| [BZ-HID-07.json](evidence/BZ-HID-07.json) | 临时 HID 从未连接状态建立加密 LE，提供进程退出且资源撤销后，同一连接保持 60 秒；该轮 19 个事件转录，**不是直接采集的原始 stdout 文件** |
| [validation.json](validation.json) | 各阶段离线验证命令、输出摘录和退出码；Python 原型归档后分别运行 63 项原型测试与 140 项实验流程测试，不属于实机证据 |
| [archive-manifest.json](archive-manifest.json) | 本目录实验、Python 原型、辅助诊断及材料的 SHA-256；不包含清单自身、Git 或私有状态 |

## 脱敏与真实性

完整附件将蓝牙地址和原电脑名称替换成稳定角色占位符，例如 `<PHONE_IDENTITY>`、`<HID_TEST_IDENTITY>`、`<COMPANION_IDENTITY>`、`<COMPUTER_NAME>`；固定实验名称 `BT-Auth-*`、公开协议字段、事件、时间参数、错误和结果仍保留。占位符不能作为实际配置使用；脱敏名称的长度不代表历史广播包中原名称的字节长度。

`sources.json` 只保存公开脱敏产物的校验值，不发布私人附件标识或未脱敏原文的校验值。原附件不是当前机器运行状态的快照，也没有提供每一轮历史实验的完整日志。

对话摘录保留用户给出的关键字段，缺失内容明确说明；没有把手工摘要伪装成原始完整日志。BZ-HID-02 只有首次配对成功的用户确认，BZ-HID-03 是随后第二次运行，二者不得混为一轮。

## 使用方式

先读 [实验报告](../RESULTS.md) 的结论和证据解释，再按 [复现手册](../docs/REPRODUCE.md) 选择场景。新的实测使用新 ID 和 [运行记录模板](../docs/RUN_RECORD_TEMPLATE.md)，保留当前失败与成功记录，不覆盖它们。

新增 BZ-HID-05/06/07 的对照表、操作顺序、身份与信任说明集中在 [HID_RELEASE.md](../docs/HID_RELEASE.md)。三轮原有事件与判定分别保留；文档澄清不增加实机测试次数。

本目录不保存实验身份密钥、原电脑密钥库、通知内容或手机时间值。`observations.json` 中的环境未知项为 null；不能根据设备地址猜测硬件型号或补造 iOS 版本。
