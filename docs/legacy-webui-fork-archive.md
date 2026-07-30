# Legacy WebUI/Fork Archive

2026-07-30 起，TTS More 的开源 TTS 主路径切换为：

```text
TTS More -> ComfyUI -> TTS-Audio-Suite -> GPT-SoVITS / IndexTTS / CosyVoice resources
```

旧路线进入归档：

- `tts-more-v1` 本地 worker
- Gradio WebUI 直连 client
- GPT-SoVITS `main` / `dev` / `proplus-hc-dev` 多 fork 收敛规划
- `deployment/tts-repos/*` 中用于把 TTS More 附加脚本复制进模型仓库的部署流程

这些代码和文档暂时保留，用于兼容已有部署、复现旧验证记录、迁移角色配置和排查历史问题。后续默认开发不再围绕这些 fork 扩展模型加载、WebUI API 适配或本地 worker 能力。

新的职责分界：

- TTS More：剧本解析、角色绑定、批量任务编排、ComfyUI workflow JSON 生成、prompt 状态展示、生成历史。
- TTS-Audio-Suite：暴露 `comfyui-tts-audio-suite-v1` support routes，提供 `resource_id` 能力发现、参考音频 asset 管理和外部 TTS engine nodes。
- ComfyUI：官方本体负责任务队列、workflow 执行、`/prompt`、`/history/{prompt_id}`、`/view` 和 `SaveAudio` 输出。

提交态 `data/services.json` 只声明三个 ComfyUI 逻辑端点，共用 `http://127.0.0.1:8188`。如果旧部署仍要使用 worker 或 Gradio，请在 `data/local/services.json` 中显式添加 legacy endpoint，不要把旧 endpoint 重新写回提交模板。
