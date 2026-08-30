# 项目接手约定

1. 先完整阅读 `TIMEOFF.md`、`README.md`，再查看 `git status --short`。
2. 权威源码仅为本项目根目录和 `novel_monitor/`；不要修改 `E:\自动化\改小说` 旧项目，也不要把 `发布版-*`、`发布候选-*`、`隔离验证-*` 当作源码。
3. 不得读取、提交、复制、覆盖或删除用户配置、密钥、Webhook、规则、小说、音频、日志、统计、回执、失败队列或输出目录。它们均由 `.gitignore` 排除。
4. 任何功能改动先完成离线测试；涉及真实接口、构建、OSS 发布、更新清单或通知用户，须获得用户明确授权。
5. 正式发布必须使用新版本号和新隔离目录，递归审计包内容，真实验证升级/回滚、`app.previous`、用户数据保持和公网哈希回读；不得覆盖既有发布包或同版本清单。
6. 当前线上版本为 `1.0.15`。其新增规则：只对新生成、超过 59 分钟的 MP3 保留前 35 分钟；历史成品不回头处理。

## 必做检查

```powershell
python -m py_compile unified_app.py voice_monitor.py test_pipeline.py
python -m unittest test_pipeline.py
```

当前完整回归基线为 66 项测试通过。其他具体证据、已知边界和下一步以 `TIMEOFF.md` 为准。

`运行回归测试.bat` 与 `.github/workflows/offline-regression.yml` 只运行离线测试。不要把真实接口、OSS 发布或收费配音加入 CI。
