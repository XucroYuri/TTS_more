# Windows CUDA Codex 交接 Prompt

把下面文本交给新 Windows CUDA 设备上的 Agent。具体命令只从 [单机 Windows CUDA Runbook](cuda-e2e-single-node.md) 复制；本页不重复执行步骤。

````text
你负责在真实 Windows CUDA 设备上完成 TTS More 认证。先阅读并严格执行：

- 单机唯一执行入口：docs/cuda-e2e-single-node.md
- 跨拓扑契约：docs/cuda-e2e-validation.md
- 人工签核模板：docs/cuda-e2e-acceptance-record.md
- 四机认证：docs/cuda-e2e-distributed.md

## 授权边界

- 仓库为 https://github.com/XucroYuri/TTS_more.git，目标分支为 dev-xu/cuda-e2e-validation。
- 开始时读取实际分支、HEAD 和 git status；锁定清单只使用仓库根 repo.lock.json。
- 保留用户已有修改和本机配置。任何删除、覆盖、清理、提交或发布都要符合用户授权。
- 只有确认是本仓库缺陷时才改代码；先写失败测试并见证 RED，再做最小修复。
- 不使用 mock、降低阈值、Skip 开关或无 GPU 单测代替真实认证。
- 未获明确授权不要 push。不要改写上游 TTS repo 历史。

## 环境与私有输入

- 应用环境固定 Python 3.11；Windows GPT-SoVITS 正式准备必须有 conda。
- 要求 CUDA 12.8、至少 16 GB VRAM、足够磁盘和可用 Playwright Chromium。
- fixture 必须由人类提供三份真实参考音频、四个 GPT/SoVITS 权重、测试文本和审核者。
- topology、fixture、hostname、IP、用户名、绝对路径、密钥、音频、权重路径和审核者身份不得提交。
- 受控原始证据仅保存在本机运行目录或受控存储；PR 和普通 artifact 只能使用脱敏可共享证据。

## 执行规则

- 单机正式路径只运行一次 runbook 中的总入口，直接传可选 RepoPaths；不要先手动部署或启动。
- 总入口必须先完成 host preflight 和 input preflight，再执行清理、部署或 worker 等待。
- SkipDeploy 或 SkipStart 仅为 diagnostic，结果不可认证、不可批准基线。
- 核心 CUDA 通过后，必须独立运行 Playwright 30 条真实队列，并使用唯一项目 ID。
- 首次 single-clean 的 6 个输出必须由两名审核者分别听审，共 12 条评分。

## 停止条件

遇到以下任一情况立即停止认证，保留证据并报告阻塞：

- Python 3.11、conda、CUDA、VRAM、磁盘或 Playwright 不满足；
- fixture、参考音频、权重、凭据或人类确认缺失；
- 清理范围与已确认 RepoPaths 不一致；
- 发现未知 GPU 进程、未知端口所有者或可能被误杀的进程；
- worker 契约、核心 CUDA、ASR、性能、Playwright 或证据完整性失败；
- 自动门禁通过但人工听审尚未完成。

## 状态

保留机器状态原值：blocked、core_failed、diagnostic_core_passed、core_passed_ui_pending、automatic_passed_human_pending。

最终回复只能使用以下人类结论之一：

1. 认证通过；
2. 自动门禁通过，人工待完成；
3. 失败；
4. 阻塞。

## 最终回复

按以下顺序报告：

- 基线分支、HEAD 和工作树；
- 环境与实际执行；
- host/input preflight；
- 核心 CUDA 与 Playwright；
- 人工听审和基线状态；
- 受控原始证据位置；
- 脱敏可共享证据位置；
- 代码修改、测试 RED/GREEN 和 commit；
- 阻塞、剩余风险和下一步。

最终回复不得复制原始日志、私有路径、机器标识、音频或审核者身份。
````
