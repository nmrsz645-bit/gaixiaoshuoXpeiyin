# LuxTTS 完整测试包的来源与许可材料

收集时间：2026-11-18。这里只补充官方原始文本和来源证据，不替换本机已有模型和引擎。

- `LuxTTS-LICENSE.txt`、`LuxTTS-code-README.md`：作者代码仓库，README 声明模型及代码采用 Apache-2.0。
- `LuxTTS-model-README.md`、`LuxTTS-model-metadata.json`：作者 Hugging Face 模型仓库及 API 元数据。元数据修订为 `527f245a276a0eb42ea103a7a512bcfd771eb9b6`。
- `ZipVoice-LICENSE.txt`：LuxTTS 所基于的 ZipVoice 官方代码仓库 Apache-2.0 原文。现有兼容代码中的版权头保留。
- `Whisper-base-model-README.md`、`Whisper-base-model-metadata.json`：OpenAI 在 Hugging Face 的 Whisper Base 模型资料。元数据修订为 `e37978b90ca9030d5170a5c07aadb050351a65bb`。
- `Whisper-upstream-LICENSE.txt`：OpenAI Whisper 原代码仓库的 MIT 许可。Hugging Face 模型卡标记 Apache-2.0，两份原始来源均保留，不将其改成同一种许可。
- `Vocos-LICENSE.txt`：Vocos 官方代码仓库的 MIT 许可。本机依赖包中附带的 Vocos/linacodec 许可也应保留。
- Python、PyTorch、TorchAudio、ONNX Runtime 和其他 Python 组件的随包 LICENSE/NOTICE 位于各自运行库目录，本目录不能替代这些原始材料。

下载地址、SHA-256 和字节数见 `sources.json`；后续可按清单重新取得官方原文并核对，联网结果可能随上游变化。

本机模型身份与官方 API 元数据一致的 SHA-256：

| 本机相对路径 | 官方 LFS SHA-256 |
|---|---|
| `weights/luxtts/model.pt` | `745855037478eb888cfa7a3603c1aa9f663f22a72d94cc1c37787228ff422095` |
| `weights/luxtts/fm_decoder.onnx` | `4510d4f5f049f14ef80207fca695e13c820e2cea61635f402954950bc62b1e3c` |
| `weights/luxtts/text_encoder.onnx` | `495eca2d5f8a911f5c361bcce5bd55cdd2508ccdd26ce3e9bf1d3c29eb974861` |
| `weights/luxtts/vocoder/vocos.bin` | `116b9875a0369d6a0156d752b4548121fe75fdc81d39943e81c46ac9bfa72d11` |
| `weights/whisper-tiny/model.safetensors` | `07cadb9f25677c8d50df603e66a98fbd842cce45047139baeb16e6219a1e807b` |

本机目录虽然名为 `whisper-tiny`，配置 `_name_or_path` 为 `openai/whisper-base`，权重摘要也匹配 Base，不应在交接中标成 Whisper Tiny。

未确认项：本机 LuxTTS/ZipVoice 是已有 Windows 兼容代码，尚未确认该兼容代码最初对应的完整上游提交及逐文件修改历史；本目录不声称完成所有第三方组件的逐项法律审计。最终测试包应另核对解压后文件清单与源运行库哈希一致。
