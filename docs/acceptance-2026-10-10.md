# GPT-SoVITS / IndexTTS 验收记录

范围仅包含 GPT-SoVITS、IndexTTS 和 Windows 启动可靠性。两个引擎的实际生成、项目历史存储、音频 HTTP 返回和工作台播放器通过；自动剧本分析仍存在以下阻塞，不能视为全自动流程全部通过。

## 真实生成结果

验收剧本为两条短中文台词。GPT-SoVITS 通过工作台生成；IndexTTS 使用同一剧本与参考音频，通过前端 `/api` 代理提交后端异步队列，再由 ComfyUI 执行。所有输出验证非空波形和下载字节一致；浏览器确认最新 IndexTTS 结果进入播放状态。

| 引擎 | 样本 | 采样率 | 时长 | 波形峰值 | 结果 |
| --- | --- | --- | --- | --- | --- |
| GPT-SoVITS | 台词 1 | 32000 Hz | 3.020 s | 0.576 | 成功 |
| GPT-SoVITS | 台词 2 | 32000 Hz | 2.860 s | 0.481 | 成功 |
| IndexTTS | 台词 1 | 22050 Hz | 3.646 s | 0.936 | 成功 |
| IndexTTS | 台词 2 | 22050 Hz | 3.170 s | 0.746 | 成功 |

额外提交一条 IndexTTS 任务，在生成过程中同时启动两个 `E:\Start-TTSMore.bat --no-browser` 进程。两个启动均返回 0，三个服务 PID 不变，原任务仍可查询并最终 completed。共验证 5 个输出，全部经前端代理返回 HTTP 200，字节与磁盘文件一致。只验证功能、输出结构及播放状态，没有对音色质量进行主观听评。

## 启动验收

本机另一个 checkout 占用了 `8000/5173`；原 ComfyUI 位于 `8188`，只加载了旧资源表。新启动器复用当前项目的 `8001/5174`，在 `8189` 找到加载 GPT-SoVITS 和 IndexTTS 注册表的实例。没有终止另一个 checkout 的服务。

Windows PowerShell 5.1 实测与测试覆盖：端口占用后回退、启动期间抢占后重试、相似目录归属区分、错误前端代理拒绝、依赖错误快速退出、PID 记录原子写入和并发重复启动。新 ComfyUI 的后续启动使用独立数据库文件；验收期间的已有 `8189` 实例曾报告默认数据库锁提示，但音频生成正常。

本机 IndexTTS 原配置仍指向旧目录和旧 worker 契约，已改为资源表注册和 ComfyUI 契约。资源路径、权重、角色库、生成音频和本机服务配置只保存在本机，不纳入 Git。

## 已修复

- 固定端口占用直接失败；每次启动重启已存在服务。
- Windows PowerShell 5.1 JSON 数组嵌套导致就绪识别失败，以及空备份路径导致 `File.Replace` 失败。
- 多 ComfyUI 实例默认 SQLite 文件锁冲突：新实例使用独立文件。
- IndexTTS `use_cuda_kernel=false` 布尔参数被写成 `"False"`，不匹配节点枚举；现转换为 `"false"`，兼容 true/false/auto。
- 启动器无条件重载服务配置导致任务查询消失；现比较实际路由，只有变化时才在队列空闲后重载。
- 两处已有前端测试遗漏 `use_environment_proxy` 字段，已同步断言。

## 待处理的问题与阻塞

1. **自动分析阻塞**：一次“开始分析”出现两个分析请求，其中一次返回 `semantic_contract_invalid / 422`。另一份分析成功，但界面停留在失败草稿。本次经 API 确认成功草稿并补角色绑定后继续验收；该绕行不能证明自动分析通过。重复请求可能与开发模式 StrictMode 的 effect 重入有关，原因尚未修复。
2. **历史分析恢复入口**：历史列表中的成功分析缺少直接打开/恢复操作；重开剧本还会恢复失败草稿。需要改进恢复逻辑。
3. **生成历史刷新**：此前 GPT-SoVITS 任务完成后，第二行短暂显示“未生成”；重新打开项目可看到完成结果。最终 manifest 刷新可能被 effect 清理取消，待修复。
4. **服务设置重载接口**：底层 `_apply_registry` 仍会重建队列和任务管理器。启动器已避免生成中重载，但通过其他设置入口直接重载仍需单独修复。
5. **队列进度准确性**：ComfyUI 实际运行时外部状态仍可显示 queued，进度长时间停在 35%；生成最终成功。

## 验证

- 后端启动器与 ComfyUI 专项测试：68 passed；未运行整个后端测试套件。
- 前端：48 个测试文件、302 项测试全部通过。
- `pnpm run build`：成功；保留单个约 518 kB JS chunk 的体积提示。
- 后端测试依赖有 Starlette/AnyIO 弃用提示，不影响验收。

详细本机证据位于 `data/local/run/chain-validation-20261010/`：`final-audio-validation.json`、`active-launch-verification.json`、`active-launch-job.json`、`workstation-index-completed.png` 和先前的分析诊断。本机证据目录已忽略，不上传业务素材。
