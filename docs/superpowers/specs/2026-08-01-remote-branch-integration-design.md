# TTS More 远端能力整合与发布闭环设计

## 目标

在已经完成本机真实验证的 `TTS More -> ComfyUI -> TTS-Audio-Suite` 链路上，吸收远端已经成熟的能力和必要修复，同时保留 GPT-SoVITS、IndexTTS、CosyVoice 三个目标引擎的可运行基线。最终通过两个可干预仓库各自的 Pull Request 完成审查、CI、合并，并让 TTS More 的本地、GitHub、Gitee `master` 指向同一提交。

## 已确认的仓库状态

### TTS More

- 发布目标为 `XucroYuri/TTS_more:master`。
- 当前 `dev-xu/comfyui-live-validation` 已包含最新 GitHub `master`，不需要再次合并主分支。
- 旧 PR #27 `chore/comfyui-cleanup` 与主线冲突且 CI 失败，不整体合并；只提取仍符合当前架构的部署示例、环境变量说明、路径清理和确定性测试意图。

### TTS-Audio-Suite

- 本地 `main` 包含 15 个尚未进入 fork 远端主分支的 TTS More Bridge 与三引擎修复提交。
- 官方 `upstream/main` 已到 5.6.2，包含 43 个本地尚未吸收的提交，并与本地改动在 12 个核心文件上重叠。
- fork 分支 `fix/ffmpeg-toolchain-and-tts-paths` 提供 FFmpeg/FFprobe 工具链检查及统一 TTS 模型目录两个修复，需要在合并上游后移植并补充回归测试。
- Audio8 与 VoxCPM 仍为上游 Draft PR，已知缺少完整运行、依赖或硬件覆盖，不进入本轮稳定发布。

## 采用的整合方案

### 1. TTS-Audio-Suite 先行整合

从当前已验证的本地 `main` 创建 `dev-xu/upstream-5.6.2-tts-more-integration`：

1. 合并 `upstream/main` 5.6.2，人工解决重叠文件。
2. 保留 API Bridge、外部资源注册、`tts_more_targets` 安装配置以及 GPT-SoVITS、IndexTTS、CosyVoice 适配器。
3. 让 DramaBox、ChatterBox v3 等已进入上游主线的能力随 5.6.2 一同保留，但不得进入 `tts_more_targets` 的最小依赖和节点集合。
4. 移植 fork 的 FFmpeg 工具链检测和统一 TTS 模型路径修复。
5. 为工具发现、模型路径、目标安装配置和 Bridge 注册补充确定性测试。
6. 运行插件单元测试、安装配置验证、ComfyUI 节点注册和三引擎真实回归。
7. 推送、创建面向 fork `main` 的 ready PR、等待或修复 CI 后合并。

### 2. TTS More 选择性整合

在现有 `dev-xu/comfyui-live-validation` 上：

1. 手工吸收 PR #27 中仍有效的公开配置模板、环境变量文档和硬编码路径防护。
2. 保留当前 `resource_id` 强制契约、单 GPU `capacity=1`、私有资源注册表和默认 `127.0.0.1` 的部署规则。
3. 不恢复会在普通 CI 中直接访问本机 ComfyUI 的测试；真实 E2E 必须显式启用，确定性契约测试继续独立运行。
4. 更新本机验证报告，记录实际采用的插件合并提交和远端能力评估结果。
5. 在插件 fork 主线合并后，重新执行 TTS More 后端、前端、发布治理和三引擎真实回归。
6. 推送、创建面向 GitHub `master` 的 ready PR、处理 CI、合并。

### 3. 暂缓能力

- Audio8：保留远端 PR、文件范围、依赖和测试状态的评估记录；上游转为 ready 且能通过目标运行环境验证后再单独整合。
- VoxCPM：推理与 LoRA 训练改动规模大，并且 Draft PR 尚无完整状态检查；未来使用独立安装配置和独立 PR，避免污染当前三引擎环境。

暂缓不是忽略：本轮报告必须写明对应远端分支、当前阻塞和重新评估门槛。

## 冲突处理原则

| 冲突区域 | 处理原则 |
|---|---|
| `nodes.py`、统一节点注册 | 以上游 5.6.2 结构为骨架，重新挂接 Bridge 节点；目标安装配置只暴露三引擎与 Bridge 必要节点。 |
| `requirements.txt`、`install.py` | 保留上游通用安装，同时保留 `TTS_AUDIO_SUITE_INSTALL_PROFILE=tts_more_targets` 的最小依赖路径。 |
| 引擎注册表和统一接口 | 保留上游新引擎元数据，不丢失三个外部适配器；按配置控制导入，避免可选依赖拖垮启动。 |
| 音频缓存与文本参数 | 优先采用上游修复，再用现有 Bridge/三引擎测试证明兼容性。 |
| TTS More 部署文档 | 以当前 ComfyUI 架构为准，只移植仍有效且不泄露本机路径的内容。 |

## 验证分层

1. **静态边界**：检查官方 ComfyUI 和三个模型项目无源码改动；检查公开文件无私有绝对路径。
2. **插件确定性测试**：Bridge、资源注册、节点映射、目标安装配置、FFmpeg 工具发现、TTS 路径解析。
3. **插件安装与导入**：在现有 ComfyUI Python 中执行 `tts_more_targets`，通过 `pip check`、`/object_info` 和 capabilities。
4. **TTS More 确定性测试**：后端全量 pytest、前端测试和构建、发布治理测试。
5. **真实三引擎回归**：每个引擎至少完成 prompt、非空非静音 WAV、TTS More 可见结果、释放和第二次请求。
6. **托管验证**：两个 PR 均需通过可用 CI；没有托管检查时必须记录并以全量本机验证作为合并门槛。

健康检查、节点导入或 capabilities 单独成功不能替代真实音频验证。

## 发布顺序

```text
TTS-Audio-Suite integration branch
  -> local/plugin/live validation
  -> fork PR to main
  -> merge
  -> TTS More final regression and documentation
  -> TTS More PR to GitHub master
  -> merge
  -> fast-forward local master
  -> push GitHub master to Gitee master
  -> verify local/GitHub/Gitee SHA parity
```

插件 PR 必须先合并，因为 TTS More 的最终证据需要引用一个可复现的插件主线提交。

## 完成标准

- TTS-Audio-Suite fork `main` 包含上游 5.6.2、本地 Bridge/三引擎工作和两个 fork 修复。
- Audio8/VoxCPM 的暂缓理由与重新进入条件有文档证据。
- 三引擎的确定性测试和本机真实回归结果没有因整合退化。
- 两个 ready PR 已审查、CI 通过或有明确的本机替代门槛，并已合并。
- TTS More 本地 `master`、GitHub `master`、Gitee `master` 的完整 SHA 一致。
- 官方 ComfyUI 与三个模型仓库没有本轮源码提交或未提交改动。

## 回退策略

- 所有整合只发生在命名功能分支；主分支只通过 PR 更新。
- 上游合并和两个 fork 修复使用独立提交，便于定位和回退。
- 若 5.6.2 无法在不修改官方 ComfyUI/模型项目的前提下恢复三引擎，通过失败证据阻止 PR 合并，而不是降低真实验证标准。
- 只有在两个主分支和三端 SHA 完成验证后，才删除临时分支或工作树。
