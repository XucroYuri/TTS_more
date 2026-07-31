# ComfyUI + TTS-Audio-Suite 本机真实验证设计

## 目标

在现有 Windows、RTX 4060 Ti 16GB 和本地模型资产条件下，建立并真实验证以下正式运行链路：

`TTS More -> ComfyUI HTTP API -> TTS-Audio-Suite API Bridge -> 官方 TTS 模型架构`

第一轮工作的重点是先尝试运行，完整记录过程中暴露的问题，区分环境、配置、契约、插件适配和模型兼容性原因，最后形成可重复执行的解决方案。没有产生可播放音频之前，不把理论测试、健康检查或节点成功加载称为真实闭环。

## 干预边界

### 可以修改

1. `XucroYuri/TTS_more`
   - 应用侧服务注册、工作流构造、参考音频传输、状态展示、测试、部署说明和本机验证工具。
2. `XucroYuri/TTS-Audio-Suite`
   - API Bridge、资源注册、外部模型适配器、节点输入契约、运行时隔离、错误信息和插件测试。

### 不修改

1. `Comfy-Org/ComfyUI`
   - 使用官方 `master` 和官方依赖，不提交本地补丁。
   - 若出现兼容问题，优先在 TTS-Audio-Suite fork 中适配；必要时只记录官方问题和最小复现。
2. GPT-SoVITS、IndexTTS、CosyVoice 三个项目
   - 仅作为官方模型代码结构、模型权重和数据目录来源。
   - 不启动它们各自的 WebUI，不部署旧 TTS More worker，也不修改其源码。
   - 若模型结构与插件不兼容，在 TTS-Audio-Suite 适配层解决。

## 采用方案

复用源码版 ComfyUI 和现有独立虚拟环境：

- ComfyUI：`F:\Code\Github\ComfyUI`
- ComfyUI Python：`F:\venvs\comfyui-tts`
- TTS-Audio-Suite fork：`F:\Code\Github\TTS-Audio-Suite`
- 插件挂载：`F:\Code\Github\ComfyUI\custom_nodes\TTS-Audio-Suite`
- TTS More：`F:\Code\Github\TTS_more`

ComfyUI 快进到官方最新 `master`。TTS-Audio-Suite 继续通过现有目录联接挂载，不重复克隆；安装动作是使用同一个 ComfyUI Python 环境同步插件依赖并验证节点注册。

不采用 `J:\ComfyUI-aki-v2` 作为验证基线，因为其第三方节点和启动器环境会增加不相关变量。只有源码版环境被证明无法修复时，才把整合包用于对照实验。

## 本机私有资源注册

创建仓库外的本机私有 `F:\TTS-More\config\tts-audio-suite-resources.yaml`，通过 `TTS_AUDIO_SUITE_RESOURCES` 仅注入 ComfyUI 进程。TTS More 只接触稳定的 `resource_id`，不保存模型绝对路径。

初始资源如下：

| resource_id | 引擎 | 源码/结构目录 | 模型目录或权重 |
|---|---|---|---|
| `gpt-sovits-local` | `gpt_sovits` | `F:\Code\Github\TTS_more\repo\GPT-SoVITS-main` | `GPT_SoVITS\pretrained_models\s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt` 与 `GPT_SoVITS\pretrained_models\s2G488k.pth` |
| `indextts-local` | `index_tts` | `F:\Code\Github\TTS_more\repo\index-tts` | `F:\Code\Github\TTS_more\repo\index-tts\checkpoints` |
| `cosyvoice-local` | `cosyvoice` | `F:\Code\Github\TTS_more\repo\CosyVoice` | `F:\Code\Github\TTS_more\repo\CosyVoice\pretrained_models\CosyVoice-300M` |

优先复用上述现有模型。若 CosyVoice-300M 被当前插件明确判定为架构不兼容，先记录失败证据和所需官方模型版本，再决定是否下载 CosyVoice3；不静默替换或重复下载模型。

## 运行拓扑与配置

- ComfyUI 默认只监听 `127.0.0.1:8188`。
- 三个 TTS More 服务端点共享一个 `resource_group`，表示同一张物理 GPU。
- 单 GPU 的初始 `capacity` 固定为 `1`。
- 旧 `tts-more-v1` 本机 worker 配置保留备份，但不参与本轮验证。
- 参考音频由 TTS More 上传到 TTS-Audio-Suite 资产接口，工作流只传递临时 `asset_id`；生成结束后释放资产和模型运行时。

## 分阶段验证

### 阶段 1：基础环境

1. 确认 ComfyUI、TTS-Audio-Suite 和 TTS More 工作树状态。
2. 将官方 ComfyUI 快进到最新 `master`，不产生本地提交。
3. 更新 ComfyUI 依赖并运行 TTS-Audio-Suite 的正式安装流程。
4. 启动 ComfyUI，检查插件导入、节点注册和端口监听。

通过标准：

- `/system_stats` 可访问。
- 启动日志中没有导致 TTS-Audio-Suite 节点缺失的导入错误。
- `TTSExternalGPTSovitsEngine`、`TTSExternalIndexTTSEngine`、`TTSExternalCosyVoiceEngine` 和资产节点已注册。

### 阶段 2：资源与 Bridge

1. 加载私有 `resources.yaml`。
2. 调用 `/api/tts-audio-suite/v1/capabilities`。
3. 分别验证三个 `resource_id` 的路径、引擎归属和可用状态。
4. 验证参考音频上传、查询、删除和运行时释放接口。

通过标准：

- capabilities 返回三个预期资源。
- ComfyUI 日志和 API 响应不泄露不必要的本机绝对路径。
- 缺失资源、错误引擎和无效资产返回明确错误，而不是静音或空结果。

### 阶段 3：直接 ComfyUI 合成

按以下顺序执行最小工作流：

1. IndexTTS：模型最完整，作为第一条真实音频基线。
2. CosyVoice：先尝试现有 CosyVoice-300M。
3. GPT-SoVITS：使用现有官方基础权重和参考音频。

每个引擎必须完成模型加载、`/prompt` 提交、`/history/{prompt_id}` 完成和 `/view` 下载音频。

### 阶段 4：TTS More 端到端

1. 启动 TTS More 后端和前端。
2. 登记三个 ComfyUI 端点。
3. 在应用中创建最小项目、角色绑定和台词。
4. 逐引擎提交真实合成。
5. 验证队列状态、外部 prompt id、生成历史、浏览器可见结果和本地音频文件。
6. 释放运行时并检查显存回收。

## 真实闭环判定

单个引擎只有同时满足以下条件才算跑通：

1. 对应资源能够加载。
2. ComfyUI prompt 正常完成。
3. 输出文件非空且可解码。
4. 音频包含有效时长和非静音采样。
5. TTS More 能记录任务状态并展示或播放结果。
6. 清理参考资产和释放运行时后，后续请求仍可再次成功。

三个引擎全部满足时，才把本机三引擎方案标记为完成。部分成功时，报告必须逐引擎列出已验证范围和剩余阻塞。

## 问题记录与修复归属

每次失败都记录：

- 时间和验证阶段
- 使用的仓库提交
- 精确命令或 API 请求
- 完整错误摘要与日志路径
- 是否可稳定复现
- 根因分类
- 允许干预的仓库
- 修复内容
- 修复后的复验结果

根因归属规则：

| 问题类型 | 首选处理位置 |
|---|---|
| 服务注册、任务编排、工作流参数、前端状态 | TTS More |
| 模型目录解析、节点输入、引擎导入、Bridge API | TTS-Audio-Suite fork |
| ComfyUI 官方回归 | 先在插件侧兼容并记录官方最小复现 |
| 三个 TTS 项目模型格式差异 | 插件适配层，不修改模型项目 |
| 本机依赖或启动配置 | 本机部署配置和最终运行手册 |

原始日志和大体积音频保存在不提交的验证产物目录；最终把问题矩阵、有效配置、启动顺序、验证证据和剩余限制整理成可提交的解决方案文档。

## 停止与升级条件

- 不因第一次导入失败就重装整套环境；先定位缺失依赖或版本冲突。
- 不修改官方 ComfyUI 或三个模型项目来绕过适配问题。
- 不在未记录原始错误前覆盖配置或模型。
- 大型模型下载、模型替换或新的独立环境，只在现有官方模型资产被证明确实无法兼容后进行。
- 若修复需要扩大到第三个可修改仓库，先停止并请求新的范围授权。
