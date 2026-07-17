# Windows 包内可重定位 Python 运行时设计

状态：已批准（延续方案 A，并由 2026-07-17 真实 Full 构建故障触发修订）
日期：2026-07-17

## 1. 问题与目标

四仓 Full 构建在把 Miniforge 安装到包内 `data/cache/portable/conda/...` 时失败。相同锁定安装器在短目录成功，但在两种深层物理路径及短盘符映射下均以 `InvalidArchiveError`/`BrokenProcessPool` 失败。短盘符无效是因为 Miniforge 的工作进程最终解析到底层物理路径。

目标是落实已批准的方案 A：Bootstrap 和 Full 的用户运行时由包内官方 Python embeddable ZIP、锁定 uv wheel 和现有依赖锁构建；目标设备不需要系统 Python、Conda、Node 或外部 `base_prefix`。Conda 只保留为源码构建兼容适配器，不参与便携包的初始化、启动、修复或 Full 运行时。

## 2. 资产与版本

TTS More、GPT-SoVITS main 和 IndexTTS 使用官方 CPython 3.11.9 Windows x64 embeddable ZIP：

- URL：`https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip`
- 大小：`11249023`
- SHA-256：`009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b`

CosyVoice 使用官方 CPython 3.10.11 Windows x64 embeddable ZIP：

- URL：`https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip`
- 大小：`8629277`
- SHA-256：`608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d`

四仓继续使用已锁定的 uv 0.11.28 Windows x64 wheel：

- wheel 内执行文件：`uv-0.11.28.data/scripts/uv.exe`
- 大小：`27603677`
- SHA-256：`f4fcf2c8d9f1444b900e6b8dbbb828825fb76eca01acd18aeaa5c90240408cda`

所有 URL、大小和哈希写入 runtime lock。运行时不得动态解析 Python 或 uv 的最新版本。

## 3. 公共运行时准备接口

新增同步文件 `portable-python.ps1`，由 TTS More 控制器和三个 worker 共用同一行为。公开函数为：

```powershell
Install-PortablePythonRuntime `
  -PackageRoot <absolute package root> `
  -RuntimeLock <absolute runtime.lock.json> `
  -Destination <absolute runtime/staging> `
  -OperationRoot <optional operation directory> `
  -CancelFile <optional cancellation marker>
```

该函数执行：

1. 从 runtime lock 读取 `assets.python` 和 `assets.uv`。
2. 使用现有 `portable_install.py ensure-asset` 下载到包内 `data/cache/portable/assets`，保持 `.partial`、Range 续传、镜像回退、大小和 SHA-256 约束。
3. 将 Python ZIP 解压到同级临时目录，不覆盖现有 `runtime/live`。
4. 验证 ZIP 只包含相对安全路径，不允许绝对路径、盘符、`..`、reparse point 或重复目标。
5. 将 `<major><minor>._pth` 配置为 `pythonXY.zip`、`.`、`Lib\site-packages` 和 `import site`，不得生成 `pyvenv.cfg`。
6. 用 .NET `ZipArchive` 从 uv wheel 提取唯一、精确路径的 `uv.exe` 到包内 `data/cache/portable/tools/uv-0.11.28/uv.exe`；拒绝缺失、重复或路径漂移。
7. 创建 `Lib/site-packages`，运行 Python 版本探针，返回 `python.exe`、`uv.exe` 和 site-packages 路径。

函数不读取系统 Python、Conda、pip、tar、7-Zip 或用户级 uv。PowerShell 与 .NET ZIP API 是 Windows 包唯一外部基础条件。

## 4. 初始化数据流

控制器和 worker 的 Initialize 流程改为：

```text
锁/空间/取消预检
  -> 下载并校验 Python ZIP 与 uv wheel
  -> Install-PortablePythonRuntime(runtime/staging)
  -> 用 staging Python 运行 portable_install.py
  -> 选择设备 profile 与下载锁定 runtime/model payload
  -> uv lock --check / export（适用时）
  -> uv pip install --python staging/python.exe --target staging/Lib/site-packages
  -> uv pip check --python staging/python.exe
  -> Python import、Torch、ONNX、FFmpeg、模型探针
  -> 原子切换 staging -> live
  -> 写入 install-state.json
```

uv 安装必须显式使用 `--link-mode copy`，避免缓存跨卷时硬链接告警，也避免 Full 包引用缓存 inode。启动器继续用 `runtime/live/python.exe -m ...`，不依赖 `Scripts` console launcher。

`Repair.cmd` 复用同一流程，只处理缺失或损坏的锁定资产，保留 `data/user` 和上一个可用 `runtime/live`。

## 5. Conda 适配器边界

`bootstrap-conda.ps1` 和 Miniforge toolchain lock 保留给源码构建、上游开发脚本或明确的兼容适配器。下列用户路径不得调用它：

- 根 `Initialize.cmd` / `Start.cmd` / `Repair.cmd`；
- `Build-Package.ps1 -Profile Full` 对 staging 包执行的初始化；
- Full 包启动、修复与离线验收。

Full ZIP 不得包含 Miniforge 安装目录、Conda package cache、`pyvenv.cfg` 或构建机前缀。Bootstrap ZIP 可以包含 Python/uv 的锁和下载配方，但不得包含下载后的运行时。

## 6. 四仓同步

规范源文件包括：

- `integrations/windows/portable-python.ps1`
- `integrations/windows/Initialize.ps1`
- worker runtime locks 与集成 manifest

同步器把这些文件复制到三个 fork 的 `tts_more/`，更新逐文件 SHA-256。TTS More 控制器使用 `scripts/portable-python.ps1`；其实现必须与 integration helper 的安全和事务语义一致，允许目录布局差异但不允许算法漂移。

三个 fork 的正式 Python 版本分别由 `component.json` 与 runtime lock 共同约束：GPT/Index 为 3.11.9，CosyVoice 为 3.10.11。版本不匹配必须在依赖安装前失败。

## 7. 验收门禁

自动化必须证明：

- 初始化脚本不再调用 `bootstrap-conda.ps1` 或 `conda create`；
- Python/uv 资产 URL、大小和 SHA-256 完整；
- Python ZIP 路径穿越、重复文件、错误 `_pth`、错误版本和损坏哈希失败关闭；
- uv wheel 只接受精确的 `uv.exe` entry；
- 无系统 Python/Conda/uv 的 fixture 初始化仍通过；
- runtime 中没有 `pyvenv.cfg`、外部 `base_prefix`、Conda 目录或机器绝对路径；
- runtime 复制到中文、空格、不同盘符和随机目录后，核心 import 与 `uv pip check` 通过；
- 四个 Full ZIP 真实构建产生 4x6 组件资产及兼容矩阵、provenance；
- 随机路径断网执行 `Start.cmd -> Stop.cmd -> Repair.cmd`，三个 worker 完成真实短音频合成。

未完成真实 Full 构建和随机路径断网业务验证前，不得宣称四包可正式交付。
