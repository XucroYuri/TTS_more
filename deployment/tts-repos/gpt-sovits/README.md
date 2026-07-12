# GPT-SoVITS 附加脚本

本目录会被顶层部署器复制到 GPT-SoVITS checkout 的 `tts-more/`。复制后的脚本方便节点排障，但**不是完整认证路径**。

正式认证只运行 [单机 Runbook](../../../docs/cuda-e2e-single-node.md) 的 `run-cuda-validation.ps1`；总入口内部调用 `deploy-local-tts.ps1`。直接运行 `deploy-local-tts.ps1` 仅用于通用部署或排障，不是完整认证路径；不要在认证总入口前先运行。顶层部署会核对根 `repo.lock.json`、要求 conda、先安装 `torchcodec==0.13`，再调用上游安装器并验证 CU128 runtime。

受控手工排障时，在复制后的 `tts-more` 目录运行：

```powershell
$env:TTS_MORE_DEVICE = "CU128"
$env:TTS_MORE_MODEL_SOURCE = "Auto"
.\tts-more-prepare.ps1
```

该命令成功不等于认证通过；仍需顶层 doctor、worker 契约、核心 CUDA、Playwright 和人工听审证据。
