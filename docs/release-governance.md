# 发布治理

TTS More 仓库只提交可复用框架、模板、测试和文档；真实运行产生的剧本、角色库、服务端点、模型路径、参考音频、生成音频和 API Key 只保存在本地运行目录或环境变量中。

## 可提交 vs 不可提交

```mermaid
flowchart LR
    subgraph Commit["可提交"]
        Src["backend/ frontend/ scripts/<br/>源码 + 测试"]
        Tpl["data/services.json<br/>data/templates/*"]
        Docs["docs/"]
    end
    subgraph Local["不可提交（本地）"]
        Env[".env.local / secrets"]
        Runtime["data/local/<br/>data/projects/<br/>data/validation/*.local.json<br/>Project/"]
        Assets["repo/ 模型权重<br/>生成音频 / 参考音频"]
    end
    Commit -.->|.gitignore 覆盖| Local
```

**可提交：** `backend/`、`frontend/`、`scripts/` 源码与测试；脱敏的 `data/services.json` 与 `data/templates/*`；`docs/`。

**不可提交：** `data/local/`（本机配置/角色库/项目）、`data/projects/`、`data/validation/*.local.json`、`deployment/app/topology*.local.json`、`Project/`（标题命名项目目录）、`.env.local` 与任何 secret、`repo/`、模型权重、生成音频、上传参考音频、演示剧本 prompt、SSH 用户或密钥。

## 配置加载优先级

```mermaid
flowchart TD
    Start["读取配置"] --> Env{"env 覆盖?"}
    Env -- "TTS_MORE_*_PATH" --> Custom["自定义路径"]
    Env -- 否 --> Local["data/local/*"]
    Local -- 不存在 --> Default["data/* 或 data/templates/*"]
```

**服务配置：** `TTS_MORE_SERVICES_PATH` → `data/local/services.json` → `data/services.json` → `data/templates/services.example.json`

**角色库：** `TTS_MORE_CHARACTERS_PATH` → `data/local/characters.json` → `data/characters.json`（兼容旧）→ `data/templates/characters.example.json`

**剧本项目：** `TTS_MORE_PROJECTS_PATH` → `data/local/projects` → `data/projects` → 旧根目录（兼容）

新写入默认进入本地运行目录。

## 发布前检查

```bash
# macOS / Linux
.venv/bin/python -m pytest backend -q
# Windows: & .\.venv\Scripts\python.exe -m pytest backend -q

pnpm --dir frontend test
pnpm --dir frontend build

git check-ignore -v data/local/characters.json data/local/services.json .env.local repo/GPT-SoVITS-main/README.md
.venv/bin/python -m pytest backend/tests/test_release_governance.py -q
```

可提交文件中不得出现：本机绝对路径、局域网地址、UNC 路径、真实角色训练名、固定演示剧本、模拟项目数据、真实音频路径。`test_release_governance.py` 会自动扫描 `192.168.2.`、`\\192.168.`、`J:\`、`F:\` 等禁止令牌。

## Windows CUDA 发布门禁

稳定发布还必须满足 [CUDA 全流程闭环验证](cuda-e2e-validation.md)：

1. 一次通过的 `single-release` Windows CUDA 运行，且能追溯到首次 `single-clean` 16 GB 基线；
2. 一次通过的四机 `distributed` 运行，且能追溯到首次分布式认证基线；
3. 两次运行各自的 `summary.json`、JUnit、WAV、worker 日志、`nvidia-smi` 和 Playwright 证据 URL；
4. 使用 [验收记录模板](cuda-e2e-acceptance-record.md) 完成的人工听审，发布至少一名审核者；首次认证为两名；
5. topology/fixture hash、TTS More commit 和三个 TTS repo 锁定 commit 一致。

任一自动门禁失败、结果缺失、性能或质量阈值超限、人工评分不足都阻止发布。prerelease 可以用于触发认证，但不能绕过稳定发布门禁。真实 topology、fixture、音频和机器信息只上传到访问受控的存储；公开 CI 工件必须脱敏。
