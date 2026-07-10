# CUDA 验收记录模板

复制本模板到访问受控的发布记录位置。不要把真实 hostname、IP、用户名、绝对资产路径、参考音频或 secret 提交到公开仓库。

## 1. 基本信息

| 字段 | 值 |
|---|---|
| Run ID | `<yyyyMMdd-HHmmss-mode>` |
| 模式 | `single-clean` / `single-release` / `distributed` |
| 发布版本或 PR | `<version-or-url>` |
| 执行开始/结束 | `<ISO-8601>` |
| 执行人 | `<operator-id>` |
| TTS More commit | `<sha>` |
| `repo.lock.json` SHA-256 | `<sha256>` |
| Topology 名称 | `<sanitized-name>` |
| Topology SHA-256 | `<sha256>` |
| Fixture SHA-256 | `<sha256>` |
| 基线记录 | `<baseline-run-url>` |
| 自动化运行 URL | `<actions-or-controlled-storage-url>` |
| Playwright report URL | `<url>` |
| 工件保留位置 | `<controlled-storage-url>` |

## 2. Repo 锁定状态

| Service ID | Repo/分支 | 锁定 commit | 实际 `HEAD` | 匹配 |
|---|---|---|---|---|
| `local-gpt-sovits-main` | `GPT-SoVITS main` | `<sha>` | `<sha>` | 是/否 |
| `local-indextts` | `IndexTTS` | `<sha>` | `<sha>` | 是/否 |
| `local-cosyvoice` | `CosyVoice` | `<sha>` | `<sha>` | 是/否 |

## 3. 机器与运行时

每个节点填写一行；单机也要分别说明 app/worker 角色共用同一机器。

| 节点/角色 | Windows 版本/Build | CPU/RAM | GPU/VRAM | NVIDIA driver | CUDA | Python | 磁盘余量 | 时钟同步 |
|---|---|---|---|---|---|---|---|---|
| `<node>` | `<value>` | `<value>` | `<model>/<GiB>` | `<version>` | `12.8` | `<version>` | `<GiB>` | 通过/失败 |

## 4. 部署与预检

| 检查项 | 结果 | 证据/备注 |
|---|---|---|
| 首次 repo/venv 洁净或发布缓存策略符合模式 | 通过/失败 | `<log-url>` |
| 锁定 repo 重新同步 | 通过/失败 | `<log-url>` |
| 依赖重新安装、附加脚本复制、配置重新渲染 | 通过/失败 | `<log-url>` |
| CUDA 12.8、VRAM >=16 GB、磁盘 | 通过/失败 | `<evidence>` |
| Topology schema 和 service 唯一归属 | 通过/失败 | `<evidence>` |
| 分布式 DNS/端口/防火墙/OpenSSH | 通过/失败/不适用 | `<evidence>` |
| 四节点 host/IP/MachineGuid 与三个 GPU UUID 唯一 | 通过/失败/不适用 | `<evidence>` |
| 三 worker 同时在线 | 通过/失败 | `<evidence>` |
| `/health`、`/capabilities`、`/status` | 通过/失败 | `<evidence>` |
| `artifact-transfer` capability | 通过/失败 | `<evidence>` |

## 5. 自动化结果

| 门禁 | 单机结果/URL | 分布式结果/URL | 备注 |
|---|---|---|---|
| 核心 5 用例 | `<result>` | `<result>` | GPT 两版本、Index 情绪、Cosy 两模式 |
| 每服务 3 条短文本 | `<result>` | `<result>` | 9 条 |
| 30 条混合队列 | `<result>` | `<result>` | `<manifest-url>` |
| 分布式并行重叠 | 不适用 | `<result>` | 至少两个 GPU 节点 |
| `path` 工件模式 | `<result>` | 不适用 | |
| `artifact` 工件模式 | `<result>` | `<result>` | 上传、下载、hash、删除 |
| WAV 自动指标 | `<result>` | `<result>` | `<summary-url>` |
| ASR CER | `<result>` | `<result>` | 单条最大 `<x>`，整体 `<x>` |
| 显存/卸载恢复 | `<result>` | `<result>` | `<nvidia-smi-url>` |
| 性能基线比较 | `<result>` | `<result>` | warm p95 变化 `<x>%` |
| 故障降级/恢复/重试 | `<result>` | `<result>` | `<log-url>` |
| Playwright 工作台闭环 | `<result>` | `<result>` | `<report-url>` |

自动阈值：WAV >1 KiB、0.5-30 秒、RMS > -50 dBFS、削波率 <=1%、静音率 <90%；单条 CER <=0.40、整体 CER <=0.25；无 OOM、峰值空闲显存 >=512 MiB、卸载后 30 秒内回到基线 +1 GiB、冷加载 <=10 分钟、短句 <=5 分钟、warm p95 退化 <=30%。

## 6. 样本听审

评分 1-5。`伪影控制` 分数越高表示伪影越少。每项必须 >=3，总均分必须 >=3.5。

| 样本/版本 ID | Service/模式 | 审核者 | 清晰度 | 音色相似度 | 情绪/韵律 | 伪影控制 | 均分 | 结论/备注 |
|---|---|---|---:|---:|---:|---:|---:|---|
| `<id>` | `GPT v2ProPlus` | `<reviewer>` |  |  |  |  |  |  |
| `<id>` | `GPT v2Pro` | `<reviewer>` |  |  |  |  |  |  |
| `<id>` | `Index emotion-text` | `<reviewer>` |  |  |  |  |  |  |
| `<id>` | `Cosy zero-shot` | `<reviewer>` |  |  |  |  |  |  |
| `<id>` | `Cosy cross-lingual` | `<reviewer>` |  |  |  |  |  |  |

汇总：

| 审核者 | 身份/角色 | 样本数 | 总均分 | 最低单项 | 结论 |
|---|---|---:|---:|---:|---|
| `<reviewer-1>` | `<role>` |  |  |  | 通过/失败 |
| `<reviewer-2>` | `<role>` |  |  |  | 通过/失败/不适用 |

首次 `single-clean` 和首次 `distributed` 认证各需要两名审核者；稳定发布至少一名审核者。

## 7. 故障与例外

| ID | 发现时间 | 节点/服务 | 症状 | 对任务/应用影响 | 恢复时间 | 根因/后续 issue | 是否阻塞 |
|---|---|---|---|---|---|---|---|
| `<id>` | `<time>` | `<service>` | `<description>` | `<impact>` | `<time>` | `<url>` | 是/否 |

例外必须列出批准人、有效期和补测计划。任何自动门禁失败、阈值超限、证据缺失或人工评分不足都不能通过例外直接放行稳定发布。

## 8. 最终签核

| 角色 | 姓名/ID | 决策 | 时间 | 签名或审查 URL |
|---|---|---|---|---|
| 执行人 | `<id>` | 通过/失败 | `<ISO-8601>` | `<url>` |
| 听审核人 1 | `<id>` | 通过/失败 | `<ISO-8601>` | `<url>` |
| 听审核人 2 | `<id>` | 通过/失败/不适用 | `<ISO-8601>` | `<url>` |
| 发布负责人 | `<id>` | 发布/阻止 | `<ISO-8601>` | `<url>` |

最终结论：`通过` / `失败`。

阻塞原因或发布说明：`<text>`
