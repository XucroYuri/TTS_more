# TTS More 应用部署资料

本目录只放应用侧的一键部署资料。需要复制到上游 TTS repo 的附加脚本位于 `deployment/tts-repos/<provider>/`，两类文件不要混用。

## Repo 路径确认

所有本机托管命令都要求人类用户确认本次选中的完整 repo 路径，即使路径与 `repo.lock.json` 一致：

```bash
cp deployment/app/repo-paths.example.json deployment/app/repo-paths.local.json
scripts/deploy-local-tts.sh --repo-paths deployment/app/repo-paths.local.json
```

确认文件只接受唯一的正式 `service_id`，并且必须包含本次选择的全部服务。默认按 `default_selected` 部署 GPT-SoVITS main、IndexTTS 和 CosyVoice；`--targets dev` 只用于 GPT-SoVITS 回归，`--targets all` 才选择所有锁定 repo。

应用托管的 checkout 必须位于 `<TTS More>/repo/`，使用仓库自身的 `.git` 目录，且实际 GitHub origin 必须与锁文件一致。符号链接、reparse point、外部 gitdir 和未确认路径会被拒绝。外部 TTS repo 应使用对应的 `deployment/tts-repos/` 附加脚本手动部署。

独立 updater 包含 `tts-more-update.sh`、`tts-more-update.ps1`、`tts-more-update.py` 和 `tts-more-update.json` 四个文件。带 submodule 的 repo 不安装独立 updater，必须由 TTS More 的 `sync-repos` 完成更新。

## Topology

- `topology.single-windows.example.json`：应用和三个正式 worker 位于同一台 Windows CUDA 主机，共享 `cuda-0`。
- `topology.four-node-lan.example.json`：一台应用控制节点和三台独立 GPU worker，限定可信 LAN。

复制为 `topology.<name>.local.json` 后填写真实 hostname 或 IP。运行时会验证 host、解析地址、Windows 机器身份和 GPU 身份；本机 topology、`repo-paths.local.json` 与 `data/validation/*.local.json` 均被忽略，不得提交。

验证 fixture 的脱敏模板位于 `deployment/validation/fixture.example.json`，真实文件建议放在 `data/validation/cuda-fixture.local.json`。

```powershell
# 单机
.\scripts\deploy-local-tts.ps1 -Profile local-all -Topology deployment\app\topology.single-windows.local.json -Node gpu-worker -RepoPaths deployment\app\repo-paths.local.json

# 应用控制节点
.\scripts\deploy-local-tts.ps1 -Profile app-only -Topology deployment\app\topology.four-node-lan.local.json -Node app-controller -RepoPaths deployment\app\repo-paths.local.json

# 独立 GPU worker
.\scripts\deploy-local-tts.ps1 -Profile worker-node -Topology deployment\app\topology.four-node-lan.local.json -Node gpt-worker -RepoPaths deployment\app\repo-paths.local.json
```

首次 `single-clean` 和首次 `distributed` 用于建立基线。基线批准后，`single-release`、分布式回归和发布工作流必须提供正数 `performance_baseline.warm_p95_seconds`。

本页只说明部署资料。正式命令见 [单机 CUDA Runbook](../../docs/cuda-e2e-single-node.md)、[分布式 CUDA Runbook](../../docs/cuda-e2e-distributed.md) 和 [CUDA 验证契约](../../docs/cuda-e2e-validation.md)。
