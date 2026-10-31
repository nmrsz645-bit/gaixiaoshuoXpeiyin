# 下一位 Codex：小说处理中心 1.0.30

> 新 Windows 接手、环境、构建、版本、OSS/网站、验收回滚和凭据：**[完整发布指南](docs/RELEASE-GUIDE.md)**。

更新日期：2026-10-31。先读本文件、README 和 AGENTS，再核对远端实际状态；旧文档中的盘符、版本或历史测试数字不作为当前依据。

## 用途、版本与仓库关系

- 用途：Windows 本地 TXT 改写/可选场景审改、企业微信发送、Edge/阿里云配音和可恢复文件队列。
- 本分支版本：**1.0.30，正式版**；目标仓库 `nmrsz645-bit/gaixiaoshuoXpeiyin`，分支 `main`，发布标签 `v1.0.30`。
- 本公开仓库 main 维护正式版 1.0.30；测试版单独维护，不得误标稳定版或擅自公开私有新增源码。
- 原始构建源提交：`ad6c3aa3f32139102c55630cb6017d2c45bb8018`。正式版的发布记录基础提交为 `3260809b9fc1a2f31a4ff675ddca214a103443ce`；测试版在该基础上演进。
- 本次只补齐 GitHub 源码、文档和 Release，沿用已有 Windows 包，不重新生成同版本包。最终发布源码提交以 `git rev-parse v1.0.30^{commit}` 及 Release 标签为准；对照核验记录的源提交和逐模块包内代码核对。
- 当前正式源码根 `version.json` 与 `unified_app.DEFAULT_APP_VERSION` 均为 1.0.30。`read_app_version` 先读取源码/安装目录的该文件，缺失或损坏时才用回退值；便捷构建批处理会复制该文件，Python 构建工具则用显式 `--version` 写入包版本。原 v1.0.30 标签的开发元数据曾为 1.0.26，后续源码维护修正了显示元数据，不重打标签、不替换已发布程序。

## 架构与关键入口

| 路径 | 职责与后续修改入口 |
| --- | --- |
| `unified_app.py` | Tkinter 设置/状态/日志、DailyStats、UnifiedService、启动停止、自动恢复及更新入口。先看 ensure_app_files、UnifiedService.start 和 check_for_updates。 |
| `runtime.py` / `credential_store.py` | 打包/源码运行目录、HTTPS 证书定位、Windows DPAPI 凭据保护。 |
| `novel_monitor/config.py` | 改写配置、路径、接口 URL、领取时模式配置。 |
| `novel_monitor/monitor_service.py` | 两线程改写调度、稳定文件判定、同名等待与重试节奏。 |
| `novel_monitor/file_utils.py` | 原始文件名关联、rewrite_name_conflict、完成文件保护、原子发布辅助。 |
| `novel_monitor/processor.py` | 领取事务、暂存改稿、发送凭据、恢复与发布到配音队列；排查重复发送先看这里。 |
| `novel_monitor/deepseek_client.py` | API 协议、分段、失败段拆分与检查点、指定词规则。 |
| `text_preparation.py` | 位于 novel_monitor：章节行/数字行清理和 7000 字上限，原文位置映射。 |
| `scene_document.py` / `scene_model.py` / `scene_review.py` | 位于 novel_monitor：主线事实/来源、场景编辑协议、独立核对、并行预算与风险报告。 |
| `semantic_compliance.py` | 位于 novel_monitor：既有定向风险复核与兼容恢复逻辑。 |
| `voice_monitor.py` / `aliyun_tts.py` | 配音领取/回执、原名输出、Edge/阿里云分支、失败恢复、音频校验及超时长裁剪。 |
| `novel_monitor/logging_setup.py` | 24 小时日志保留；不要把日志轮转当作业务进度清理。 |
| `tools/build_github_release.py` / `prepare_domestic_release.py` | 新目录构建、ZIP/逐文件哈希清单、安排 OSS 待上传文件；不自动授权生产上传。 |
| `updater-patch/` | 现有生产 UpdateAgent.exe 与公开地址配置，不含凭据；稳定版使用，测试版禁用启动。 |
| `test_pipeline.py` / `test_scene_*.py` / `test_text_preparation.py` / `test_release_manifest.py` / `test_rollback_v125.py` | 事务保护、语义/场景、并行、清理、更新清单和旧行为回归。 |

事务顺序：领取 TXT 到 `.rewrite-processing/<任务ID>` → 清理/原始字节备份 → 原路径改写或场景审改 → 固定词处理/最终清理 → 暂存/发送凭据 → 已改完成 TXT → `.voice-processing`/音频回执 → 完成 TXT+MP3。检查点和回执用于恢复与去重，不能当缓存清空。

## 环境和依赖

- 目标 Windows 10/11 x64；本轮真实测试：Windows x64、CPython **3.13.15**、Tkinter。
- 直接依赖：requests **2.32.4**、edge-tts **7.2.5**、imageio-ffmpeg **0.6.0**、PyInstaller **6.14.2**、alibabacloud-nls-python-sdk **1.0.2**；其余 SDK/certifi 及传递版本见 `requirements-windows-verified.txt`。
- Windows 包自带 Python/证书/FFmpeg；源码开发先建独立 .venv，勿使用带真实业务配置的安装目录开发。NSIS 安装程序仅测试版需要 NSIS 3，makensis.exe 在 PATH 或默认 NSIS 目录。
- `.env.example` 只说明配置边界，程序不读 .env；安全配置模板/字段、Key/Webhook/阿里云获取入口见 examples/configuration/README.md 和 README 初次配置。

## 开发启动与测试命令

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows-verified.txt
cmd /c run_tests.bat
.\.venv\Scripts\python.exe -m unittest test_pipeline.py test_scene_compliance.py test_scene_parallel.py test_text_preparation.py test_release_manifest.py test_rollback_v125.py
.\.venv\Scripts\python.exe unified_app.py
```

测试不需要 Key，不调用真实 AI/TTS/Webhook。普通 GUI 启动会初始化配置，并在约 300 ms 后尝试启动处理；务必用没有真实任务/凭据的隔离副本。

首次标签的 GitHub Windows 回归也复现了跨盘 TEMP 的 os.path.relpath 测试夹具错误。后续维护提交将该测试的 TemporaryDirectory 显式放到 Path.cwd()，当前分支不再依赖 TEMP 所在盘，默认回归已在跨盘环境通过。应用逻辑、版本和原始 Windows 包未改变，既有 v1.0.30/v1.0.31-beta 标签未重打。开发请取最新 main/测试分支；若复验原标签，则可使用下述同盘临时目录配置。

```powershell
$taskTemp = Join-Path (Get-Location) '.test-temp'
New-Item -ItemType Directory -Path $taskTemp -Force | Out-Null
$env:TEMP=$taskTemp; $env:TMP=$taskTemp; $env:TMPDIR=$taskTemp
cmd /c run_tests.bat
```

本轮通过：正式版 409 项、测试版 416 项（含 7 项快捷方式/更新测试）。测试运行在单独子进程及本次专用同盘临时目录；真实计费接口、平台内容审核和生产队列恢复不在本轮验收范围。

## 构建打包

```powershell
.\.venv\Scripts\python.exe tools\build_github_release.py --version 1.0.30 --output C:\npc-builds\1.0.30-new
```

输出目录必须不存在。`构建桌面版.bat` 是旧便捷入口，会写固定目录；可重复验收应使用上述带显式版本/新输出目录的 Python 工具。本轮保留原 Windows 文件，未重新构建同版本；NSIS 安装/覆盖/卸载没有在本轮对当前 Windows 用户环境执行。

## 发布和升级

1. GitHub：读取远端 head/标签/Release，正常提交和 fast-forward 推送，拒绝覆盖他人改动；创建缺失版本标签、上传经审查的原始包和 SHA256SUMS。正式 v1.0.30 标为 stable；v1.0.31-beta 必须 prerelease 且不能设为 latest。
2. GitHub Actions 当前只离线测试/构建/保存 artifact，不上传 OSS 或切换生产清单；测试版 CI 还要明确运行 test_desktop_shortcut.py。
3. 生产更新为单独工作：稳定版检查 OSS latest.json；不可变版本 ZIP → 公网完整哈希回读 → latest.js → latest.json 最后 → 网站/隔离升级回滚验收。必须另行明确授权，GitHub 发布不等于生产更新。
4. 稳定版运行中可能静默检查并重启，生产复现前先核实版本和活动任务；测试版版本号带 `-` 时 check_for_updates 直接返回。
5. 升级/恢复必须保留配置、原文备份、队列、处理中目录、发送/音频回执、统计和音频；保持原文件名，不自动改名绕过冲突。所有客户电脑是否升级不能从 Release 状态推断。

## 安全与数据

只提交源码、离线合成测试、安全示例和已脱敏说明。禁止真实 .env/config.json、Key/Token/Webhook/Cookie、浏览器配置、TXT/MP3、日志/回执/任务目录及内部聊天记录。测试夹具中的短合成文本属于回归样本，不是真实任务数据。

API Key/Webhook 以本地文件保存，应通过 GUI 管理。阿里云 AccessKey 的 DPAPI 密文依赖 Windows 用户；不要把受保护字段误当可通用迁移的配置。变更凭据权限、远程通知、生产重试和恢复均需针对实际操作的授权。

## 已完成、已知问题和后续入口

- 已完成：1.0.30 固定行清理/7000 截断、备份和同名保护；已发送暂存稿不受新空正文阻断；场景复核并行及检查点恢复；测试版界面/快捷方式/安装源与对应离线回归。
- 同名等待曾有用户反馈，现象后来已报告解除；未取得故障机完整日志，根因仍未独立确认，不能宣称修复了某个死锁。复现需保留版本、时刻、占用路径、任务 ID、retry.json/semantic-mode.json 和相关日志，先只读区分活动任务、延迟重试、重复入队和历史残留。
- 当前主要验证限制：外部 AI/语音服务的可用性、真实业务发送/配音、客户升级、平台审核结论，以及本轮新安装/覆盖/卸载的 Windows 用户环境行为均未验收。包内代码和 ZIP/安装程序载荷已静态核对，见核验记录。
- 已知安装边界：NSIS 的同名进程检查作用于当前系统同名 EXE；不要在生产程序仍运行时做安装测试。安装/卸载涉及桌面、开始菜单及 HKCU 项，必须先隔离，不能仅靠 /D 新目录假定完全无副作用。
- 下一次修改先确认具体目标和授权；队列/发送问题优先读 processor/file_utils/monitor_service，配音问题读 voice_monitor/aliyun_tts，场景行为读 scene_review/text_preparation，界面/安装问题读 beta 的 ui_theme/installer。每次以实际源码和测试证据更新本文件。

## 更新器组件源码缺口

应用 Python 源码、测试和打包入口齐备；UpdateAgent 当前仅保留固定二进制与产品配置，其原始源码尚未对应确认，不能宣称所有组件源码均可重建。固定 SHA-256、实际类型、产品字段、排除的同名候选及维护入口见 [docs/updater-dependency.md](docs/updater-dependency.md)。本轮未替换更新器或执行生产更新。

## 标签与后续维护提交

程序标签保留最初的源码/文档快照；最新分支另有跨盘测试夹具、版本显示元数据及文档维护提交。正式根版本和显示回退均已对齐 1.0.30；AI/队列/配音业务算法和已发布字节不变。Release 说明列出两类提交，不能把维护提交误当成新程序版本。GitHub 标签首次自动构建在测试阶段失败，未执行打包；后续分支回归状态应按实际 Actions 结果读取。

## 正式源版本元数据维护

根 version.json 确实参与源码启动和旧批处理打包，不是闲置文件；已与显示回退统一为 1.0.30。原标签及 Windows 包未重打/替换。最新源码只有显示回退常量与原包对应应用源码不同，明确差异、AST 核对和版本来源见 [源版本元数据核验](docs/releases/2026-10-31-source-version-metadata.md)。

## GitHub Latest 生产兼容约束

最新正式程序为 1.0.30，但 GitHub Latest 标记保留在 v1.0.18，因为旧更新器固定读取 releases/latest/download/latest.json。最新程序人工下载使用版本专属 v1.0.30 Release；新补传使用 --latest=false，迁移旧更新通道必须另核生产授权。旧入口已回读恢复原清单 SHA-256，细节见 [兼容入口](docs/legacy-github-update-compatibility.md)。
