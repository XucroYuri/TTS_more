# TTS More

TTS More 是面向剧本配音生产的工作台。它不训练模型，也不替代 GPT-SoVITS、IndexTTS 或 CosyVoice；它负责把剧本、角色音色、TTS endpoint、生成队列和历史试听放到一个低干扰的操作界面里。

默认心智模型很简单：

```mermaid
flowchart LR
  A["导入剧本"] --> B["提取台词"]
  B --> C["选择角色音色"]
  C --> D["接入 TTS endpoint"]
  D --> E["生成台词"]
  E --> F["试听历史"]
```

## 产品边界

TTS More 只做这些事：

- 把自由剧本文本解析成角色、括注和台词行。
- 给当前剧本角色选择或保存常用音色。
- 保存已经启动的 Gradio / TTS API endpoint。
- 按服务状态、资源组和任务队列调度生成。
- 保存每行台词的多次生成历史，支持行内试听。

TTS More 不做这些事：

- 不管理上游模型仓库的安装、训练和启动。
- 不把本机 repo path 当作产品配置。
- 不提交真实角色库、模型权重、生成音频、`.env.local` 或本机运行数据。
- 不把 mock 服务伪装成真实验收结果。

## 工作台结构

主界面是三栏：

- 左侧：剧本列表、原文编辑、新建、保存和提取台词。
- 中间：台词列表、角色筛选、批量生成和历史试听。
- 右侧：当前行的音色、参考资源、生成文本和生成本行。

顶部入口使用任务名：

- `角色`：给当前剧本角色选择或保存常用音色。
- `队列`：有生成任务时查看分发状态。
- `解析`：配置剧本解析用 LLM。
- `接入`：粘贴并检测 TTS Gradio endpoint。

普通生成不需要先理解模型仓库、cluster key、binding_id 或旧兼容路径。

## 最短启动

### Windows

```powershell
py -3.10 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e 'backend[dev]'
cd frontend
pnpm install
cd ..
.\scripts\start-dev.ps1
```

### macOS / Linux

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e 'backend[dev]'
(cd frontend && pnpm install)
./scripts/start-dev.sh
```

默认地址：

- 后端：`http://127.0.0.1:8000`
- 前端：`http://127.0.0.1:5173`

## 接入 TTS 服务

先启动你自己的 GPT-SoVITS、IndexTTS 或 CosyVoice WebUI。然后在工作台顶部打开 `接入`：

1. 选择服务类型。
2. 粘贴 Gradio WebUI 地址，例如 `http://127.0.0.1:9872`。
3. 点击 `检测并保存`。

检测结果会写入 `data/local/services.json`。这个文件只属于本机或团队内部环境，不提交。

## 激活剧本解析

在工作台顶部打开 `解析`，粘贴开物基模 API Key 后保存。默认预设使用：

- Base URL：`https://kwjm.com`
- 模型：`gpt-5.5`
- 环境变量名：`KWJM_API_KEY`

高级解析服务只在需要多个 OpenAI-compatible provider 或自定义模型时再展开。

## 数据位置

仓库只提交模板和代码。真实运行数据默认在本机：

```text
data/local/services.json
data/local/characters.json
data/parser_providers.json
Project/
.env.local
repo/
```

这些路径都不应提交。剧本项目默认写入 `Project/`；如需换位置，设置 `TTS_MORE_PROJECTS_PATH`。

## 常规验证

### Windows

```powershell
& .\.venv\Scripts\python.exe -m pytest backend -q
cd frontend
pnpm test -- --run
pnpm build
```

### macOS / Linux

```bash
.venv/bin/python -m pytest backend -q
(cd frontend && pnpm test && pnpm build)
```

发布边界检查：

```bash
git status --short
git check-ignore -v data/local/services.json data/local/characters.json data/parser_providers.json Project/example/.project-id .env.local repo/GPT-SoVITS/README.md
.venv/bin/python -m pytest backend/tests/test_release_governance.py -q
```

真实 TTS 验收需要可用 endpoint、模型资源和 GPU：

```bash
TTS_MORE_SERVICE_MODE=real TTS_MORE_RUN_REAL_TTS=1 \
  .venv/bin/python -m pytest backend/tests/test_real_tts_validation.py -q
```

如果模型、权重、参考音频或端口缺失，真实验收应该失败并给出明确诊断。

## 文档索引

- [极简工作台审计与改进计划](docs/minimal-workbench-audit.md)
- [前端设计基线](frontend/design.md)
- [开源 TTS 服务接入](docs/open-source-tts-services.md)
- [发布治理说明](docs/release-governance.md)
- [GPT-SoVITS fork 历史增强提示](docs/agent-prompts/gpt-sovits-fork-enhancement.md)

判断下一步时，以当前代码、测试结果和这些文档为准，不要把旧进展列表当作最新计划。

## 远端仓库

- GitHub：`https://github.com/XucroYuri/TTS_more`
- Gitee：`https://gitee.com/chengdu-flower-food/TTS_more`
