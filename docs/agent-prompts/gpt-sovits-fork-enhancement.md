# GPT-SoVITS Fork 改造 Prompt

## 角色与目标

你是一个 GPT-SoVITS 仓库的维护者。本仓库是我们团队的 fork（`https://github.com/XucroYuri/GPT-SoVITS`），服务于一个名为"TTS More"的多 TTS 服务调度工作台。TTS More 通过 HTTP API 调用 GPT-SoVITS 进行语音合成。

**目标**：对 GPT-SoVITS 做最小化改造，增加"训练角色（模型名）选择"和"训练样本浏览"两个功能，让 TTS More 能通过 API 获取到之前无法远程获取的训练数据（参考音频 + 参考文本），消除手动填写参考音频路径和文本的痛点。同时解除参考音频时长的硬编码校验限制（3~10 秒），不再因时长阻断合法的生成任务。

## 约束

1. **尽量不改动现有函数的逻辑**，只新增端点和辅助函数
2. **不改动现有 API 的签名和返回格式**（向后兼容）
3. **新增的 API 端点必须在 api_v2.py 中实现**（端口 9880），不依赖 Gradio（端口 9872）
4. **Gradio WebUI 的改动仅限新增组件和事件绑定**，不改动现有组件的 id 或绑定关系
5. 所有新增代码需要 `from __future__ import annotations` 兼容 Python 3.10

## 仓库结构与关键文件

```
GPT-SoVITS/
├── api_v2.py                          # FastAPI REST API 服务（端口 9880）← 主要改造目标
├── GPT_SoVITS/
│   ├── inference_webui.py             # Gradio WebUI（端口 9872）← UI 改造目标
│   └── TTS_infer_pack/
│       └── TTS.py                     # TTS 推理 pipeline（不改动）
├── config.py                           # 全局配置（含 get_weights_names、exp_root="logs"）
└── logs/                               # 训练数据根目录（exp_root）
    └── <exp_name>/                     # 每个角色/实验的训练数据
        ├── 2-name2text.txt             # 文本标注（Tab分隔: wav_name\tphones\tword2ph\tnorm_text）
        ├── 5-wav32k/                   # 32kHz 训练音频
        │   ├── xxx.wav
        │   └── yyy.wav
        └── ...
```

## 数据结构说明

### 权重文件命名规则（已从 s1_train.py / s2_train.py 源码确认）

```
GPT 权重:  GPT_weights[_v2]/<exp_name>-e<epoch>.ckpt
SoVITS 权重: SoVITS_weights[_v2]/<exp_name>_e<epoch>_s<step>.pth
```

`exp_name` 就是用户在训练 WebUI 中填写的"实验/模型名"，也等于 `logs/` 下的目录名。

**从权重路径提取 exp_name 的规则**：
- GPT: 取文件名，去掉 `-e<数字>` 及之后部分。如 `GPT_weights_v2/光头TTS-华-e10.ckpt` → `光头TTS-华`
- SoVITS: 取文件名，去掉 `_e<数字>_s<数字>` 及之后部分。如 `SoVITS_weights_v2/光头TTS-华_e4_s72.pth` → `光头TTS-华`

### 2-name2text.txt 格式（已从 1-get-text.py 源码确认）

每行用 Tab 分隔 4 个字段：
```
<wav_name>\t<phones>\t<word2ph>\t<norm_text>
```
- `wav_name`: 音频文件名（如 `audio_001.wav`），对应 `5-wav32k/` 下的文件
- `norm_text`: 该音频对应的**归一化文本**（如 `你好世界`），这是最关键字段——作为参考文本使用

### logs 目录与权重的对应关系

```
logs/光头TTS-华/         ← exp_name = "光头TTS-华"
├── 2-name2text.txt      ← 训练标注
├── 5-wav32k/            ← 训练音频（32kHz）
│   ├── audio_001.wav    ← 对应 2-name2text.txt 第一行的 wav_name
│   └── audio_002.wav
GPT_weights_v2/光头TTS-华-e10.ckpt      ← 同一 exp_name 的 GPT 权重
SoVITS_weights_v2/光头TTS-华_e4_s72.pth  ← 同一 exp_name 的 SoVITS 权重
```

## 改造任务

### 任务一：api_v2.py 新增 4 个 API 端点

在 `api_v2.py` 的 `if __name__` 之前（第 567 行 `return` 之后），新增以下端点。**不改动任何现有代码**。

#### 端点 1: `GET /models` — 列出所有训练角色（模型名）

扫描 `logs/` 目录，返回每个子目录名（即 exp_name），并附带匹配到的权重文件：

```python
@APP.get("/models")
async def list_models():
    """列出所有训练角色（logs 目录名）及其匹配的权重。

    从 logs/ 目录扫描子目录名作为模型名，再从 GPT/SoVITS 权重目录中
    匹配同名权重文件。支持 GPT_weights、GPT_weights_v2 等 6 个版本目录。
    """
```

**返回格式**（JSON）：
```json
{
  "models": [
    {
      "name": "光头TTS-华",
      "gpt_weights": ["GPT_weights_v2/光头TTS-华-e5.ckpt", "GPT_weights_v2/光头TTS-华-e10.ckpt"],
      "sovits_weights": ["SoVITS_weights_v2/光头TTS-华_e4_s72.pth", "SoVITS_weights_v2/光头TTS-华_e8_s144.pth"],
      "has_training_data": true,
      "sample_count": 15
    }
  ]
}
```

**实现要点**：
- `logs/` 目录不存在时返回空列表
- 用 `config.py` 的 `GPT_weight_root` 和 `SoVITS_weight_root` 列表扫描权重目录
- 从权重文件名提取 exp_name（用正则），与 logs 目录名做匹配
- `sample_count` 来自 `2-name2text.txt` 的行数
- 按 exp_name 字母序排序

#### 端点 2: `GET /models/{model_name}/samples` — 列出训练样本

返回指定角色的训练音频和对应文本：

```python
@APP.get("/models/{model_name}/samples")
async def list_model_samples(model_name: str):
    """列出指定角色的训练样本（音频文件 + 参考文本）。

    读取 logs/<model_name>/2-name2text.txt 解析标注，
    扫描 logs/<model_name>/5-wav32k/ 获取音频文件列表。
    """
```

**返回格式**（JSON）：
```json
{
  "model_name": "光头TTS-华",
  "samples": [
    {
      "audio_name": "audio_001.wav",
      "audio_path": "logs/光头TTS-华/5-wav32k/audio_001.wav",
      "text": "你好世界",
      "lang": "zh"
    }
  ],
  "total": 15
}
```

**实现要点**：
- 解析 `2-name2text.txt`，每行 Tab 分隔 4 字段：`wav_name\tphones\tword2ph\tnorm_text`
- `text` 取第 4 字段（norm_text），`lang` 从 `2-name2text.txt` 无法获取（该文件不含语言），默认 `"zh"`，可从文件名或辅助文件推断
- `audio_path` 返回相对于 GPT-SoVITS 根目录的路径
- 只返回 `5-wav32k/` 中实际存在的音频文件（与 name2text 做交集）
- model_name 含特殊字符时做路径安全检查（防止目录穿越）

#### 端点 3: `GET /status` — 服务状态

返回当前加载的权重和版本信息：

```python
@APP.get("/status")
async def service_status():
    """返回当前服务状态：加载的权重、版本、设备。"""
```

**返回格式**：
```json
{
  "version": "v2",
  "device": "cuda",
  "gpt_weights": "GPT_SoVITS/pretrained_models/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
  "sovits_weights": "GPT_SoVITS/pretrained_models/s2G2333k.pth",
  "languages": ["auto", "auto_yue", "en", "zh", "ja", "yue", "ko", "all_zh", "all_ja", "all_yue", "all_ko"]
}
```

**实现要点**：直接读取 `tts_config` 对象的属性（`version`、`device`、`t2s_weights_path`、`vits_weights_path`、`languages`）。

#### 端点 4: `POST /upload_ref` — 上传参考音频（仅跨机部署需要）

```python
@APP.post("/upload_ref")
async def upload_reference_audio(file: UploadFile = File(...)):
    """上传参考音频文件，返回服务端本地路径供 /tts 使用。

    同机部署不需要此端点（直接传本地路径即可）。
    """
```

**实现要点**：
- 需要在文件头新增 `from fastapi import UploadFile, File`（当前只导入了 `FastAPI, Response`）
- 保存到 `uploaded_audio/` 目录（自动创建）
- 文件名用 `uuid4().hex[:16] + "_" + 原文件名` 避免冲突
- 返回 `{"path": "uploaded_audio/xxxx_ref.wav"}`

### 任务二：api_v2.py 新增辅助函数

在 api_v2.py 的端点之前新增以下辅助函数：

```python
import re
from pathlib import Path
from config import GPT_weight_root, SoVITS_weight_root

def _extract_exp_name_from_gpt_weight(filename: str) -> str:
    """从 GPT 权重文件名提取 exp_name。
    例: '光头TTS-华-e10.ckpt' → '光头TTS-华'
    """
    stem = Path(filename).stem
    return re.split(r"-e\d+", stem, flags=re.IGNORECASE)[0]

def _extract_exp_name_from_sovits_weight(filename: str) -> str:
    """从 SoVITS 权重文件名提取 exp_name。
    例: '光头TTS-华_e4_s72' → '光头TTS-华'
    """
    stem = Path(filename).stem
    return re.split(r"_e\d+_s\d+", stem, flags=re.IGNORECASE)[0]

def _scan_model_weights() -> dict[str, dict[str, list[str]]]:
    """扫描所有权重目录，按 exp_name 分组。
    返回: {"光头TTS-华": {"gpt": [...], "sovits": [...]}}
    """
    grouped: dict[str, dict[str, list[str]]] = {}
    for root in GPT_weight_root:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for f in root_path.iterdir():
            if f.is_file() and f.suffix == ".ckpt":
                name = _extract_exp_name_from_gpt_weight(f.name)
                grouped.setdefault(name, {"gpt": [], "sovits": []})
                grouped[name]["gpt"].append(f"{root}/{f.name}")
    for root in SoVITS_weight_root:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for f in root_path.iterdir():
            if f.is_file() and f.suffix == ".pth":
                name = _extract_exp_name_from_sovits_weight(f.name)
                grouped.setdefault(name, {"gpt": [], "sovits": []})
                grouped[name]["sovits"].append(f"{root}/{f.name}")
    return grouped

def _read_name2text(logs_dir: Path) -> dict[str, dict[str, str]]:
    """读取 2-name2text.txt，返回 {wav_name: {"text": ..., "lang": ...}}。"""
    path = logs_dir / "2-name2text.txt"
    if not path.exists():
        return {}
    output: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        wav_name = parts[0].strip()
        text = parts[3].strip()
        if wav_name and text:
            output[wav_name] = {"text": text, "lang": "zh"}
            output[Path(wav_name).stem] = {"text": text, "lang": "zh"}
    return output
```

### 任务三：Gradio WebUI 增加"模型选择"和"训练样本浏览"

在 `GPT_SoVITS/inference_webui.py` 的 Gradio Blocks 定义中（第 1213 行 `gr.Group()` 模型切换区域之后，第 1232 行 `gr.Markdown(html_center(i18n("*请上传并填写参考信息")` 之前），新增一个"训练角色选择"区域。

**新增 UI 结构**（插入到模型切换区域和参考音频区域之间）：

```python
        # ===== 新增：训练角色选择区域 =====
        with gr.Group():
            gr.Markdown(html_center(i18n("训练角色选择（从训练数据中快速选择）"), "h3"))
            with gr.Row():
                model_name_dropdown = gr.Dropdown(
                    label=i18n("模型名（训练实验名）"),
                    choices=[],
                    value="",
                    interactive=True,
                    scale=14,
                )
                refresh_models_btn = gr.Button(i18n("刷新模型列表"), variant="primary", scale=7)
                auto_select_weights_btn = gr.Button(i18n("自动匹配权重"), variant="secondary", scale=7)
            with gr.Row():
                ref_sample_dropdown = gr.Dropdown(
                    label=i18n("训练样本音频"),
                    choices=[],
                    value="",
                    interactive=True,
                    scale=14,
                )
                ref_sample_player = gr.Audio(
                    label=i18n("样本预览"),
                    type="filepath",
                    scale=14,
                )
            apply_sample_btn = gr.Button(
                i18n("应用选中样本到参考音频和文本"),
                variant="primary",
            )
```

**新增事件处理函数**（在 inference_webui.py 的 Gradio Blocks 定义之前，大约第 1200 行附近添加）：

```python
def scan_model_names() -> list[str]:
    """扫描 logs/ 目录获取所有训练角色名。"""
    from config import exp_root
    import os
    root = Path(exp_root)
    if not root.exists():
        return []
    return sorted([d.name for d in root.iterdir() if d.is_dir() and (d / "2-name2text.txt").exists()])

def on_model_name_change(model_name: str):
    """模型名变化时，加载该模型的训练样本列表。"""
    if not model_name:
        return gr.Dropdown(choices=[], value=""), gr.Audio(value=None)
    from config import exp_root
    logs_dir = Path(exp_root) / model_name
    wav_dir = logs_dir / "5-wav32k"
    samples = []
    if wav_dir.exists():
        name2text = _read_name2text_for_webui(logs_dir)
        for f in sorted(wav_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in (".wav", ".mp3", ".flac"):
                text = name2text.get(f.name, {}).get("text", "") or name2text.get(f.stem, {}).get("text", "")
                label = f"{f.name}" + (f" | {text[:30]}" if text else "")
                samples.append((label, str(f)))
    return gr.Dropdown(choices=[s[0] for s in samples], value=samples[0][0] if samples else ""), gr.Audio(value=samples[0][1] if samples else None)

def on_ref_sample_change(sample_label: str, model_name: str):
    """训练样本选择变化时，更新预览播放器。"""
    if not sample_label or not model_name:
        return gr.Audio(value=None)
    from config import exp_root
    wav_name = sample_label.split(" | ")[0]
    audio_path = Path(exp_root) / model_name / "5-wav32k" / wav_name
    return gr.Audio(value=str(audio_path) if audio_path.exists() else None)

def on_apply_sample(sample_label: str, model_name: str):
    """应用选中样本：写入参考音频路径和参考文本。"""
    if not sample_label or not model_name:
        return None, "", gr.Dropdown()
    from config import exp_root
    wav_name = sample_label.split(" | ")[0]
    audio_path = Path(exp_root) / model_name / "5-wav32k" / wav_name
    logs_dir = Path(exp_root) / model_name
    name2text = _read_name2text_for_webui(logs_dir)
    text = name2text.get(wav_name, {}).get("text", "") or name2text.get(Path(wav_name).stem, {}).get("text", "")
    return str(audio_path) if audio_path.exists() else None, text, gr.Dropdown()

def _read_name2text_for_webui(logs_dir: Path) -> dict[str, dict[str, str]]:
    """读取 2-name2text.txt（WebUI 版，与 api_v2.py 的 _read_name2text 逻辑一致）。"""
    path = logs_dir / "2-name2text.txt"
    if not path.exists():
        return {}
    output: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        wav_name = parts[0].strip()
        text = parts[3].strip()
        if wav_name and text:
            output[wav_name] = {"text": text, "lang": "zh"}
            output[Path(wav_name).stem] = {"text": text, "lang": "zh"}
    return output

def on_auto_select_weights(model_name: str):
    """根据模型名自动匹配并选择最佳 GPT/SoVITS 权重。"""
    if not model_name:
        return gr.Dropdown(), gr.Dropdown()
    weights = _scan_model_weights_for_webui()
    model_weights = weights.get(model_name, {"gpt": [], "sovits": []})
    # 选择最高 epoch 的权重（排序后最后一个）
    gpt_best = sorted(model_weights["gpt"])[-1] if model_weights["gpt"] else None
    sovits_best = sorted(model_weights["sovits"])[-1] if model_weights["sovits"] else None
    # 触发权重切换
    if gpt_best:
        change_gpt_weights(gpt_best)
    if sovits_best:
        prompt_lang = i18n("中文")
        text_lang = i18n("中文")
        try:
            # change_sovits_weights 是 generator，需要消费它
            for _ in change_sovits_weights(sovits_best, prompt_lang, text_lang):
                pass
        except Exception:
            pass
    return gr.Dropdown(value=gpt_best), gr.Dropdown(value=sovits_best)

def _scan_model_weights_for_webui() -> dict[str, dict[str, list[str]]]:
    """扫描权重目录（WebUI 版）。"""
    import re
    from config import GPT_weight_root, SoVITS_weight_root
    grouped: dict[str, dict[str, list[str]]] = {}
    for root in GPT_weight_root:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for f in root_path.iterdir():
            if f.is_file() and f.suffix == ".ckpt":
                stem = f.stem
                name = re.split(r"-e\d+", stem, flags=re.IGNORECASE)[0]
                grouped.setdefault(name, {"gpt": [], "sovits": []})
                grouped[name]["gpt"].append(f"{root}/{f.name}")
    for root in SoVITS_weight_root:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for f in root_path.iterdir():
            if f.is_file() and f.suffix == ".pth":
                stem = f.stem
                name = re.split(r"_e\d+_s\d+", stem, flags=re.IGNORECASE)[0]
                grouped.setdefault(name, {"gpt": [], "sovits": []})
                grouped[name]["sovits"].append(f"{root}/{f.name}")
    return grouped
```

**新增事件绑定**（在 Gradio Blocks 定义内，第 1408 行 `GPT_dropdown.change(...)` 之后添加）：

```python
        # ===== 新增事件绑定 =====
        refresh_models_btn.click(
            fn=scan_model_names,
            inputs=[],
            outputs=[model_name_dropdown],
        )
        model_name_dropdown.change(
            fn=on_model_name_change,
            inputs=[model_name_dropdown],
            outputs=[ref_sample_dropdown, ref_sample_player],
        )
        ref_sample_dropdown.change(
            fn=on_ref_sample_change,
            inputs=[ref_sample_dropdown, model_name_dropdown],
            outputs=[ref_sample_player],
        )
        apply_sample_btn.click(
            fn=on_apply_sample,
            inputs=[ref_sample_dropdown, model_name_dropdown],
            outputs=[inp_ref, prompt_text, ref_sample_dropdown],
        )
        auto_select_weights_btn.click(
            fn=on_auto_select_weights,
            inputs=[model_name_dropdown],
            outputs=[GPT_dropdown, SoVITS_dropdown],
        )
        # 页面加载时自动扫描模型列表
        app.load(fn=scan_model_names, inputs=[], outputs=[model_name_dropdown])
```

### 任务四：更新 api_v2.py 的 import

在 api_v2.py 第 119 行修改 FastAPI import，添加 UploadFile 和 File：

```python
# 原代码（第 119 行）:
from fastapi import FastAPI, Response

# 改为:
from fastapi import FastAPI, Response, UploadFile, File
```

### 任务五：解除参考音频时长的硬编码限制

**背景**：GPT-SoVITS 在两处对参考音频时长做了硬编码校验（3~10 秒），超出范围直接 `raise OSError` 阻断生成。但实际上超出此范围的参考音频仍可正常推理（只是质量和显存占用有差异），这个校验过于严苛，导致合法的生成任务被禁止。

**涉及文件和精确行号**（两处校验逻辑完全相同）：

#### 位置 1：`GPT_SoVITS/inference_webui.py` 第 853-856 行

```python
# 当前代码（第 853-856 行）:
wav16k, sr = librosa.load(ref_wav_path, sr=16000)
if wav16k.shape[0] > 160000 or wav16k.shape[0] < 48000:
    gr.Warning(i18n("参考音频在3~10秒范围外，请更换！"))
    raise OSError(i18n("参考音频在3~10秒范围外，请更换！"))
```

#### 位置 2：`GPT_SoVITS/TTS_infer_pack/TTS.py` 第 815-817 行

```python
# 当前代码（第 815-817 行，_set_prompt_semantic 方法内）:
wav16k, sr = librosa.load(ref_wav_path, sr=16000)
if wav16k.shape[0] > 160000 or wav16k.shape[0] < 48000:
    raise OSError(i18n("参考音频在3~10秒范围外，请更换！"))
```

**改造要求**：

将两处硬编码的时长阻断改为**软警告**——超限时打印日志/Warning 提示但不 `raise`，让推理继续执行。同时将阈值改为可配置。

#### inference_webui.py 第 853-856 行改为：

```python
wav16k, sr = librosa.load(ref_wav_path, sr=16000)
ref_duration = wav16k.shape[0] / 16000
if ref_duration < 3 or ref_duration > 10:
    gr.Warning(i18n(f"参考音频时长 {ref_duration:.1f} 秒，超出推荐的 3~10 秒范围，可能影响合成质量"))
```

**改动要点**：
- 删除 `raise OSError(...)` 行，改为仅 `gr.Warning` 提示
- 用 `ref_duration` 变量（秒）替代原始采样点数比较，提高可读性
- Warning 文案从"请更换！"改为"可能影响合成质量"（建议性而非阻断性）

#### TTS.py 第 815-817 行改为：

```python
wav16k, sr = librosa.load(ref_wav_path, sr=16000)
ref_duration = wav16k.shape[0] / 16000
if ref_duration < 3 or ref_duration > 10:
    print(f"[Warning] Reference audio duration {ref_duration:.1f}s is outside the recommended 3~10s range, proceeding anyway")
```

**改动要点**：
- 删除 `raise OSError(...)` 行
- TTS.py 是 pipeline 层（非 Gradio），不能用 `gr.Warning`，改用 `print` 输出警告
- 不中断执行流程，让后续的 HuBERT 特征提取和 VQ 编码正常进行

#### Gradio UI 标签文案更新（inference_webui.py 第 1234 行）

```python
# 当前代码（第 1234 行）:
inp_ref = gr.Audio(label=i18n("请上传3~10秒内参考音频，超过会报错！"), type="filepath", scale=13)

# 改为:
inp_ref = gr.Audio(label=i18n("请上传参考音频（推荐3~10秒）"), type="filepath", scale=13)
```

**改动要点**：标签从"超过会报错！"改为"推荐3~10秒"——不再用恐吓性文案，改为推荐性提示。

## 验证清单

完成改造后，需要验证：

1. **`GET /models` 返回正确的模型列表**（含 logs 目录名和匹配权重）
2. **`GET /models/{name}/samples` 返回训练样本**（含音频路径和文本）
3. **`GET /status` 返回当前权重状态**
4. **`POST /upload_ref` 能上传文件并返回路径**
5. **Gradio WebUI 中选择模型名后，样本下拉菜单自动填充**
6. **点击"应用选中样本"后，参考音频和文本被正确填入**
7. **点击"自动匹配权重"后，GPT/SoVITS 下拉菜单选中对应权重**
8. **现有功能不受影响**（`/tts`、`/set_gpt_weights`、`/set_sovits_weights` 正常工作）
9. **所有新增端点返回的音频路径都是服务端可访问的相对路径**（相对于 GPT-SoVITS 根目录）
10. **时长校验已解除**：传入 2 秒或 15 秒的参考音频不再报错阻断，仅打印/Warning 提示，合成正常完成
11. **inference_webui.py 和 TTS.py 两处校验都已改为软警告**（不 raise）
12. **Gradio 参考音频标签文案已更新**为推荐性提示
