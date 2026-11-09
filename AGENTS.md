# 项目维护约定

1. 先读 README.md、HANDOFF.md、docs/releases/2026-10-31-github-verification.md，再核对 git status、分支、HEAD 和远端。当前本分支为 1.0.31；不要用旧 TIMEOFF 历史路径判断源码位置。
2. 权威源码是当前 Git checkout 根目录、novel_monitor/ 和 tools/。发布包、安装目录、业务目录、历史 build/验证副本均不是开发源。
3. 保护用户配置、Key、Webhook、原文/备份、TXT/MP3、日志、队列/处理中、统计和回执；不清登录、不清锁、不重置真实任务、不覆盖未提交改动。同名文件等待，不能改名绕过保护。
4. 新功能或修复先隔离复现和离线回归；有真实接口/任务/发送/恢复/发布副作用时，依据当前明确授权边界执行，不复用历史授权。GitHub 发布不授权 OSS/更新清单/下载站变更。
5. 正式版 1.0.31 与 1.0.31-beta 测试分支保持区分。保留仓库可见性，不将私有测试源码擅自公开；正常推送，不强推，不覆盖已有标签/Release/包。
6. 只提交完整源码、安全模板和脱敏说明。审查当前文件及本次新增历史，禁止密钥、个人/业务数据、浏览器资料和聊天记录进入 Git；禁止 git add -f 绕过保护。
7. 回归：cmd /c run_tests.bat；测试版另运行 python -m unittest test_desktop_shortcut.py。Windows 跨盘 TEMP 的测试夹具问题按 HANDOFF 用同盘临时目录处理。
8. 完成后用真实输出报告版本、源码提交、测试及未验证项；更新 README/HANDOFF 和核验记录，不把本地包、源码推送、GitHub Release、生产升级与真实业务成功混称。

9. GitHub Latest 为旧客户端生产兼容锚点 v1.0.18；新正式程序 1.0.31 使用版本专属 Release 下载。GitHub 补传不要默认设置 Latest，先读 docs/legacy-github-update-compatibility.md，改变旧更新通道须单独授权。
