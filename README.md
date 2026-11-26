# 小说处理中心 1.0.34

程序按本地词表处理全文，使用“原词=替换词”的规则，保留未命中的原文。见 [1.0.34 发布说明](docs/releases/2026-11-26-v1.0.34.md)。

Edge、本地 LuxTTS、晓伊/晓晓默认随机、0.5～2 倍调速和私人声音克隆延续 [1.0.32](docs/releases/2026-11-18-v1.0.32.md)。模型、CPU/GPU 依赖随完整包及更新包分发，运行目录留空自动使用 `app/lux-runtime`。

提供 Edge、本地 LuxTTS，以及 Edge 失败后转本地 LuxTTS 三种模式。完整模型包约 5 GB；旧客户端首次升级的下载与临时解压约需额外 12.3 GB 空间，原安装和业务数据另计。测试候选的实际记录见 [Lux 功能](docs/LUX-VOICE-20261118.md)、[完整环境](docs/LUX-PORTABLE-20261118.md) 和 [Edge](docs/EDGE-REPAIR-20261118.md)。

> 新 Windows 接手、环境、构建、版本、OSS/网站、验收回滚和凭据：**[完整发布指南](docs/RELEASE-GUIDE.md)**。

Windows 本地 TXT 词语替换、企业微信发送与自动配音工具。

流程：`待改小说 → 本地词替换 → 企业微信发送 → 已改完成 → 配音 → 完成（TXT + MP3）`。

## 版本、源码和下载

- 对应仓库：<https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin>，分支 `main`；[本版本 Release](https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin/releases/tag/v1.0.34) 提供完整 OSS 下载直链、校验文件与说明。大模型 ZIP 通过 OSS 分发。
- 本公开仓库维护正式版源码；测试版单独维护，不能误标为稳定版或擅自公开测试源码。
- 1.0.34 沿用 1.0.31 的同名跳过、24 小时同名成品回收和自动流程页。旧版说明见 [1.0.31](docs/releases/2026-11-10-v1.0.31.md)。
- 开发者先读 [HANDOFF.md](HANDOFF.md)、[AGENTS.md](AGENTS.md) 和本次发布记录。源码、包、上传、网站和真实客户端验收分别报告。
- 根 `version.json`、源码回退值及正式构建参数一致为 **1.0.34**。`build_github_release.py` 仅构建应用基础包，必须再经 `build_lux_release.py` 组合模型才能正式分发。带 `-` 后缀的测试版继续跳过更新器。正式客户端使用静默更新。

## GitHub Latest 兼容约束

正式下载使用上方版本专属 Release 链接。GitHub Latest 标记为保护 1.0.18 及以前客户端的旧清单入口而保留在 v1.0.18；不要据此误判最新版，也不要在补传时默认改变 Latest。详见 [旧客户端更新兼容](docs/legacy-github-update-compatibility.md)。

## Windows 安装与启动

1. 从对应 Release 下载完整 Windows ZIP（测试版另提供安装 EXE），并用 `Get-FileHash -Algorithm SHA256` 与 `SHA256SUMS.txt` 核对。
2. 解压到当前用户可写的新目录，保持 `Start-App.cmd`、`app/` 等原始层级；不要仅复制一个 EXE。已有安装先停止程序、关闭守护并备份整个旧目录，保留原队列、配置、回执和音频。
3. 双击根目录 `Start-App.cmd`。Windows 包自带 Python 和 FFmpeg。词替换与 LuxTTS 在本地运行，企业微信和 Edge 使用网络。
4. 首次启动会在程序的 `app/` 内建立空白配置和工作目录，并尝试启动处理。先填写企业微信 Webhook 和词表，再启动业务。
5. Windows 10/11 x64 为目标平台；本轮验证环境为 Windows x64、CPython 3.13.15。下载来源/安全提示需自行核对，不要通过关闭安全软件解决。

## 初次配置

程序通过界面保存配置，**不会读取 `.env`**。安全空模板和字段解释见 [examples/configuration/README.md](examples/configuration/README.md)，模板不能替代自己的真实配置。

| 设置 | 获取方式及保存位置 |
| --- | --- |
| 本地词表 | 在“改小说设置”点击“编辑指定违禁词”。每行写一组 `原词=替换词`。原有 AI 配置与场景审改选项保留在界面，1.0.34 的处理以本地词表为准。 |
| 企业微信 | 在本人有管理权限的群内添加机器人，取得 Webhook 并填入“改小说设置”。接口测试会真实向机器人发送消息。获取方式参见[企业微信群机器人文档](https://developer.work.weixin.qq.com/document/path/91770)。 |
| Edge 语音 | “配音设置”选 `edge`，保存音色、语速、音量和音调，使用在线 Edge 服务。 |
| 本地 LuxTTS | 选 `lux_tts`，或选 `edge_then_lux` 作为 Edge 失败后的备用。完整测试包的运行目录留空即自动使用随包模型，语速可设 0.5～2.0 倍。通过“LuxTTS 音色与声音克隆”导入私人参考音频或选择默认两种参考音色。 |
| 目录与规则 | 默认输入 `待改小说`，改写输出/配音输入必须同为 `已改完成`，最终输出 `完成`。在对应设置页保存目录和指定违禁词。 |

AI Key 和 Webhook 分别保存在 `app/改写/deepseek_api_key.txt`、`wechat_webhook_url.txt`；语音配置在 `app/config.json`，改写配置在 `app/改写/config.json`。私人克隆音色存于 `app/lux-custom-voices`，迁移时需连同参考音频和元数据保存。配置、规则、日志、正文和回执均属于用户本地数据，不随 Git 或 Release 分发。

## 主要操作与实际处理规则

1. 保存“改小说设置”和“配音设置”。有授权时可点击“测试全部接口”；它会测试配音并发送企业微信测试消息。
2. 将本人有权处理的 UTF-8 TXT 放入配置的输入目录。确认路径后启动自动处理；程序默认同时处理两本改写任务。
3. 新任务只替换词表命中的原词，长词优先，以一次扫描完成替换。保留章节、换行和全文。
4. 命中只有原词、缺少替换词的规则时，保留原文，等补齐词表后再处理。
5. 旧版已发送并取得有效回执的任务沿用暂存稿续办。待发送的旧任务用原文走本地词表。暂存稿和回执无法核对时保留任务等待核对。
6. 处理完成的 TXT 继续走企业微信发送、配音队列和原名输出。
7. MP3 使用最初 TXT 的文件名主体。完成目录的同名 TXT 或 MP3 超过 24 小时后，程序将旧成品移入该目录下的 `.expired-name-conflicts`，再处理这本书。时长按创建时间和最后修改时间中的较晚者计算。队列和处理中任务保留原位。占用仍存在时跳过该书，其他书继续处理。配音失败保留已改 TXT，仅按回执恢复配音。
8. 新生成 MP3 超过 59 分钟时保留前 35 分钟；59 分钟及以内不裁，历史成品不回头处理。裁剪失败保留原音频和回执。
9. 停止任务用界面“停止”；退出前关闭无人值守。不要强制清空处理中目录来解除等待。

当前开发分支已移除“AI 改写记录”入口和日报生成。已有日报仍可在 `app/改写/AI改写记录` 文件夹查看。

## 常见问题与取证

| 现象 | 核对方法 |
| --- | --- |
| 缺少 Webhook，启动失败 | 在设置页补齐并保存。排查时使用脱敏信息。 |
| 同名文件仍在，跳过本书 | 本轮继续处理其他书。提示中的占用路径可定位 `.rewrite-processing`、配音输入、`.voice-processing` 或完成目录里的文件。旧成品回收日志记录原路径和回收位置。排查时保留任务 ID、时间、日志和事务文件。 |
| 已改完成有 JSON，但等待配音为 0 | 默认配音只扫描 TXT；`.preparation.json` 是清理/备份/映射记录，`.semantic-review.json` 是审改/风险报告。`.rewrite-queue-names/*.json` 是任务原名关联，回执是防重/恢复依据；不能一概删除。 |
| Edge 或本地 LuxTTS 失败 | 看 `app/voice_monitor.log` 和所选服务。`edge_then_lux` 会在 Edge 失败后尝试本地合成。选择“仅测试 Edge”或“试听 LuxTTS”核对对应引擎。 |
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
