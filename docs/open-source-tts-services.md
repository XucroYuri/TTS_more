# 开源 TTS 服务接入与混合部署

TTS More 的核心开源 TTS 顺序固定为：

`GPT-SoVITS -> IndexTTS -> CosyVoice -> TTS API`

TTS API 目前是占位入口，产品重点先放在 GPT-SoVITS、IndexTTS、CosyVoice 三个开源服务。

## 安装入口

先部署 TTS More：

```powershell
git clone https://github.com/XucroYuri/TTS_more.git
```

如果本机或局域网还没有可用 TTS 服务，可以按需部署以下项目，并启动它们各自的推理 WebUI。

```powershell
git clone https://github.com/XucroYuri/GPT-SoVITS.git repo/GPT-SoVITS
git clone https://github.com/XucroYuri/index-tts.git repo/index-tts
git clone https://github.com/XucroYuri/CosyVoice.git repo/CosyVoice
```

这些 fork 作为稳定镜像使用。TTS More 不再绑定本机 repo 路径，也不托管启动进程；只接入已经运行的 Gradio WebUI。

## 接入方式

在工作台打开 `服务与资源 -> 开源接入`，选择一个开源 provider 后只需要配置一个字段：

```text
Gradio WebUI 地址，例如 http://tts-webui.local:9872
```

`127.0.0.1` 和 `localhost` 仍然兼容。TTS More 会根据 URL 自动标记为本机端点、局域网端点或公网端点，并固定使用对应 provider 的 Gradio contract：

- GPT-SoVITS：`gradio-gpt-sovits-webui`
- IndexTTS：`gradio-indextts2-webui`
- CosyVoice：`gradio-cosyvoice-webui`

配置保存到 `data/local/services.json`。不要把局域网 IP、生成音频或真实角色配置写入 `data/templates/`。

## 状态判定

服务状态分为：

- `not_configured`：尚未配置。
- `endpoint_unreachable`：端点不可达。
- `partial`：部分可用，例如端口可达但能力或协议未完全确认。
- `ready`：端点与协议检测通过，可进入生成候选。

生成界面的服务下拉只显示当前 provider 下可解释的 `ready` 或 `partial` 端点。`blocked`、`disabled` 等状态只在服务管理面板中展示，不进入生成候选。

## 混合部署

理想部署方式是本机、局域网、云端混合使用：

- 高频使用或需要本机文件资源的服务可在本机启动 WebUI 后用 `127.0.0.1` 接入。
- 局域网机器通过内部 DNS 名称或手动输入的 endpoint 提供额外 GPU。
- 云端服务通过公网 URL 接入，适合高并发或远程资源。

每个 endpoint 都声明 `resource_group` 和 `capacity`。同一资源组按容量限制执行，不同资源组可以并行执行。

示例：

- `gradio-gpu-0 capacity=1`：本机或一台局域网机器上三个 WebUI 串行。
- `lan-studio-gpu capacity=1`：局域网机器独立执行。
- `cloud-cosyvoice-a10 capacity=2`：云端实例最多并发两个任务。

## 队列调度

队列按 `provider + service_id + cluster_key` 聚类，尽量减少模型反复加载。

GPT-SoVITS cluster key：

```text
service_id + logs + GPT 权重 + SoVITS 权重 + 参考音频 + 参考文本
```

IndexTTS cluster key：

```text
service_id + 参考音频 + 情绪模式 + 情绪来源 + 高级参数
```

CosyVoice cluster key：

```text
service_id + mode + speaker + prompt audio + prompt text + instruct + speed + seed
```

如果当前服务已经加载某个 cluster，队列会优先完成同 cluster 的待执行任务；否则选择待执行数量最多的 cluster，并通过等待时间避免长期饥饿。

## 进程边界

GPT-SoVITS、IndexTTS、CosyVoice 的启动、停止和模型资源管理都由各自 WebUI 负责。TTS More 只保存 endpoint、检测 Gradio `/config`、调用对应 api_name，并把生成音频写回项目历史。

## 发布安全

提交到 GitHub/Gitee 前确认：

- `data/local/` 不提交。
- `.env.local` 不提交。
- `repo/` 不提交。
- 生成音频、模型权重、manifest 运行历史不提交。
- `data/templates/services.example.json` 只保留脱敏模板。
