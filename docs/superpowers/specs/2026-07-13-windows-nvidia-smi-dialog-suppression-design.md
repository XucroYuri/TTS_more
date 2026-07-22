# Windows nvidia-smi 后台探测弹窗抑制设计

## 背景

从 TTS More 与 GPT-SoVITS 的独立 `Start.cmd` 启动并进行局部联调时，Windows 曾弹出 `nvidia-smi.exe - Application Error (0xc0000142)`。随后进行的独立命令、TTS More 硬件状态接口和 GPT Worker 状态接口验证均成功：NVIDIA 驱动文件签名有效、CUDA 可用、系统无待重启标记。因此当前证据不支持持续性驱动损坏，更符合后台硬件探测子进程偶发初始化失败且错误对话框进入交互桌面的情形。

## 目标

- TTS More 应用本体及 GPT-SoVITS、IndexTTS、CosyVoice Worker 在后台调用 `nvidia-smi` 时不显示控制台或模态错误窗口。
- 保留现有超时、退出码和输出解析逻辑。
- `nvidia-smi` 缺失、超时或异常退出时，接口继续返回现有的降级状态，不阻塞应用或 Worker 启动。
- Windows 之外的平台行为保持不变。

## 非目标

- 不修复或替换 NVIDIA 驱动。
- 不移除 `nvidia-smi`，也不引入 NVML 或 PyTorch 作为 TTS More 应用本体的新依赖。
- 不修改 GPU 认证流程的通过标准。
- 不吞掉状态接口中的诊断信息。

## 方案选择

采用应用内统一保护方案：增加一个小型 Windows 子进程辅助模块，由 TTS More 本体的硬件探测和三个 Worker 共用。

未采用的方案：

- 仅修改 `Start.cmd`：不能覆盖 Worker 被服务管理器、Python 或用户直接启动的场景。
- 用 NVML/PyTorch 完全替换 `nvidia-smi`：会增加依赖和交付复杂度，并且 TTS More 本体不应为了状态探测加载完整 GPU 推理运行时。

## 组件与数据流

辅助模块负责两件事：

1. Windows 下为当前后台服务进程启用非交互式错误处理模式，使其创建的探测子进程不向桌面展示系统错误对话框。设置必须幂等，并保留进程已有错误模式位。
2. 为 `subprocess.run` 提供平台相关参数。Windows 使用 `CREATE_NO_WINDOW`；非 Windows 返回空参数。

调用路径保持简单：

```text
状态接口
  -> 配置非交互式子进程错误处理
  -> subprocess.run(nvidia-smi, 平台参数, timeout)
  -> 成功：解析 GPU/UUID
  -> 失败：返回 degraded 或空 UUID
```

接入点限定为：

- `backend/app/hardware.py`：TTS More 本体 GPU 状态探测。
- `backend/app/workers/runtime.py`：GPT-SoVITS、IndexTTS、CosyVoice 的统一 CUDA UUID 探测。

## 错误处理

- Windows API 不可用或设置失败时，辅助模块不得让应用启动失败；仍继续执行原有探测并依靠现有异常处理降级。
- `nvidia-smi` 超时、不可执行或返回非零退出码时，不弹窗、不抛到 API 边界。
- TTS More 本体继续返回 `available=false, status=degraded` 及错误文本。
- Worker 继续返回 `device_uuid=null`，其余 CUDA 和显存信息仍尽可能由 PyTorch 提供。

## 测试设计

按测试驱动顺序实施：

1. 为辅助模块添加平台行为测试，证明 Windows 参数包含 `CREATE_NO_WINDOW`，非 Windows 不附加 Windows 参数。
2. 添加 Windows 错误模式测试，证明既有模式位被保留、目标抑制位被加入，调用失败时安全降级。
3. 为 `hardware.py` 添加调用测试，证明 `nvidia-smi` 探测使用统一保护参数且现有解析结果不变。
4. 扩展 Worker 运行时测试，证明 UUID 探测使用相同保护参数且异常时仍返回空 UUID。
5. 运行相关测试、完整后端回归和 `git diff --check`。
6. 从 TTS More 与 GPT-SoVITS 的独立 `Start.cmd` 重新启动，重复调用 `/api/services/status`、`/api/startup/checks` 和 Worker `/status`，确认接口、CUDA 信息和进程状态正常且不产生弹窗。

## 验收标准

- 两条 `nvidia-smi` 调用链均使用同一平台辅助模块。
- Windows 后台探测没有控制台或系统错误弹窗。
- 模拟失败不会阻塞 API 或启动过程。
- TTS More 与 GPT-SoVITS 的真实启动和状态接口验证通过。
- 不影响工作区中与本修复无关的现有改动。
