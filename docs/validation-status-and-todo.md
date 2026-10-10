# 阶段验收与 TO DO（2026-10-10）

本阶段交付实验目录与参考绑定修复、可选原生 GPU 协调开发源码，以及 Windows 命令行记忆。已有真实剧本的完整合成、音频历史和导出证据保存在本机；本阶段新增包装层仍只完成离线验证和配置检查，未部署到生产原生实例。

## 已完成

- 同名多版本实验具有唯一选项，保留完整训练名称和数字前缀；GPT、SoVITS 权重分别绑定。Inspector 独立加载当前服务目录。
- 自动默认参考必须有标注且实测为 3–10 秒；保留全部日志样本，不再只看前八个。切换实验/参考时清除旧权重别名、音频和标注。
- GPU 协调包含 300 秒原子空闲判定、原生优先准入、持久 dirty 使用记录、明确的模型/缓存适配、Gradio 及双入口 ASGI 包装。
- 后端让出与用户取消分开；已完成台词保留，当前未提交台词等待重试，释放后清除驻留签名。Suite 的受支持 TTS 文本/SRT 入口在模型构造前取得实际使用权。
- 本机专用配置保留原解释器、参数、环境和端口，固定关键源码哈希；配置检查不导入模型。真实配置、凭证、音频、模型和证据均不提交。
- [Windows 命令行记忆](windows-agent-memory.md) 已接入 AGENTS.md。[GPU 协调说明](native-gpu-coordination.md) 记录协议、能力和部署边界。

实验目录阶段 [PR #43](https://github.com/XucroYuri/TTS_more/pull/43)、协调源码 [PR #48](https://github.com/XucroYuri/TTS_more/pull/48) 和插件 [PR #28](https://github.com/XucroYuri/TTS-Audio-Suite/pull/28) 均已合并，对应完整跨平台 CI 通过。离线覆盖包括 CPU 适配器、真实回环 HTTP、300 秒边界、流式取消、清理失败、重启持久阻挡、LAN 资源组隔离及台词精确一次恢复；这些不代替生产 GPU 合成验证。

本次最终本地针对回归：后端 337 通过、1 跳过（后端环境无 torch）；真实 torch CPU 适配器测试已在独立依赖环境通过。前端 126 项及生产构建通过；Suite 针对回归 185 项通过。Suite 的 CI 触发分支也补齐了实际默认分支 master，保留上游 main。

## TO DO

| 优先级 | 待完成工作 | Issue | 验收边界 |
|---|---|---|---|
| P1 | 安排维护启用包装，并验证真实卸载、恢复与原生优先 | [TTS_more #44](https://github.com/XucroYuri/TTS_more/issues/44) | 每个原生入口使用真实权重验证，PID/端口不变、显存下降、恢复后音频有效 |
| P1 | 实测并约束各引擎让出及清理期限 | [Suite #26](https://github.com/XucroYuri/TTS-Audio-Suite/issues/26) | 外部子进程和 CUDA 分配清理后才确认 clean；未支持模式保持拒绝 |
| P1 | 安全恢复持久 dirty 使用记录 | [TTS_more #45](https://github.com/XucroYuri/TTS_more/issues/45) | 独立核实旧 GPU 持有者退出，保留审计记录；禁止盲 reset/删除日志 |
| P1 | 明确并扩展直接/混合工作流准入覆盖 | [Suite #27](https://github.com/XucroYuri/TTS-Audio-Suite/issues/27) | 所有允许的 GPU 路径先准入再分配；未门控插件不能声称受保护 |
| P2 | 完成当前构建的真实浏览器用户流程验收 | [TTS_more #46](https://github.com/XucroYuri/TTS_more/issues/46) | 实际操作实验、两组权重、参考/标注、历史、取消和导出 |
| P2 | 改善不完整资源提示，并恢复可选 LAN 并行补测 | [TTS_more #47](https://github.com/XucroYuri/TTS_more/issues/47) | 缺权重/确切参考保持 pending；LAN 故障不影响独立本机资源组 |
| P3 | 改善 Windows 后端重启时的端口释放等待 | [TTS_more #49](https://github.com/XucroYuri/TTS_more/issues/49) | 有界等待自有旧进程及监听退出；拒绝干预意外端口占用者；新后端 HTTP 就绪后才报告成功 |

## 本阶段的实际边界

现有原生应用保持运行；尚未对它们进行本阶段包装部署、重启或进程注入。自动 GPU 协调保持关闭，不能描述为已完成生产共存验收。首次启用仍需用户安排维护窗口。

收尾时仅在队列为空后刷新 TTSMore 后端以加载当前代码，HTTP 已恢复且队列为空；原生 TTS 与 ComfyUI 的 PID 保持不变。首次重启遇到旧监听尚未释放，确认释放后启动成功，此时序优化已记入 #49。

协调入口当前保护 Suite 受支持的 TTS 工作流，其他 GPU 插件未受门控。启用时使用专用 TTS ComfyUI 实例；没有安全协作中断契约的进程内模式拒绝准入，默认关闭模式保留原有行为。

浏览器接口已提供，但本机验收地址仍被工具报告的已保存权限规则拦截，禁止换浏览器绕过。该状态只说明界面验收未完成，不构成产品界面失败结论。可选 LAN 服务最新检查不可达，新增参考上传与并行实测待其恢复后继续。
