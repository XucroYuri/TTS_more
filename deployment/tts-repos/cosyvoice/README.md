# CosyVoice 附加脚本

本目录会被顶层部署器复制到 CosyVoice checkout 的 `tts-more/`。复制后的脚本方便节点排障，但**不是完整认证路径**。

正式 Windows CUDA 部署必须从 TTS More 根目录运行顶层 `deploy-local-tts.ps1`。顶层流程会处理 `openai-whisper`/setuptools 兼容、避开 requirements 中旧 torch，并安装验证指定 CU128 runtime。完整认证只使用 [单机 Runbook](../../../docs/cuda-e2e-single-node.md)。

受控手工排障时，在复制后的 `tts-more` 目录运行：

```powershell
$env:TTS_MORE_MODEL_SOURCE = "Auto"
$env:TTS_MORE_BASE_PYTHON = "python"
.\tts-more-prepare.ps1
```

该命令成功不等于认证通过；仍需顶层 doctor、核心 CUDA、Playwright 和人工听审证据。
