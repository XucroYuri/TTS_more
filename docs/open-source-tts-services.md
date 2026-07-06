# 开源 TTS 服务接入与混合部署

TTS More 的核心开源 TTS 顺序固定为：

`GPT-SoVITS -> IndexTTS -> CosyVoice -> TTS API`

TTS API 目前是占位入口，产品重点先放在 GPT-SoVITS、IndexTTS、CosyVoice 三个开源服务。

## 安装入口

先部署 TTS More：

```powershell
git clone https://github.com/XucroYuri/TTS_more.git
```

如果本机没有可用 TTS 服务，可以按需克隆以下项目。推荐放在 `repo/`，也可以放在任意路径后通过接入向导绑定。

```powershell
git clone https://github.com/XucroYuri/GPT-SoVITS.git repo/GPT-SoVITS
git clone https://github.com/XucroYuri/index-tts.git repo/index-tts
git clone https://github.com/XucroYuri/CosyVoice.git repo/CosyVoice
```

这些 fork 作为稳定镜像使用。只要 HTTP contract 兼容，也可以接入官方源头仓库或已经部署好的服务。

## 接入方式

在工作台打开 `服务与资源 -> 开源接入`，选择一个开源 provider 后按步骤配置：

1. 本机项目路径：绑定 repo path，并可配置启动、停止、日志和资源诊断。
2. 本机端口：服务已经在本机运行，只填写 `base_url` 与协议。
3. 局域网端点：使用 IP + 端口接入可信局域网服务。
4. 公网 URL：使用公网 URL 接入云端服务。

无论哪种方式，生成请求都只通过 HTTP endpoint 发起。本机项目路径只用于管理和诊断，不作为推理调用路径。

配置保存到 `data/local/services.json`。不要把本机路径、局域网 IP、生成音频或真实角色配置写入 `data/templates/`。

## 状态判定

服务状态分为：

- `not_configured`：尚未配置。
- `repo_missing`：配置了本机项目路径，但目录不存在。
- `repo_found`：项目目录存在，但 endpoint 还未确认可用。
- `endpoint_unreachable`：端点不可达。
- `partial`：部分可用，例如端口可达但能力或协议未完全确认。
- `ready`：端点与协议检测通过，可进入生成候选。

生成界面的服务下拉只显示当前 provider 下可解释的 `ready` 或 `partial` 端点。`blocked`、`disabled`、`repo_missing` 等状态只在服务管理面板中展示，不进入生成候选。

## 混合部署

理想部署方式是本机、局域网、云端混合使用：

- 高频使用或需要本机文件资源的服务部署在本机。
- 局域网机器通过独立 IP + 端口提供额外 GPU。
- 云端服务通过公网 URL 接入，适合高并发或远程资源。

每个 endpoint 都声明 `resource_group` 和 `capacity`。同一资源组按容量限制执行，不同资源组可以并行执行。

示例：

- `local-gpu-0 capacity=1`：本机单卡，GPT-SoVITS、IndexTTS、CosyVoice 串行。
- `lan-192-168-2-166-gpu capacity=1`：局域网机器独立执行。
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

## 本机托管服务

本机托管服务只有在满足以下条件时才允许由 TTS More 启停：

- `source_profile = local_repo`
- `managed = true`
- 已配置 start command 和工作目录

局域网和公网 endpoint 不提供远程进程控制，只做 health、capability、load state 轮询。

## 发布安全

提交到 GitHub/Gitee 前确认：

- `data/local/` 不提交。
- `.env.local` 不提交。
- `repo/` 不提交。
- 生成音频、模型权重、manifest 运行历史不提交。
- `data/templates/services.example.json` 只保留脱敏模板。
