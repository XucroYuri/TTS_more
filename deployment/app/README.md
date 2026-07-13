# TTS More 应用部署资料

本目录只放应用侧的一键部署资料，与复制到上游 TTS repo 的 `deployment/tts-repos/<provider>/` 附加脚本明确分离。

## Repo 路径确认

本机 checkout 路径与 `repo.lock.json` 不同时，把 `repo-paths.example.json` 复制为 `repo-paths.local.json`，再由人类用户确认路径：

```bash
cp deployment/app/repo-paths.example.json deployment/app/repo-paths.local.json
```

显式传入：

```bash
scripts/deploy-local-tts.sh --repo-paths deployment/app/repo-paths.local.json
```

默认工作流按 `repo.lock.json` 的 `default_selected` 部署 GPT-SoVITS main、IndexTTS 和 CosyVoice。`--targets dev` 只用于 GPT-SoVITS 回归，`--targets all` 才选择所有锁定 repo。路径示例包含全部条目，以支持显式回归。

应用管理的本地 worker 路径必须位于 TTS More 项目根目录内。外部 TTS repo 使用 `deployment/tts-repos/` 下的对应附加脚本手动部署。

## Topology

- `topology.single-windows.example.json`：应用和三个正式 worker 在同一台 Windows CUDA 机器，三服务共享 `cuda-0`。
- `topology.four-node-lan.example.json`：一台应用控制节点和三台独立 GPU worker，限定可信 LAN；四个 host 必须不同且非 loopback，运行时还会检查解析 IP、Windows `MachineGuid` 和三个 GPU UUID 唯一。

复制为 `topology.<name>.local.json` 后填写真实 hostname/IP。本机 topology、`repo-paths.local.json` 和 `data/validation/*.local.json` 都被 git ignore，不得提交。

Validation fixture 的脱敏模板位于 `deployment/validation/fixture.example.json`，真实副本建议放到 `data/validation/cuda-fixture.local.json`。

Windows profile：

```powershell
# 单机
.\scripts\deploy-local-tts.ps1 -Profile local-all -Topology deployment\app\topology.single-windows.local.json -Node gpu-worker

# 应用控制节点
.\scripts\deploy-local-tts.ps1 -Profile app-only -Topology deployment\app\topology.four-node-lan.local.json -Node app-controller

# 独立 GPU worker
.\scripts\deploy-local-tts.ps1 -Profile worker-node -Topology deployment\app\topology.four-node-lan.local.json -Node gpt-worker
```

首次 `single-clean` 和首次 `distributed` 用于建立基线，可省略 `-RequireBaseline`；首轮通过并批准后，所有 `single-release`、分布式回归和 release workflow 都必须要求正数 `performance_baseline.warm_p95_seconds`。

本页只说明部署资料，不提供认证命令。Windows 单机正式路径见 [单机 Runbook](../../docs/cuda-e2e-single-node.md)；字段、唯一服务归属和证据语义见 [CUDA 验证契约](../../docs/cuda-e2e-validation.md)。
