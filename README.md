# TTS More

TTS More is an outer orchestration project for local and external TTS services:

- `GPT-SoVITS` for trained character weights and reference-audio generation.
- `index-tts` for strong emotional speech from per-character references.
- Commercial and generic HTTP endpoints for optional network TTS providers.

The outer app keeps local repos independent and adds a FastAPI orchestration layer plus a React script dubbing workstation.

## Workbench

- Product-style three-column script dubbing workstation: service/resources sidebar, script line task table, and line inspector.
- Chinese and English UI through i18next; Chinese is the fallback, with browser language detection and a top-bar language switch.
- File-based projects and manifests.
- Character library with reusable voice bindings, model profiles, service overrides, and reference audio groups.
- OpenAI-compatible multi-provider parser contract with rule-based fallback.
- Service endpoint registry in `data/services.json`, so local repo workers and remote API services share one scheduling model.
- Queue grouping by service/profile/resource group, with same-resource serial execution and different-resource parallel execution.
- Real network endpoint mode by default, using the standard worker contract for `/health`, `/capabilities`, `/load`, `/synthesize`, and `/unload`.
- Geist-inspired compact visual baseline is stored in `frontend/design.md`.

## Setup

```powershell
& 'C:\Users\xuyu_\AppData\Roaming\uv\python\cpython-3.10.20-windows-x86_64-none\python.exe' -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e 'backend[dev]'
cd frontend
pnpm install
```

Copy `.env.example` to `.env.local` to configure local endpoint paths, parser providers, or commercial TTS keys. Do not commit `.env.local`.

## Run

```powershell
.\scripts\start-dev.ps1
```

Backend: `http://127.0.0.1:8000`  
Frontend: `http://127.0.0.1:5173`

## Service Mode

TTS More defaults to real network endpoint mode. Local and remote services are both called through the URLs in `data/services.json`; a stopped local service is shown as not started and is not treated as ready.

For local GPT-SoVITS and IndexTTS generation:

1. Prepare local model resources:

   ```powershell
   .\scripts\prepare-models.ps1 -Source ModelScope -Device CU128
   ```

   The script creates separate virtual environments under `repo/GPT-SoVITS/.venv` and `repo/index-tts/.venv`; downloads IndexTTS checkpoints to `repo/index-tts/checkpoints`; and writes suggested real-mode values to `.env.local` when that file does not exist.

2. Start local standard workers with `.\scripts\start-service-workers.ps1`. Add `-StartGPTSoVITS` if the GPT-SoVITS Python environment is ready.
3. Edit `data/services.json` for remote machines by adding external endpoints with their own `resource_group`.
4. Optional commercial TTS providers are first-class services. Configure keys in `.env.local` only; `services.json` references env var names such as `OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, and the Volcengine app/token/cluster variables.

The routing layer uses provider type, API contract, capabilities, voice bindings, health, priority, and resource group. Default priority keeps GPT-SoVITS first and IndexTTS second, with commercial or generic HTTP providers available as opt-in profiles or lower-priority candidates. VibeVoice is no longer a local core model; register it as an external generic HTTP endpoint only if you still want to use it.

Reference audio is scanned from `\\192.168.2.12\ai\项目\音色克隆\音源归档` by default. GPT-SoVITS trained weights are expected under `\\192.168.2.12\ai\项目\音色克隆\模型训练`.

## Verify

```powershell
& .\.venv\Scripts\python.exe -m pytest backend
cd frontend
pnpm test -- --run
pnpm build
```

Real core-model validation is gated because it requires large models and local GPU resources:

```powershell
$env:TTS_MORE_SERVICE_MODE="real"
$env:TTS_MORE_RUN_REAL_TTS="1"
& .\.venv\Scripts\python.exe -m pytest backend/tests/test_real_tts_validation.py -q
```
