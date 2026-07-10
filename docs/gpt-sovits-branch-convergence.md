# GPT-SoVITS 分支收敛

本文记录 `XucroYuri/GPT-SoVITS` 三个开发分支的职责、收敛顺序和 TTS More 部署选择。它描述的是发布流程，不代表尚未通过门禁的代码已经合入 `main`。

## 分支职责

| 分支 | 职责 | 默认部署 |
|---|---|---|
| `main` | 上游兼容的 fork 产品分支；当前仍锁定同步上游的基线，收敛 PR 通过后承载 fork 功能 | 是 |
| `dev` | 主动适配和新功能的开发、回归来源；收敛期间只读保留 | 否 |
| `xucroyuri/proplus-hc-dev` | 早期探索分支；只吸收可兼容能力，不直接合并旧基线 | 否 |

长期流向固定为：`upstream/main → dev` 同步和验证 → 独立收敛分支 → fork `main` 产品发布。收敛工作使用 `dev-xu/gpt-sovits-main-convergence`，不重写 `dev` 或 `proplus-hc-dev` 历史。

旧分支 tip 由 `archive/proplus-hc-dev-pre-convergence-2026-07-10` 标签永久保留。首个稳定产品版本发布后，才删除远端 `proplus-hc-dev` 分支。

## 部署选择

`repo.lock.json` 使用 `default_selected` 区分正式服务和回归实例：

- 默认：GPT-SoVITS `main`、IndexTTS、CosyVoice。
- `--targets dev`：只准备 GPT-SoVITS `dev` 回归实例。
- `--targets all`：准备锁文件中的全部实例，包括旧 `proplus-hc-dev`。

提交的 `data/services.json` 只呈现正式服务。回归配置必须由显式目标重新生成，避免用户无意启动三个 GPT-SoVITS 实例并占用多份显存。

## 合并门禁

收敛 PR 必须先完成 Python 3.9–3.12、Gradio `<5`、API、安全上传、静态检查和 LF 检查。随后在真实 CUDA 设备上验证：

1. 默认 `v2ProPlus` 合成。
2. 显式切换 `v2Pro` 合成。
3. `/models`、样本发现、`/upload_ref` 和 `/tts` 联动。
4. TTS More worker 的发现、加载和合成。

CUDA 门禁未通过时不得合入 fork `main`，也不得删除远端旧分支。Windows 启动、便携打包和 ASR 本地路径属于后续独立 PR，不阻塞本次核心代码评审。
