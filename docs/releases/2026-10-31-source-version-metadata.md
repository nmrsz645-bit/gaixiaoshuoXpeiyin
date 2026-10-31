# 正式源码版本元数据修正（1.0.30）

## 实际读取链路

- 源码运行时 `runtime.BASE_DIR` 为源码根；`unified_app.read_app_version` 读取该目录的 `version.json`，用于 `app_window_title`，文件缺失或损坏时返回 `DEFAULT_APP_VERSION`。
- 旧便捷入口 `构建桌面版.bat` 会把根 `version.json` 复制到生成的程序目录，所以它不是闲置文件。
- `tools/build_github_release.py --version 1.0.30` 在构建程序目录写入显式传入的版本；原 Windows 包的实际 `app/version.json` 一直是 1.0.30。
- 更新器通过产品配置 `versionFile=version.json` 读取目标安装目录的版本；它不读取 GitHub 仓库或开发源码目录。

## 当前分支与不可变标签

正式源码的根 `version.json`、显示回退值与默认窗口标题统一为 1.0.30。只改这一项显示元数据、根版本文件及测试/说明，未改 AI、审改、发送、队列或配音算法。

原 v1.0.30 标签固定在 `72268308babcb1f81ee17af760ffdda237c80343`，保留首次源码快照（当时根开发元数据为 1.0.26）；当前维护分支在其后正常追加修正。没有重打标签、没有重新构建或替换任何已发布 EXE/ZIP，没有改变客户端或生产清单。

测试线仍为私有仓库 `feature/ui-installer-1.0.31`，根版本/显示回退均为 1.0.31-beta；标签 v1.0.31-beta 仍指向 `3ce48276c8793701d37f66dedd1b6fa3e2ecdfc1`。

## 验证与源码/包对应边界

- 先将版本测试改为正式 1.0.30 要求，在旧显示回退 1.0.26 上复现失败；修正后，正常文件、自定义文件、损坏 JSON 和缺失文件的读取/回退测试通过，完整 409 项通过。
- 当前应用 Python 源码相对原正式包的对应源码，仅 `DEFAULT_APP_VERSION` 的 1.0.26 → 1.0.30 字面值有差异；移除这项明确声明的元数据差异后 AST 相同。原标签源码与原程序包的 22 模块核对记录仍保留，不能把它误说成新维护源码与旧包逐字节一致。
- 最新维护分支/镜像的 GitHub 回归必须按实际 Actions 结果复核。开发和后续修改取最新 main，复验原包则使用原标签/构建源及其原始哈希；不要用同版本重建包覆盖已发布文件。
- 更新器源码缺口仍在 [docs/updater-dependency.md](../updater-dependency.md)，不是根 UPDATER-DEPENDENCY.md；大写文件名仅为 Release 附件名称，两个位置不可混淆。
