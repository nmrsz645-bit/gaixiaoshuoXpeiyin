# 小说处理中心

Windows 本地“小说改写 + 自动配音”程序。处理流程为：

`待改小说` → AI 改写 → `已改完成` → Edge/阿里云配音 → `完成`。

## 开发启动

开发和运行环境为 **Windows + Python 3.13**。真实配置和业务文件不会随 Git 迁移。

> 源码已推送到 GitHub `main` 分支。另一台电脑可按下面步骤克隆并接手开发；直接使用仍请优先使用已发布的 1.0.15 完整包。Git 不会迁移真实配置、接口 Key、小说、音频和日志。

```powershell
git clone https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin.git
cd gaixiaoshuoXpeiyin
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe unified_app.py
```

也可运行 `启动小说处理中心.bat`。首次启动后，在“改小说设置”和“配音设置”中填写自己的接口配置；真实 Key、Webhook 和用户配置不属于 Git 仓库。若要迁移业务数据，应在用户明确同意后，单独、安全地复制配置、规则、小说与音频，绝不提交到 Git。

## 测试与构建

```powershell
python -m py_compile unified_app.py voice_monitor.py test_pipeline.py
python -m unittest test_pipeline.py
```

Windows 上可运行 `run_tests.bat`；`运行回归测试.bat` 保留为中文兼容入口。GitHub Actions 会对每次推送和 Pull Request 运行同一套离线回归；它不读取 Key、不调用真实 AI/TTS，也不发布更新。

首次提交并推送后，在 GitHub 的 **Actions** 页面确认 `Offline regression` 绿灯，才算新电脑开发环境可复现。克隆后的首次操作应为 `py -3.13 -m venv .venv`、安装 `requirements.txt`，再运行 `run_tests.bat`；脚本会优先使用 `.venv`。通过后才填写自己的接口配置并启动程序。

构建桌面候选使用 `构建桌面版.bat`。正式发布前必须完整阅读 `TIMEOFF.md`，并遵守其中的隔离构建、用户数据保护、升级回滚和公网回读要求。

## 当前发布状态

线上正式版本为 **1.0.15**：新生成的配音 MP3 超过 59 分钟时，自动保留前 35 分钟。完整发布记录、校验值和未完成事项见 `TIMEOFF.md`。

## 仓库边界

仓库只保存源码、依赖、说明和交接记录。`.gitignore` 已排除用户小说、改写结果、音频、配置、密钥、日志、构建包、隔离验证目录和历史发布目录。不要用 `git add -f` 绕过这些保护规则。
