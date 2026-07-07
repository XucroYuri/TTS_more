# TTS More 发布治理说明

## 目标

TTS More 的代码仓库只提交可复用框架、模板、测试和文档；真实运行产生的剧本、角色库、服务端点、模型路径、参考音频、生成音频和 API Key 只保存在本地运行目录或环境变量中。

## 可提交内容

- `backend/`、`frontend/`、`scripts/` 中的框架源码和测试。
- `data/services.json`：脱敏后的服务模板，不包含个人路径、局域网地址或真实鉴权信息。
- `data/templates/services.example.json`：服务配置示例。
- `data/templates/characters.example.json`：空角色库模板。
- `docs/`：设计、计划和发布治理文档，不提交可复用演示剧本 prompt。

## 不可提交内容

- `data/local/`：本机服务配置、真实角色库、真实剧本项目和 manifest。
- `data/projects/`：运行项目目录。
- `data/demo/`：旧测试项目路径，保留为忽略项。
- `data/templates/demo-*`：任何演示剧本模板。
- `docs/*demo*-prompt.md`：任何演示剧本生成 prompt。
- `.env.local` 与任何 secret 文件。
- `repo/`、模型权重、下载缓存、生成音频、上传参考音频。

## 本地配置优先级

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
2. `data/local/projects`
3. `data/projects`
4. 旧根目录项目（兼容旧数据）

新写入的角色库、服务配置和剧本项目默认进入本地运行目录。

## 发布阶段

1. `chore: 隔离本机运行数据与发布模板`
   - 数据路径治理、模板、忽略规则、发布检查测试。
2. `feat: 强化真实 TTS 服务感知与任务调度`
   - 服务状态分层、候选服务过滤、任务队列轮询和真实签名闭环。
3. `refactor: 优化生成工作台交互与选中态`
   - 蓝色主视觉、去重诊断入口、生成面板信息层级。
4. `chore: 清理演示剧本和模拟数据`
   - 移除可提交演示剧本模板、演示 prompt 和产品源码中的固定示例台词。
5. 推送私有远端并创建中文 PR。

## 发布前检查

```powershell
.\.venv\Scripts\python.exe -m pytest backend -q
pnpm --dir frontend test -- --run
pnpm --dir frontend build
git check-ignore -v data/local/characters.json data/local/services.json .env.local repo/GPT-SoVITS/README.md
```

可提交文件中不得出现本机绝对路径、局域网地址、UNC 路径、真实角色训练名、固定演示剧本、模拟项目数据或真实音频路径。
