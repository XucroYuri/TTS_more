# GPT-SoVITS Fork 能力契约

这份文档给维护 `https://github.com/XucroYuri/GPT-SoVITS` fork 的 Agent 使用。它不是补丁，不提供旧行号，也不要求照抄实现。动手前必须先在 GPT-SoVITS 仓库里重新搜索当前文件、函数和端点。

目标只有一个：让 TTS More 能稳定调用 GPT-SoVITS，获取训练音色、权重、参考音频和参考文本，并在真实推理失败时给出明确诊断。

## 不变约束

- 保持 GPT-SoVITS 现有推理 API 向后兼容。
- 所有新增路径必须做目录穿越防护，不返回绝对本机路径给前端。
- 不泄露用户本机目录、局域网地址、API Key、模型权重内容或真实音频内容。
- 真实调用失败就失败，不用 mock 或空音频伪装成功。
- 参考音频时长超出推荐范围时只做软警告，不直接阻断合法推理。
- 修改后必须能被 TTS More 的 endpoint 检测、模型目录读取和真实生成流程解释。

## TTS More 当前需要什么

TTS More 支持两条 GPT-SoVITS 接入路径。

### 路径 A：Gradio WebUI

这是日常首选路径。工作台在 `接入` 中保存一个已经启动的 Gradio endpoint，api contract 为：

```text
gradio-gpt-sovits-webui
```

最低要求：

- `/config` 可访问。
- Gradio dependency 中存在 `api_name = "get_tts_wav"`。
- `get_tts_wav` 能返回可下载或可读取的音频结果。

增强能力：

- 如果 WebUI 暴露 `change_gpt_weights`，TTS More 会先切 GPT 权重。
- 如果 WebUI 暴露 `change_sovits_weights`，TTS More 会先切 SoVITS 权重。
- 如果 WebUI 暴露 `on_select_ref_audio`，TTS More 可用 logs 下拉项反查参考音频和参考文本。
- 如果 WebUI 暴露模型和参考音频相关组件，TTS More 会从 Gradio config 中读取候选项；没有 `api_name` 时会按组件 label 做降级识别。

Gradio 路径不要要求 TTS More 知道 GPT-SoVITS 的本机 repo path。TTS More 只需要 endpoint、api_name、组件结构和生成结果。

### 路径 B：API v2

API v2 用于更清晰地暴露模型目录和训练样本。api contract 为：

```text
gpt-sovits-api-v2
```

建议端点：

```text
GET /models
GET /models/{model_name}/samples
GET /status
POST /upload_ref
```

`/models` 返回训练角色或 logs 目录：

```json
{
  "models": [
    {
      "name": "demo-hero-logs",
      "gpt_weights": ["GPT_weights/demo-hero-logs-e40.ckpt"],
      "sovits_weights": ["SoVITS_weights/demo-hero-logs_e24_s264.pth"],
      "has_training_data": true,
      "sample_count": 1
    }
  ]
}
```

`/models/{model_name}/samples` 返回训练样本：

```json
{
  "model_name": "demo-hero-logs",
  "samples": [
    {
      "audio_name": "hero_001.wav",
      "audio_path": "logs/demo-hero-logs/5-wav32k/hero_001.wav",
      "text": "不好！",
      "lang": "zh"
    }
  ],
  "total": 1
}
```

路径约束：

- `model_name` 必须安全解析到 `logs/<model_name>` 下。
- `audio_path` 返回服务端可访问的相对路径，不返回绝对路径。
- 只返回实际存在的音频文件。
- `2-name2text.txt` 按 tab 分隔读取，优先使用第 4 列作为参考文本。
- 文件不存在时返回空列表和诊断，不抛出不透明异常。

## 权重和训练数据约定

GPT-SoVITS 常见结构：

```text
logs/<logs_name>/
  2-name2text.txt
  5-wav32k/*.wav
GPT_weights*/<logs_name>-e<epoch>.ckpt
SoVITS_weights*/<logs_name>_e<epoch>_s<step>.pth
```

推荐匹配规则：

- GPT 权重名去掉 `-e<数字>` 及其后缀得到 `logs_name`。
- SoVITS 权重名去掉 `_e<数字>_s<数字>` 及其后缀得到 `logs_name`。
- 同名 logs、GPT 权重和 SoVITS 权重可以合并成一个候选。
- 候选排序应稳定；同一 logs 下推荐最新或最高 epoch/step 的权重。

`2-name2text.txt` 读取规则：

```text
wav_name<TAB>phones<TAB>word2ph<TAB>norm_text
```

TTS More 只需要：

- `wav_name`
- 对应音频路径
- `norm_text`
- 语言，无法判断时可默认 `zh`

## 参考音频时长

不要把 3 到 10 秒写成硬阻断。推荐做法：

- 小于 3 秒或大于 10 秒：提示“可能影响质量”。
- 继续执行推理流程。
- 只有文件不可读、格式不支持、解码失败或推理过程真实失败时才返回错误。

原因：TTS More 会批量调度任务。过硬的时长校验会把本可生成的任务误判为配置错误，也会让用户误以为 endpoint 不可用。

## Gradio WebUI 可选增强

如果要改 WebUI，新增能力应该独立于现有组件，不破坏旧 UI：

- 训练角色下拉：来自 logs 目录。
- 样本下拉：来自 `logs/<name>/5-wav32k` 和 `2-name2text.txt`。
- 样本试听：播放服务端可访问的参考音频。
- 应用样本：把参考音频和参考文本填入现有输入。
- 自动匹配权重：根据 logs 名选择 GPT 和 SoVITS 权重。

这些增强是为了方便 GPT-SoVITS 原生 WebUI 用户。TTS More 不依赖这些 UI 文案，只依赖 endpoint 和可解释的 Gradio/API contract。

## TTS More 侧验收

在 TTS More 仓库中，相关验证至少包括：

```bash
.venv/bin/python -m pytest backend/tests/test_services.py -q
.venv/bin/python -m pytest backend/tests/test_api.py -q
(cd frontend && pnpm test)
```

真实环境验收：

```bash
TTS_MORE_SERVICE_MODE=real TTS_MORE_RUN_REAL_TTS=1 \
  .venv/bin/python -m pytest backend/tests/test_real_tts_validation.py -q
```

验收重点：

- `gradio-gpt-sovits-webui` endpoint 检测能识别 `get_tts_wav`。
- Gradio 生成能上传或传递参考音频。
- 权重切换后不会立即用空结果冒充成功。
- API v2 `/models` 和 `/samples` 能返回模型目录和训练样本。
- 返回路径是相对路径或 endpoint 可访问路径。
- 参考音频超出推荐时长只警告，不阻断。
- 失败时错误信息能指向 endpoint、api_name、权重、参考音频或模型加载问题。

## 交付说明

提交 GPT-SoVITS fork 时，请在 PR 中说明：

- 采用的是 Gradio 增强、API v2 增强，还是两者都有。
- 新增或变更的 endpoint。
- 路径安全处理方式。
- 参考音频时长策略。
- 用 TTS More 跑过的检测或真实生成证据。

不要在 PR 或文档里粘贴本机绝对路径、真实局域网地址、私有音频文件名或 API Key。
