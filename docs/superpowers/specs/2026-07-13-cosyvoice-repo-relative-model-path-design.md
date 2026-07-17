# CosyVoice 可迁移模型路径生成设计

## 背景

TTS More 通过 `local-cosyvoice` 服务启动 CosyVoice Worker 后，基础健康检查和 OpenAPI 均正常，但 `/load` 返回 500。日志证明 Worker 实际收到的模型路径是：

```text
F:\Code\Github\TTS_more\pretrained_models\CosyVoice-300M
```

真实模型位于：

```text
F:\Code\Github\TTS_more\repo\CosyVoice\pretrained_models\CosyVoice-300M
```

`repo.lock.json` 中的 `model_dir` 按语义是相对于 CosyVoice 仓库根目录的路径；部署配置生成器却直接把它写入以 TTS More 项目根目录为基准的 Worker 环境。监督器随后将 `_DIR` 相对路径绝对化，最终得到错误位置。

## 目标

- 保持 `repo.lock.json` 与具体电脑安装盘符无关。
- 将 CosyVoice 仓库内相对模型路径转换成 TTS More 项目根目录相对路径。
- 支持 `repo/CosyVoice` 之外的自定义 CosyVoice 仓库目录。
- 用户明确配置绝对模型目录时保持原值，不强制模型位于仓库内部。
- 修复默认配置、模板和本机可重建配置，并完成真实 GPU 推理验证。

## 非目标

- 不修改 IndexTTS 或 GPT-SoVITS 的路径规则。
- 不为当前电脑写入固定的 `F:` 盘绝对路径。
- 不改变 `repo.lock.json` 中 `model_dir` 的“相对仓库根目录”语义。
- 不修改 ServiceSupervisor 对通用 `_DIR` 环境变量的项目根目录解析规则。
- 不改变 CosyVoice Worker 的模型加载或推理接口。

## 方案

采用部署生成器源头修复。

`scripts/tts_more_deploy.py::_worker_env` 在生成 CosyVoice 环境时执行以下规则：

1. 读取仓库路径，例如 `repo/CosyVoice`。
2. 读取仓库内模型路径，例如 `pretrained_models/CosyVoice-300M`。
3. 若模型路径是目标平台的绝对路径，则保持原值。
4. 若模型路径是相对路径，则生成规范化路径：

```text
repo/CosyVoice/pretrained_models/CosyVoice-300M
```

最终 ServiceSupervisor 仍负责把该项目根目录相对路径解析为当前电脑上的绝对路径。这样安装根目录可以在任意盘符或目录，配置仍可迁移。

## 配置数据流

```text
repo.lock.json
  repo.path = repo/CosyVoice
  repo.model_dir = pretrained_models/CosyVoice-300M
        |
        v
tts_more_deploy.py render-services
  TTS_MORE_COSYVOICE_MODEL_DIR =
  repo/CosyVoice/pretrained_models/CosyVoice-300M
        |
        v
ServiceSupervisor(project_root=<当前安装根目录>)
        |
        v
<当前安装根目录>/repo/CosyVoice/pretrained_models/CosyVoice-300M
```

## 文件范围

- 修改 `scripts/tts_more_deploy.py`：生成 CosyVoice 项目根目录相对模型路径。
- 修改 `backend/tests/test_deploy_tool.py`：覆盖默认仓库、自定义仓库和绝对模型目录。
- 刷新 `data/services.json`：提交的默认服务配置。
- 刷新 `data/templates/services.example.json`：用户复制模板。
- 修改 `.env.example` 中的示例，明确 TTS More 项目根目录语义。
- 刷新忽略的 `data/local/services.json`，供当前运行实例验证。

`repo.lock.json` 保持不变，因为其值已经正确表达仓库内相对路径。

## 错误处理

- 空 `model_dir` 使用 `pretrained_models/CosyVoice-300M` 默认值，再与仓库路径组合。
- 目标平台绝对路径保持不变。
- 生成结果仍交给现有 Supervisor 安全边界检查和环境解析。
- 模型目录不存在时，Worker 继续通过 `/load` 返回失败并写入服务日志，不静默下载错误路径。

## 测试与验证

按 TDD 顺序实施：

1. 修改现有部署渲染测试，先证明默认 CosyVoice 路径仍错误。
2. 增加自定义仓库路径测试，要求模型路径跟随仓库位置。
3. 增加目标平台绝对模型路径测试，要求原值保持不变。
4. 实现最小生成逻辑并运行部署、监督器和 Worker 相关测试。
5. 重新渲染默认及本机服务配置，检查没有本机固定盘符进入提交文件。
6. 通过 TTS More 停止并重新启动 `local-cosyvoice`。
7. 验证 `/openapi.json`、`/health`、`/status` 和模型路径。
8. 加载 `CosyVoice-300M`，使用仓库自带 `asset/zero_shot_prompt.wav` 进行真实 zero-shot 合成。
9. 下载 Artifact，校验 SHA256、RIFF/WAV 结构、采样率和时长。
10. 卸载模型并确认 GPU 显存回落；Worker 保持由 TTS More 管理并在线。

## 验收标准

- 默认生成配置不包含机器专属绝对路径。
- CosyVoice 模型路径包含配置中的仓库路径。
- 自定义仓库路径和绝对模型目录测试通过。
- `/load` 不再请求不存在的模型 ID。
- CosyVoice 真实音频合成及 Artifact 校验通过。
- IndexTTS 已验证输出和现有 TTS More/GPT 服务不受影响。
