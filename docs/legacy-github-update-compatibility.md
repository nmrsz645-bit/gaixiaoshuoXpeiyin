# GitHub Latest 兼容入口与最新程序版本

最新正式程序为 **1.0.31**，下载使用指定 [v1.0.31 Release](https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin/releases/tag/v1.0.31)。**GitHub Latest 标记保留给 v1.0.18 旧客户端更新兼容入口，不能据 Latest 标记判断当前程序最新版。**

## 已核实依据

旧 v1.0.18 的配置（提交 `b5276725c555bc5792325ddfc8046e427c449d6c`，updater-patch/updater-config.json）使用：

`https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin/releases/latest/download/latest.json`

新 v1.0.30 GitHub Release 没有生产 latest.json。将其设为 Latest 会改变旧客户端清单入口；本轮授权仅补齐 GitHub，没有授权迁移该生产更新通道。因此 Latest 已恢复原 v1.0.18，其原 latest.json 文件未改、未复制到新 Release、未替换。

回读兼容入口返回原始 v1.0.18 清单，SHA-256 `42307b53a2e98371574e9791fdfca12c98e0e43c537334b4b8669be8d269225d`，123963 字节。v1.0.30 仍为已发布正式版（非 prerelease），可使用版本专属链接下载；v1.0.31-beta 仍为私有预发布且不设 Latest。

## 后续发布边界

- 后续 GitHub 正式包补传先使用 `--latest=false`，保持上述旧入口不变；不要默认把较大版本设为 Latest。
- 若确需迁移旧客户端 GitHub 更新通道，必须单独获得生产授权，核清旧客户端版本/配置、更新清单/载荷兼容性、升级回滚及数据保护，再执行。
- 1.0.19 及之后使用现有 OSS 通道；本轮 OSS 清单、程序文件和下载站未改。
- 根 version.json 与显示回退仍为当前正式 1.0.31；旧客户端兼容标记与程序版本、不可变包标签及最新维护提交必须分开记录。
