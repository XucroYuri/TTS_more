# TTS More 部署方案

当前正式部署路线是 **TTS More + ComfyUI + TTS-Audio-Suite**：

```text
TTS More（剧本、角色、编排）
        │ HTTP API
        ▼
ComfyUI（工作流与任务队列）
        │ custom node
        ▼
TTS-Audio-Suite（GPT-SoVITS / IndexTTS / CosyVoice 等引擎）
```

GPT-SoVITS、IndexTTS 与 CosyVoice 仓库只提供模型和运行资源；它们不再作为由 TTS More 管理、打包或启动的独立 worker 服务。

## 从零部署

完整的安装、服务端点配置和工作台合成验证步骤见 [ComfyUI TTS 后端接入指南](comfyui-integration.md#从零部署指南)。

部署时只需准备：

1. ComfyUI；
2. `XucroYuri/TTS-Audio-Suite` custom node；
3. TTS More 主线；
4. GPT-SoVITS、IndexTTS、CosyVoice 的模型/数据目录，供插件按其上游约定加载。

## 已停止的路线

Windows 四个独立便携包、`bootstrap`/`full` ZIP、TTS More 对三个上游应用的启动停止和修复控制，均已停止开发与发布。它们不是受支持的部署方式，也不会再由 CI 或 Release 工作流构建。

保留 `repo.lock.json`、部署辅助脚本及三个上游仓库，是为了复用模型资源、进行受控的仓库同步和支持 ComfyUI 插件加载；这不表示恢复旧 worker 或便携包架构。
