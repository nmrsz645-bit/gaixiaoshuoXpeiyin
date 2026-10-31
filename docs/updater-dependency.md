# UpdateAgent 维护依赖与尚未闭合的源码缺口

更新于 2026-10-31。本仓库提供完整的应用 Python 源码、测试、打包工具，以及沿用的更新器二进制/产品配置；**不声称已取得更新器的原始源码**。

## 实际文件与产品配置

- 固定文件：`updater-patch/UpdateAgent.exe`，26112 字节，SHA-256 `59e3fd575350c274e2134168d7f61c3caca8ac61377570af92e5a559f54d1241`。
- 实际为 .NET Framework 程序，反射类型为 `UpdateManifest`、`UpdateCore`、`UpdateTransaction`、`UpdateAgent`、`UpdateAgent.AgentConfig`、`Program`，不是 PyInstaller 的 Python 程序。
- 小说产品配置在 `updater-patch/updater-config.json`：`appId=xiao-shuo-chu-li-zhong-xin`，`appRoot=..\app`，`versionFile=version.json`，重启入口为小说处理中心.exe。`manifestUrl` 指向该产品原公开 OSS 更新清单；`preservePaths=["*"]`、`excludePaths=["version.json"]` 及 requiredFiles 由现有更新器读取。
- 正式包内部 updater-patch、测试便携包内部 updater-patch 和根 updater 文件均与上述固定 SHA-256 一致；本轮没有替换任何包内或生产 EXE。

## 构建和运行边界

`tools/build_github_release.py` / `tools/build_installer.py` 复用上述二进制和配置，不会编译更新器。它是应用构建的固定依赖，需要 Windows .NET Framework 4.x。稳定版 `unified_app.check_for_updates` 安排更新检查；测试版带 `-beta` 的版本直接跳过。

本仓库能维护/构建应用 Python 逻辑，但**当前不能从已确认的原始源码重建这一个更新器组件**。如果要修改下载、校验、进程停止、替换或回滚实现，必须先找回与固定二进制对应的源码及构建参数，再执行隔离测试；不要用其他项目同名源码替换。

## 本轮核对与排除

- 用户另一个 B 站项目的 C# 源码定义 `ReleaseManifest` / `Updater`，与本文件的类型、接口不同；未复制或修改该项目文件。
- 掌心项目提供的 update_agent.py 及其 UpdateAgent.spec 的 Analysis 入口为 Python，与本项目实际 .NET 程序的格式和类型不相符；没有据文件名认定为同一实现。
- 只读恢复/编译分析不能证明找到了原始构建源，因此不作为源码已闭合的证据，也不替换当前受校验的二进制。
- 本轮包内相同性、离线回归和 GUI 启动不等于新的生产更新/回滚验收；没有执行正式更新器 CLI、停止用户进程或改变 OSS。

这是本轮 GitHub 补齐后仍保留的一项精确缺口，应在任何“全部组件源码齐全”的结论中单独列出。
