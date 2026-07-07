# TTS More

TTS More 是一个面向剧本配音生产的多 TTS 服务调度工作台。项目目标不是重写各个 TTS 模型，而是在 GPT-SoVITS、IndexTTS、CosyVoice 以及未来 TTS API 之上建立统一的剧本解析、角色音色配置、任务队列、服务状态感知和生成历史管理能力。

当前主线开发重点是三个开源 TTS 服务：

```text
GPT-SoVITS -> IndexTTS -> CosyVoice -> TTS API
```

其中 TTS API 暂时保留为占位入口，当前核心开发资源集中在 GPT-SoVITS、IndexTTS、CosyVoice 三个开源服务上。

## 当前定位

TTS More 是外层独立框架项目，`repo/` 下的 TTS 项目保持为可更新的上游仓库或本地部署目录。外层框架只负责：

- 导入和解析剧本，将自由文本转为角色、括注、台词行。
- 管理全局角色库和项目角色映射。
- 根据角色、台词、音色绑定和服务状态选择 TTS 服务。
- 通过 HTTP endpoint 调用本机、局域网或公网 TTS 服务。
- 将生成任务放入队列，按资源组和模型加载签名调度。
- 保存每行台词的多批次音频历史和参数快照。
- 提供 React 工作台完成普通用户可操作的配音生产流程。

重要原则：

- 所有推理调用都走 HTTP endpoint。
- 本机 repo path 不作为产品配置发布；运行时只保存已启动服务的 HTTP endpoint。
- 不把真实角色库、本机路径、局域网 IP、模型权重、生成音频或 `.env.local` 提交到远端仓库。
- Mock 只允许存在于测试和开发 fixture，产品默认路径必须面向真实服务或空状态。

## 最短操作路径

日常使用只需要记住一条线：

```mermaid
flowchart LR
  A["新建或打开剧本"] --> B["提取台词"]
  B --> C["匹配角色和音色"]
  C --> D["接入并检测 TTS 服务"]
  D --> E["生成选中台词"]
  E --> F["在行内试听历史版本"]
```

工作台刻意压成三块：

- 左侧：剧本和原文，只负责新建、编辑、提取台词。
- 中间：台词任务，只负责筛选、选行、播放历史和批量生成。
- 右侧：当前行，只负责选择生成方式、声音资源和生成本行。

顶部入口使用任务名：`角色` 维护角色/音色，`队列` 查看生成进度，`解析` 配置 LLM，`接入` 配置 TTS endpoint。普通生成不需要先理解模型仓库路径、cluster key 或历史兼容项。

## 总体架构

```text
React/Vite 工作台
  |
  | REST API
  v
FastAPI Orchestrator
  |
  |-- 项目与版本存储
  |-- 角色库与 Voice Binding
  |-- 服务注册与状态轮询
  |-- Provider Router
  |-- 异步任务队列
  |-- Manifest / 音频历史
  |
  +--> GPT-SoVITS Endpoint
  +--> IndexTTS Endpoint
  +--> CosyVoice Endpoint
  +--> TTS API Endpoint 占位
```

### 前端

前端位于 `frontend/`，使用 React、Vite、TypeScript 和 i18next。

当前工作台由三部分组成：

- 左侧剧本控制台：剧本选择、原文预览、编辑入口、解析入口。
- 中间台词任务列表：角色筛选、台词行、生成状态、行级历史音频。
- 右侧生成配置面板：当前行的生成方式、声音资源、服务诊断和生成确认。

界面已经从早期 demo 表格逐步改为生产工作台形态：

- 角色筛选使用头像卡。
- 台词行聚焦角色、括注、台词和生成状态。
- 历史音频归属中间台词行展开区域。
- 右侧只保留当前行配置和生成确认。
- 选中态和主操作正在统一为蓝色系视觉，不再使用黑色作为主流程焦点。

### 后端

后端位于 `backend/`，使用 FastAPI。

核心模块包括：

- `models.py`：项目、角色、服务、任务、manifest 等核心 schema。
- `services.py`：TTS service endpoint 注册、状态检查、HTTP contract 调用。
- `queue.py`：资源组、cluster key、异步任务调度和状态追踪。
- `open_source_tts.py`：开源 TTS 服务目录、检测和本地配置写入。
- `role_library.py`：角色库、logs 扫描、参考音频候选和 binding 生成。
- `storage.py`：文件制项目存储、模板数据、运行数据隔离。
- `main.py`：REST API 入口。

## 数据与配置

仓库只提交空模板和不含个人数据的配置样板，真实运行数据默认进入本地目录。

推荐结构：

```text
data/
  templates/
    services.example.json
    characters.example.json
  local/
    services.json          # 本机或团队内部真实服务配置，不提交
    characters.json        # 本机或团队内部真实角色库，不提交
    projects/              # 旧兼容项目目录，不提交
  parser_providers.json    # 本机 LLM 解析配置，不提交
Project/                   # 默认真实剧本、manifest、生成历史，不提交
```

配置加载优先级：

```text
.env.local / data/local/services.json > data/services.json > data/templates/services.example.json
```

剧本项目默认写入仓库根目录下的 `Project/`。如果设置了 `TTS_MORE_PROJECTS_PATH`，则使用该环境变量指定的目录。

`.env.local` 用于保存 API key、本机路径和私有参数。它不应提交到 GitHub 或 Gitee。

## 服务接入

### 推荐安装顺序

先获取 TTS More：

```powershell
git clone https://github.com/XucroYuri/TTS_more.git
```

如果本机或局域网还没有可用 TTS 服务，可以按需部署以下项目，并启动它们各自的推理 WebUI：

```powershell
git clone https://github.com/XucroYuri/GPT-SoVITS.git repo/GPT-SoVITS
git clone https://github.com/XucroYuri/index-tts.git repo/index-tts
git clone https://github.com/XucroYuri/CosyVoice.git repo/CosyVoice
```

这些 fork 作为稳定镜像使用，目的是降低上游更新导致集成失效的风险。TTS More 不再绑定本机 repo 路径，也不托管启动进程；只接入已经运行的 Gradio WebUI。

### 极简 Gradio 接入

在工作台打开顶部 `接入`：

1. 选择 GPT-SoVITS、IndexTTS 或 CosyVoice。
2. 粘贴推理 WebUI 的 Gradio 地址，例如 `http://tts-webui.local:9872`。
3. 点击 `检测并保存`，让工作台确认 `/config` 与所需 api_name 可用后写入本地服务目录。

`127.0.0.1` 和 `localhost` 仍然兼容，本质上也是 Gradio endpoint。局域网或公网 endpoint 不提供远程进程控制，只做健康检查、能力检查和任务调用。

接入向导会写入 `data/local/services.json`，不会污染可提交模板。

### LLM 解析激活

在工作台打开顶部 `解析`，输入开物基模 API Key 并保存激活即可。开物基模已内置 `https://kwjm.com`、`gpt-5.5` 和 `KWJM_API_KEY` 预设；高级配置折叠区仍可维护其他 OpenAI-compatible 服务。

## Provider 能力

### GPT-SoVITS

GPT-SoVITS 是默认优先级最高的训练音色 provider。

当前设计采用 logs-first：

```text
角色名称 / 昵称 / 别名
  -> logs_name
  -> GPT 权重
  -> SoVITS 权重
  -> 参考音频
  -> 参考文本
  -> 生成台词音频
```

生成前会计算加载签名：

```text
service_id + logs_name + gpt_weights_path + sovits_weights_path + ref_audio_path + prompt_text + prompt_lang + text_lang
```

同一服务内签名一致时可以复用加载状态；签名变化时必须切换权重和参考音频。无法从 WebUI 强回读状态时，只能标记为假定成功，不应显示强验证绿色。

### IndexTTS

IndexTTS 是强情绪和临时配音能力的核心 provider。

当前重点不是强制每个角色都配置 IndexTTS 角色库，而是在右侧配置面板中复现原生 IndexTTS 的临时生成能力：

- 上传参考音频。
- 拖拽参考音频。
- 录音作为参考音频。
- 选择情绪控制方式。
- 展开高级参数。

未命中角色库的角色默认不会自动兜底到 IndexTTS，避免错误音色批量生成。用户需要手动建立当前行临时配置。

### CosyVoice

CosyVoice 已作为一等开源 provider 接入，排序在 IndexTTS 之后、TTS API 之前。

第一版以 endpoint 接入为主，不强制要求 `repo/CosyVoice` 存在。计划支持：

- `sft`
- `zero_shot`
- `cross_lingual`
- `instruct`

CosyVoice 的 cluster key 设计为：

```text
service_id + mode + speaker_id + prompt_audio_path + prompt_text + instruct_text + speed + seed
```

### TTS API

TTS API 当前是占位入口，用于后续接入 OpenAI、Gemini、xAI、火山引擎等商业或云端 TTS 服务。

当前阶段不把 TTS API 作为核心验收目标，也不让它抢占 GPT-SoVITS、IndexTTS、CosyVoice 的主流程。

## 队列与资源调度

调度层使用 `resource_group` 和 `capacity` 控制并发。

典型场景：

- 本机单 GPU：`local-gpu-0 capacity=1`，三个开源 TTS 串行执行。
- 局域网 GPU 机器：独立资源组，可和本机并行。
- 云端 endpoint：独立资源组，可按服务声明容量并行。

队列按 provider、service 和 cluster key 聚合：

- GPT-SoVITS：按 logs、GPT 权重、SoVITS 权重、参考音频和参考文本聚合。
- IndexTTS：按参考音频、情绪模式、情绪来源和高级参数聚合。
- CosyVoice：按模式、speaker、prompt audio、prompt text、instruction、speed 和 seed 聚合。

当前策略：

- 同资源组按容量限制执行。
- 不同资源组可以并行。
- 当前已加载 cluster 有待执行任务时优先继续执行。
- 否则优先选择待执行数量最多的 cluster。
- 使用等待时间避免长期饥饿。
- 本机服务不在每组任务后强制 unload，优先减少频繁加载卸载成本。

## 角色库与项目角色

角色库支持全局预设角色和项目角色引用。

当前规则：

- 全局角色库保存角色名、别名、昵称、匹配名和多个 provider binding。
- 项目剧本解析后，项目角色按名称、别名、昵称等字段匹配全局角色。
- 命中角色库时，项目角色默认引用全局配置。
- 生成或交付前可以冻结为项目快照。
- 未命中角色不会自动分配错误音色，需要用户手动配置当前行或加入角色库。

GPT-SoVITS 角色配置以 logs 为核心入口。IndexTTS 以行级临时参考音频配置为主要入口。CosyVoice 绑定为普通 voice binding，不影响 GPT-SoVITS 的 logs-first 逻辑。

## 剧本与生成历史

项目数据采用文件制。

核心概念：

- `ScriptProject`：一个剧本配音项目。
- `ScriptRevision`：剧本文本版本。
- `ParseRevision`：某个剧本文本版本对应的解析结果。
- `ScriptLine`：稳定台词行，使用 `line_uid` 关联生成历史。
- `GenerationVersion`：某一行的某次生成结果。

重新编辑或重新解析剧本会创建新版本分支，不覆盖旧台词和旧音频。生成历史记录中会保存：

- provider
- service
- binding
- 参数摘要
- requested load signature
- verified load signature
- 音频路径
- 状态
- 错误摘要

历史音频播放器保留在中间台词行展开区域；右侧面板只在用户选中某个历史版本时显示对应参数。

## 当前状态

代码和测试是当前事实来源。面向 Agent 或人类协作时，优先读取：

- [极简工作台审计与改进计划](docs/minimal-workbench-audit.md)
- [开源 TTS 服务接入与混合部署](docs/open-source-tts-services.md)
- [发布治理说明](docs/release-governance.md)

不要把旧进展列表当作最新计划；需要判断下一步时，以当前 UI、测试结果和上述文档为准。

## 本地开发

### 安装依赖

```powershell
py -3.10 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e 'backend[dev]'
cd frontend
pnpm install
```

macOS / Linux 等价命令：

```bash
uv venv --python 3.11 .venv        # 或 python3.11 -m venv .venv
uv pip install --python .venv/bin/python -e 'backend[dev]'   # 或 .venv/bin/pip install -e 'backend[dev]'
(cd frontend && pnpm install)
```

后端依赖约束为 `requires-python = ">=3.10,<3.14"`，已验证 3.10 / 3.11 / 3.12 / 3.13。

### 启动开发环境

```powershell
.\scripts\start-dev.ps1
```

macOS / Linux：

```bash
./scripts/start-dev.sh
```

默认地址：

- 后端：`http://127.0.0.1:8000`
- 前端：`http://127.0.0.1:5173`

### 模型准备

本机模型准备脚本：

```powershell
.\scripts\prepare-models.ps1 -Source ModelScope -Device CU128
```

该脚本面向本机真实环境，可能下载大模型并创建子项目虚拟环境。运行前请确认磁盘、网络、CUDA 和 Python 环境。

## 验证

常规回归：

```powershell
& .\.venv\Scripts\python.exe -m pytest backend -q
cd frontend
pnpm test -- --run
pnpm build
```

macOS / Linux：

```bash
.venv/bin/python -m pytest backend -q
(cd frontend && pnpm test && pnpm build)
```

真实 TTS 验收需要本机或网络 endpoint、模型资源和 GPU：

```powershell
$env:TTS_MORE_SERVICE_MODE="real"
$env:TTS_MORE_RUN_REAL_TTS="1"
& .\.venv\Scripts\python.exe -m pytest backend/tests/test_real_tts_validation.py -q
```

macOS / Linux：

```bash
TTS_MORE_SERVICE_MODE=real TTS_MORE_RUN_REAL_TTS=1 \
    .venv/bin/python -m pytest backend/tests/test_real_tts_validation.py -q
```

真实模式下禁止伪装 mock 成功。如果模型、权重、参考音频或服务端口缺失，验收应返回明确诊断。

## 发布与安全治理

提交前必须确认：

- `repo/` 不提交。
- `.env.local` 不提交。
- `data/local/` 不提交。
- `data/parser_providers.json` 不提交。
- `Project/` 不提交。
- 生成音频、模型权重、manifest 运行历史不提交。
- 本机路径、UNC 路径、局域网 IP 不进入模板和 README。
- 真实角色库不进入公开模板。
- 测试 fixture 中的 mock 不进入产品默认路径。

可使用：

```powershell
git status --short
git check-ignore -v data/local/services.json data/local/characters.json data/parser_providers.json Project/example/.project-id .env.local repo/GPT-SoVITS/README.md
& .\.venv\Scripts\python.exe -m pytest backend/tests/test_release_governance.py -q
```

macOS / Linux：

```bash
git status --short
git check-ignore -v data/local/services.json data/local/characters.json data/parser_providers.json Project/example/.project-id .env.local repo/GPT-SoVITS/README.md
.venv/bin/python -m pytest backend/tests/test_release_governance.py -q
```

## 远端仓库

当前主分支已同步到：

- GitHub：`https://github.com/XucroYuri/TTS_more`
- Gitee 团队仓库：`https://gitee.com/chengdu-flower-food/TTS_more`

团队内部提交和 PR 默认使用中文标题与中文描述，便于开发同步和审查。

## 参考文档

- [开源 TTS 服务接入与混合部署](docs/open-source-tts-services.md)
- [极简工作台审计与改进计划](docs/minimal-workbench-audit.md)
- [发布治理说明](docs/release-governance.md)
- [前端设计基线](frontend/design.md)
