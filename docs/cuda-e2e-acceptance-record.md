# CUDA 验收记录模板

复制本模板到访问受控的位置填写，**不得提交到公开仓库**。执行步骤只使用 [单机 Runbook](cuda-e2e-single-node.md) 或 [分布式 Runbook](cuda-e2e-distributed.md)。

## 1. 基本信息

| 字段 | 值 |
|---|---|
| Run ID | |
| 模式 | `single-clean` / `single-release` / `distributed` |
| 分支与 TTS More commit | |
| 仓库根 `repo.lock.json` SHA-256 | |
| 执行开始/结束 | |
| 执行人 | |
| Topology 安全名称 / SHA-256 | |
| Fixture 安全名称 / SHA-256 | |
| 基线运行 ID | |
| 受控原始证据位置 | |
| 脱敏可共享证据位置 | |
| Playwright JUnit | `frontend/test-results/playwright-junit.xml` |
| 失败 trace/screenshot/video | 不适用或受控位置 |

受控原始证据可包含音频、日志、机器标识和签名；脱敏可共享证据不得包含这些内容。

## 2. 主机与锁定部署

应用控制面固定 Python 3.11；Windows GPT-SoVITS 正式准备必须有 conda。

| 检查项 | 结果 | 证据 |
|---|---|---|
| Windows、Python 3.11、conda | 通过 / 失败 / 阻塞 | |
| CUDA 12.8、VRAM >=16 GB、磁盘 | 通过 / 失败 / 阻塞 | |
| 非任务 GPU 进程与端口所有权 | 通过 / 失败 / 阻塞 | |
| 根 `repo.lock.json` 与三个实际 HEAD | 通过 / 失败 | |
| 三个 repo venv、模型和 CU128 runtime | 通过 / 失败 | |
| Topology schema、服务唯一归属、capacity | 通过 / 失败 | |
| Fixture、3 参考音频、4 权重 | 通过 / 失败 / 阻塞 | |
| `/health`、`/capabilities`、`/status` | 通过 / 失败 | |
| 应用 `/api/health`、`/api/services/status` | 通过 / 失败 | |

分布式记录还要核对时间、DNS、SSH、四台机器身份和三个 GPU 的唯一性；只保存脱敏结论和 hash，不复制原始标识到可共享记录。

## 3. 自动门禁

| 门禁 | 单机 | 分布式 | 证据 |
|---|---|---|---|
| 核心 5 模型能力 | 通过 / 失败 | 通过 / 失败 | summary / JUnit |
| GPT path 与 artifact | 通过 / 失败 | 不适用 | summary / JUnit |
| WAV 与 ASR CER | 通过 / 失败 | 通过 / 失败 | summary / JUnit |
| 显存、卸载和性能 | 通过 / 失败 | 通过 / 失败 | summary / 聚合 GPU 指标 |
| Playwright 30 条队列 | 通过 / 失败 / 未运行 | 通过 / 失败 / 未运行 | Playwright JUnit |
| 三条代表性历史音频 | 通过 / 失败 / 未运行 | 通过 / 失败 / 未运行 | JUnit |
| 分布式并行重叠 | 不适用 | 通过 / 失败 | JUnit |
| 故障降级、恢复和重试 | 不适用 | 通过 / 失败 | fault recovery summary |

自动阈值以 [CUDA 验证契约](cuda-e2e-validation.md#必过矩阵) 为准，不在本记录中放宽。

## 4. 机器状态

勾选且只选一个：

- [ ] `blocked`：环境、资产、凭据或人类输入缺失；
- [ ] `core_failed`：核心 CUDA 门禁失败；
- [ ] `diagnostic_core_passed`：Skip 诊断通过，不可认证；
- [ ] `core_passed_ui_pending`：核心通过，Playwright 待完成；
- [ ] `automatic_passed_human_pending`：自动门禁通过，人工待完成。

## 5. 首次认证听审

首次 `single-clean` 必须填写以下 6 case × 2 reviewer 共 12 行。发布回归可保留一名审核者，但不得删除 case。

| Case | Reviewer | 清晰度 | 音色 | 情绪/韵律 | 伪影控制 | 总均分 | 结论/备注 |
|---|---|---:|---:|---:|---:|---:|---|
| `gpt-v2ProPlus` | `reviewer-1` | | | | | | |
| `gpt-v2ProPlus` | `reviewer-2` | | | | | | |
| `gpt-v2Pro` | `reviewer-1` | | | | | | |
| `gpt-v2Pro` | `reviewer-2` | | | | | | |
| `gpt-v2ProPlus-artifact` | `reviewer-1` | | | | | | |
| `gpt-v2ProPlus-artifact` | `reviewer-2` | | | | | | |
| `index-emotion-text` | `reviewer-1` | | | | | | |
| `index-emotion-text` | `reviewer-2` | | | | | | |
| `cosyvoice-zero-shot` | `reviewer-1` | | | | | | |
| `cosyvoice-zero-shot` | `reviewer-2` | | | | | | |
| `cosyvoice-cross-lingual` | `reviewer-1` | | | | | | |
| `cosyvoice-cross-lingual` | `reviewer-2` | | | | | | |

每个单项至少 3/5，总均分至少 3.5。审核者身份和签名只写入受控原始证据，不进入脱敏可共享证据。

## 6. 故障与例外

| ID | 阶段 | 症状 | 影响 | 根因 / issue | 是否阻塞 |
|---|---|---|---|---|---|
| | | | | | |

自动门禁失败、证据缺失、阈值超限或人工评分不足不能通过例外放行稳定发布。

## 7. 人工签核

| 角色 | 受控身份 | 决策 | 时间 | 签名或受控 URL |
|---|---|---|---|---|
| 执行人 | | | | |
| 审核者 1 | | | | |
| 审核者 2 | | | | |
| 发布负责人 | | | | |

最终人类结论，勾选且只选一个：

- [ ] **认证通过**：所有自动门禁和所需人工签核完成；
- [ ] **自动门禁通过，人工待完成**；
- [ ] **失败**：自动或人工门禁失败；
- [ ] **阻塞**：缺少环境、资产、凭据或人类确认。

阻塞原因或发布说明：
