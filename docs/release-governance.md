# 发布治理说明

这份文档只写稳定规则，不写阶段路线图。判断能不能提交时，以当前代码、测试和这里的边界为准。

## 发布目标

仓库只提交可复用框架、模板、测试和文档。真实运行产生的剧本、角色库、服务 endpoint、模型路径、参考音频、生成音频和 API Key 只留在本机运行目录或环境变量里。

## 可以提交

- `backend/`、`frontend/`、`scripts/` 中的框架源码和测试。
- `data/services.json`：脱敏服务模板，不含个人路径、局域网地址或真实鉴权信息。
- `data/templates/services.example.json`：服务配置示例。
- `data/templates/characters.example.json`：空角色库模板。
- `docs/`：稳定说明、操作手册、审计计划和发布治理文档。

## 不能提交

- `data/local/`：本机服务配置、真实角色库、运行项目和 manifest。
- `data/parser_providers.json`：本机 LLM 解析服务配置。
- `Project/`：默认真实剧本项目、manifest、生成历史和回收站。
- `data/projects/`、`data/demo/`、`data/lan-tts-demo/`、`data/real-validation/`、`data/validation/`。
- `.env.local`、`.env.*` 和任何 secret 文件。
- `repo/`、模型权重、下载缓存、生成音频、上传参考音频。
- 固定演示剧本、演示 prompt、真实角色训练名、本机绝对路径、UNC 路径、局域网地址。
- `.omc/`、`.omo/`、`.omx/` 等本地 Agent/运行态目录。

## 本地数据路径

服务配置读取顺序：

1. `TTS_MORE_SERVICES_PATH`
2. `data/local/services.json`
3. `data/services.json`
4. `data/templates/services.example.json`

角色库读取顺序：

1. `TTS_MORE_CHARACTERS_PATH`
2. `data/local/characters.json`
3. `data/characters.json`（兼容旧数据）
4. `data/templates/characters.example.json`

剧本项目读取顺序：

1. `TTS_MORE_PROJECTS_PATH`
2. `Project/`
3. `data/local/projects`
4. `data/projects`
5. 旧根目录项目（兼容旧数据）

新写入的角色库、服务配置、解析服务配置和剧本项目默认进入不可提交的本地运行路径。

## 发布前检查

常规检查：

```bash
git status --short
git check-ignore -v data/local/characters.json data/local/services.json data/parser_providers.json Project/example/.project-id .env.local repo/GPT-SoVITS/README.md
.venv/bin/python -m pytest backend -q
(cd frontend && pnpm test && pnpm build)
```

PowerShell 等价命令：

```powershell
git status --short
git check-ignore -v data/local/characters.json data/local/services.json data/parser_providers.json Project/example/.project-id .env.local repo/GPT-SoVITS/README.md
& .\.venv\Scripts\python.exe -m pytest backend -q
cd frontend
pnpm test -- --run
pnpm build
```

如果 `git check-ignore` 没有命中上面的本地运行路径，先修 `.gitignore` 或本地 exclude，再考虑提交。不要为了让检查通过而移动真实数据进模板目录。

## 真实验收

真实 TTS 验收不是普通单元测试。只有本机或网络 endpoint、模型资源、参考音频和 GPU 都就绪时才运行：

```bash
TTS_MORE_SERVICE_MODE=real TTS_MORE_RUN_REAL_TTS=1 \
  .venv/bin/python -m pytest backend/tests/test_real_tts_validation.py -q
```

如果真实资源缺失，测试应该跳过或给出明确诊断；产品路径不能伪装 mock 成功。
