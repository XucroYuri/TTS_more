# TTS More + ComfyUI 理论可用性验证

日期：2026-07-31

## 结论

在不连接真实 GPU、ComfyUI、TTS-Audio-Suite 和模型资源实例的前提下，当前代码已经具备“TTS More 负责编排，ComfyUI 负责任务队列和音频生成”的闭环基础。

TTS More 侧保留的核心职责是：剧本和台词数据管理、角色音色绑定、批量生成任务创建、服务路由、生成历史和前端状态展示。音频生成职责被收敛到 ComfyUI HTTP API，TTS-Audio-Suite custom node 负责具体 GPT-SoVITS、IndexTTS、CosyVoice 工作流节点和模型资源。

## 已验证链路

1. 服务合同

   `build_service_client()` 已识别 `comfyui-tts-audio-suite-v1`，并把这类端点路由到 `ComfyUITTSClient`。保留 `comfyui-tts-v1` 是为了兼容主线已有配置。

2. 工作流提交

   `ComfyUITTSClient` 会构建 TTS-Audio-Suite 工作流，调用 ComfyUI `/prompt` 提交任务，轮询 `/history/{prompt_id}`，再通过 `/view` 下载输出音频。

3. 资源与音色

   `resource_id` 已作为服务默认参数或角色绑定参数参与 load signature 和 cluster key，角色配置中的 `resource_id`、参考音频、prompt text 等参数会保留到生成任务中。

4. 队列状态

   `SynthesisRequest` 支持进度回调，ComfyUI prompt id/status 会写入 `GenerationQueueItem.external_job_id` 和 `GenerationQueueItem.external_status`。前端队列和台词行会展示 ComfyUI prompt 状态。

5. 默认配置

   `data/services.json` 已改为三个禁用态 ComfyUI 外部端点：GPT-SoVITS、IndexTTS、CosyVoice。用户只需启用并填写实际 `resource_id`、`base_url` 与模型资源配置。

## 验证命令

已通过的理论验证测试：

```bash
.venv/bin/python -m pytest backend/tests/test_comfyui_client.py::TestComfyUITTSClient::test_build_client_via_audio_suite_contract backend/tests/test_comfyui_client.py::TestComfyUITTSClient::test_synthesize_reports_prompt_status_updates backend/tests/test_service_queue.py::test_generation_job_manager_records_external_prompt_status -q
```

这些测试使用 `httpx.MockTransport` 模拟 ComfyUI 的 `/prompt`、`/history/{prompt_id}` 和 `/view`，验证 TTS More 侧的合同、任务提交、状态回传和队列记录。

## 真实实例边界

以下内容没有在本机理论验证中覆盖，必须在有实际 ComfyUI + TTS-Audio-Suite + 模型资源的机器上确认：

- TTS-Audio-Suite 节点类名和输入字段是否与当前 `workflow_builder.py` 完全一致。
- `resources.yaml` 中的 `resource_id` 是否能被插件正确加载和释放。
- 参考音频上传、删除和模型显存释放在真实插件环境中的行为。
- 长批量台词进入 ComfyUI 队列后的吞吐、失败重试和显存回收表现。
- 首次模型下载、离线模型路径和多 GPU `resource_group` 的实际部署细节。

## 遗留风险

仓库中仍保留旧 worker、Gradio、portable package 相关代码和测试，主要用于迁移审计和历史兼容。当前正式方向不依赖这些路径。后续如果要继续减复杂度，应单独开清理分支，删除旧 UI 入口、部署脚本和测试夹具，而不是把这次 ComfyUI 收口和大规模删除混在一起。
