# LuxTTS 随包模型完整测试版

2026-11-18，北京时间。分支 `codex/lux-voice-20261118`，版本 `1.0.32-lux-test.2`，在 `4a4d200abd724e59b866a124d202402d71b65316` 的语速、默认双参考、私人声音克隆与移除阿里云配音功能上增加完整模型环境打包。未授权发布，本次仅生成隔离测试包。

## 解压与使用

完整包根目录包含 `Start-Test.cmd`、`Test-LuxTTS.cmd`、`test-app`、`lux-runtime`。在 Windows x64 上解压到可写的本地 NTFS 文件夹，保留整个目录结构。无需安装 Python、另下模型或启动破甲。NVIDIA GPU 可用时自动尝试 GPU，失败尝试 CPU；系统显卡驱动仍由电脑提供。

双击 `Start-Test.cmd` 打开程序。配音设置中的 LuxTTS 运行目录留空即使用随包环境；不保存本机自动发现的绝对目录，因此整包移动后仍可使用。主动填写的外部路径继续优先。引擎仍提供 Edge、LuxTTS、Edge 失败转 LuxTTS 三种模式。AI、企业微信和 Edge 仍须使用相应设置与网络，本地 LuxTTS 无需它们。

首次空白启动可能提示缺少 AI Key，关闭提示后可直接试听本地配音。LuxTTS 默认随机选晓伊、晓晓参考音色，同一本书使用同一参考；支持 0.5～2 倍速和导入 MP3/WAV 私人声音。音色听感可能与原生 Edge 不同。

双击 `Test-LuxTTS.cmd` 只使用本地模型分别测试两个默认参考，不加载业务配置，不运行任务、其他接口、守护或更新器。日志、结果和 MP3 写入 `test-app/lux-test-results/时间与随机编号`，完成后打开结果目录。首次模型加载需等待。命令行也可调用：

```powershell
& '.\test-app\小说处理中心.exe' --lux-portable-check
& '.\test-app\小说处理中心.exe' --lux-portable-check --lux-backend cpu --lux-check-voice zh-CN-XiaoyiNeural
```

## 资源来源与打包边界

读取本机 `D:\AILiveRelease\qa-ailive-gpu109-b22c3bee\ailive-independent` 中的 `app/voice/lux_worker.py`、`runtime/lux-tts/{code,deps,gpu-deps,weights}`，及 `runtime/game-ocr/py312` 顶层嵌入式 Python 和 `Library/bin`。源目录保持原位，只复制这些白名单资源。`Lib/site-packages` 属于 OCR，未随包携带；不能遗漏 `Library/bin` 中的 MSVC 运行库。

配音资源解压后约 7.3 GB。随包保留 Python、模型、CPU/GPU 依赖已有许可证，另含 `third_party/lux` 的原始官方许可、模型卡、URL/哈希及模型修订核对记录。Windows 兼容代码的原始上游提交及完整修改历史尚未对应确认，不能宣称全部第三方来源历史已还原。

源程序配置、私人音色、原文、队列、回执、日志、缓存、OCR 和破甲主程序均不加入新包。测试程序沿用版本后缀跳过更新器，交付结构另使用 `test-app`。构建器有相对路径、大小及 SHA-256 清单，可在完整解压后核对。

## 构建

应用环境使用 `requirements-windows-verified.txt`，Python 3.13.15、PyInstaller 6.14.2。运行模型使用随包 Python 3.12、CPU ONNX 及 CUDA PyTorch 环境。第一步只打应用，再使用完整包工具装配：

```powershell
python tools/build_github_release.py --version 1.0.32-lux-test.2 --output <新的应用输出目录>
python tools/build_lux_portable.py --app-archive <应用输出目录/novel-processing-center-1.0.32-lux-test.2-windows-x64.zip> --runtime-source 'D:\AILiveRelease\qa-ailive-gpu109-b22c3bee\ailive-independent' --output <新的完整包输出目录> --guide-file <使用说明文件> --notices-directory third_party/lux
```

使用离线回归以及完整解压后的冻结程序自检验证。后续补充本次真实产物的大小、哈希和 GPU/CPU 结果；它们不能替代另一台电脑的现场验收。
