# IndexTTS 附加脚本

本目录会被顶层部署器复制到 IndexTTS checkout 的 `tts-more/`。复制后的脚本方便节点排障，但**不是完整认证路径**。

正式认证只运行 [单机 Runbook](../../../docs/cuda-e2e-single-node.md) 的 `run-cuda-validation.ps1`；总入口内部调用 `deploy-local-tts.ps1`。直接运行 `deploy-local-tts.ps1` 仅用于通用部署或排障，不是完整认证路径；不要在认证总入口前先运行。顶层流程会安装指定 CU128 runtime，并准备 w2v-bert、BigVGAN、semantic codec 和 CampPlus 等辅助资源。

受控手工排障时，在复制后的 `tts-more` 目录运行：

```powershell
$env:TTS_MORE_MODEL_SOURCE = "Auto"
.\tts-more-prepare.ps1
```

该命令只覆盖上游基础准备；缺少顶层辅助资源和 doctor 证据时不得认证。
