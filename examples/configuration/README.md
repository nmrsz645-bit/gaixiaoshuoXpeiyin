# 安全配置参考

所有凭据字段为空。程序不加载本目录，也不加载 .env；首次启动生成本地配置后，优先在 GUI 中填写并保存，避免复制示例覆盖已有设置。

- voice-config.example.json 对应 app/config.json：input_dir/output_dir 是配音队列/成品路径；engine 为 edge、aliyun 或 edge_then_aliyun；rate/pitch/volume 使用 Edge 字符串；阿里云 AppKey 和 DPAPI 字段由 GUI 保存，示例不能合成。
- rewrite-config.example.json 对应 app/改写/config.json：outputDir/failedDir 相对改写配置根目录；logDir 是日志位置；stableSeconds/retryIntervalMinutes/maxRetries 为文件稳定/重试参数；deepseekModel/maxRequestChars/requestTimeoutSeconds 为 AI 请求配置；semanticComplianceEnabled 为新领取任务模式。
- app/改写/检测.txt 保存输入目录，app/改写/ai_api_url.txt 保存完整 chat/completions URL；deepseek_api_key.txt 与 wechat_webhook_url.txt 是真实本地凭据文件，不在仓库提供实例。
- 在 DeepSeek/自定义供应商控制台取得本人 Key 和模型；在有管理权的企业微信群机器人设置取得 Webhook；阿里云 AppKey 来自本人智能语音项目、AccessKey 来自已授权账号。具体入口见根 README，不替他人创建凭据、不扩大权限。
- 生成的真实 config.json、.env、凭据、规则、小说和日志均被 .gitignore 排除。示例没有 API Key、Webhook、密文、真实目录、用户名称或业务数据。
