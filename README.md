# 小说处理中心 1.0.30

Windows 本地 TXT 改写、风险片段审改、企业微信发送与自动配音工具。当前状态：**正式版**。

流程：`待改小说 → AI 改写/审改 → 企业微信发送 → 已改完成 → 配音 → 完成（TXT + MP3）`。

## 版本、源码和下载

- 对应仓库：<https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin>，分支 `main`；[本版本 Release](https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin/releases/tag/v1.0.30) 含可运行 Windows 包、校验文件及说明文档。
- 正式版源码主仓库是公开的 `nmrsz645-bit/gaixiaoshuoXpeiyin`。私有 `nmrsz645-bit/novel-processing-center` 保存同源正式版及独立测试分支，不改变其私有权限。
- 正式版仍为 **1.0.30**；**1.0.31-beta 是测试版**。版本数字较大不表示已通过正式发布验收。GitHub Release 与 OSS 客户端更新是两个独立渠道。
- 开发者先读 [HANDOFF.md](HANDOFF.md)、[AGENTS.md](AGENTS.md)。源码、构建源提交、测试及限制见 [本轮核验记录](docs/releases/2026-10-31-github-verification.md)。
- 本分支根 `version.json` 和源码显示回退值均为 **1.0.30**。源码启动读取该文件，构建也必须显式传 `--version 1.0.30`；旧标签中的 1.0.26 开发元数据已在后续维护分支修正，原 Windows 包和标签保持不变。

## Windows 安装与启动

1. 从对应 Release 下载完整 Windows ZIP（测试版另提供安装 EXE），并用 `Get-FileHash -Algorithm SHA256` 与 `SHA256SUMS.txt` 核对。
2. 解压到当前用户可写的新目录，保持 `Start-App.cmd`、`app/` 等原始层级；不要仅复制一个 EXE。已有安装先停止程序、关闭守护并备份整个旧目录，保留原队列、配置、回执和音频。
3. 双击根目录 `Start-App.cmd`。Windows 包自带 Python 和 FFmpeg，运行时不需要安装 Python；AI、企业微信、Edge/阿里云语音均需要网络。
4. 首次启动会在程序的 `app/` 内建立空白配置和工作目录，并尝试启动处理。缺少 Key/Webhook 时会提示配置未完成；先完成下节设置再启动业务。
5. Windows 10/11 x64 为目标平台；本轮验证环境为 Windows x64、CPython 3.13.15。下载来源/安全提示需自行核对，不要通过关闭安全软件解决。

## 初次配置

程序通过界面保存配置，**不会读取 `.env`**。安全空模板和字段解释见 [examples/configuration/README.md](examples/configuration/README.md)，模板不能替代自己的真实配置。

| 设置 | 获取方式及保存位置 |
| --- | --- |
| AI 接口 | 在“改小说设置”选 DeepSeek 官方或自定义服务，填写本人有权使用的 API Key、完整接口 URL 和模型并保存。DeepSeek Key 在其[平台控制台](https://platform.deepseek.com/)创建；自定义接口由对应供应商提供。默认 URL 为 `https://api.deepseek.com/chat/completions`，默认模型 `deepseek-v4-flash`，每次最多 8000 字、等待 300 秒。 |
| 企业微信 | 在本人有管理权限的群内添加机器人，取得 Webhook 并填入“改小说设置”。改写流程启动会校验 Key 和 Webhook；接口测试会真实向机器人发送消息。获取方式参见[企业微信群机器人文档](https://developer.work.weixin.qq.com/document/path/91770)。 |
| Edge 语音 | “配音设置”选 `edge`，保存音色、语速、音量和音调。不需要阿里云凭据，但依赖在线 Edge 服务。 |
| 阿里云语音 | 选 `aliyun` 或 `edge_then_aliyun` 时，在本人[智能语音控制台](https://nls-portal.console.aliyun.com/)取得项目 AppKey，并提供已获授权的 AccessKey ID/Secret。通过界面保存；不要扩大账号权限或把凭据写进仓库。 |
| 目录与规则 | 默认输入 `待改小说`，改写输出/配音输入必须同为 `已改完成`，最终输出 `完成`。在对应设置页保存目录、审改开关和指定违禁词。 |

AI Key 和 Webhook 分别保存在 `app/改写/deepseek_api_key.txt`、`wechat_webhook_url.txt`；语音配置在 `app/config.json`，改写配置在 `app/改写/config.json`。阿里云 AccessKey 使用 Windows DPAPI 保护，不能直接迁移到另一 Windows 用户后假定可解密。配置、规则、日志、正文和回执均属于用户本地数据，不随 Git 或 Release 分发。

## 主要操作与实际处理规则

1. 保存“改小说设置”和“配音设置”。有授权时可点击“测试全部接口”；该操作会调用真实 AI/语音服务并发送企业微信消息，可能产生费用。
2. 将本人有权处理的 UTF-8 TXT 放入配置的输入目录。确认路径后启动自动处理；程序默认同时处理两本改写任务。
3. 不勾选“AI 场景审改”沿用原 `optimize_text` 路径；勾选时执行风险定位、场景修改和独立复核。开关在领取任务时写入 `semantic-mode.json`，不改变已领取任务的模式。
4. 两条路径都先整行删除单个 0～9 数字行、以“第1章/第一章”开头的行及 `1.正文/12.正文` 式编号行；识别时允许行首连续 `#`。`###12`、普通 `###正文标题`、日期和小数保留。先删行，再只取前 **7000 个非空白字符**，标点计入，超出的结尾会被截掉。
5. 有删行/截断时保存原始字节备份和映射；空送审正文保留原文并停止该篇。审改保护范围是清理后保留的正文。场景编辑保留关键事件性质、人物关系和结局；剩余内容风险会记录后继续，必需技术/协议/事实核对未完成则保留进度等待重试。
6. AI 最后核对后执行指定词精确替换/遮盖，再执行固定清理；此后不再调用 AI。TXT 处理不保证广告标题、封面、字幕或视频审核通过。
7. MP3 使用最初 TXT 的文件名主体。同名排队/处理中/完成 TXT 或 MP3 仍存在时等待，不覆盖、不自动改名。配音失败保留已改 TXT，仅按回执恢复配音。
8. 新生成 MP3 超过 59 分钟时保留前 35 分钟；59 分钟及以内不裁，历史成品不回头处理。裁剪失败保留原音频和回执。
9. 停止任务用界面“停止”；退出前关闭无人值守。不要强制清空处理中目录来解除等待。

## 常见问题与取证

| 现象 | 核对方法 |
| --- | --- |
| 缺少 Key/Webhook，启动失败 | 在设置页补齐并保存。不要把真实配置上传到 GitHub 求助。 |
| 同名文件仍在，改写等待 | 读取提示中的占用完整路径，检查 `.rewrite-processing`、配音输入、`.voice-processing` 和完成目录。界面 TXT 数为 0 不代表内部事务目录为空；保留任务 ID、时间和日志后分析，不删除锁/任务目录。 |
| 已改完成有 JSON，但等待配音为 0 | 默认配音只扫描 TXT；`.preparation.json` 是清理/备份/映射记录，`.semantic-review.json` 是审改/风险报告。`.rewrite-queue-names/*.json` 是任务原名关联，回执是防重/恢复依据；不能一概删除。 |
| Edge 403、断网或阿里云失败 | 看 `app/voice_monitor.log` 和所选服务；`edge` 不会转阿里云，只有 `edge_then_aliyun` 会按配置回退。先保留 TXT/音频/回执，不对全部任务盲目重复合成。 |
| 改写中断或发送结果不确定 | 保留 `.rewrite-processing`、重试/模式文件和发送凭据，先核实外部发送结果，再决定恢复，避免重复收费或重复发送。 |
| 两端目录不一致 | 核对改写输出与配音输入是同一个实际路径，保存后重启需遵循任务停止边界。 |

日志位于 `app/改写/logs/monitor.log`（含轮转文件）与 `app/voice_monitor.log`，按约 24 小时保留；故障出现后尽快另存所需时间段。提交问题只提供脱敏错误、版本、任务 ID 和文件清单，不提交小说正文、Key、Webhook 或整个运行目录。

## 开发启动、测试和打包

```powershell
git clone https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin.git
cd gaixiaoshuoXpeiyin
git switch main
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows-verified.txt
cmd /c run_tests.bat
.\.venv\Scripts\python.exe unified_app.py
```

`requirements.txt` 是直接依赖；`requirements-windows-verified.txt` 固定了本轮通过测试的依赖闭包。先读 HANDOFF 的安全边界再启动 GUI，因为源码运行也会建立本地配置并尝试自动处理。首次标签曾暴露跨盘临时目录夹具问题，最新维护分支已修复；复验原标签时按 HANDOFF 使用同盘测试临时目录。程序标签和已发布包均未替换。

构建命令、NSIS 要求、GitHub 与生产更新的区别、源码和实际包的关联，以及未执行项目均在 [HANDOFF.md](HANDOFF.md)。

## 更新器依赖说明

应用 Python 源码完整；随包 UpdateAgent 为沿用的固定二进制，其原始源码尚未确认对应，因此所有组件源码的闭合仍有这一项缺口。详见 [更新器维护边界](docs/updater-dependency.md)。
