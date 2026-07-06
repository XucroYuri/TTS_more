# TTS More

TTS More is an outer orchestration project for local and external TTS services:

- `GPT-SoVITS` for trained character weights and reference-audio generation.
- `index-tts` for strong emotional speech from per-character references.
- `CosyVoice` for zero-shot, cross-lingual, and instruction-style open-source TTS.
- TTS API and generic HTTP endpoints are kept as optional placeholders while the core workflow focuses on the three open-source providers.

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
py -3.10 -m venv .venv
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

## Open-Source TTS Services

TTS More itself is installed first:

```powershell
git clone https://github.com/XucroYuri/TTS_more.git
```

If you do not already have compatible TTS services, clone one or more of the supported open-source projects. The `repo/` folder is the recommended local convention, but any local path can be bound later in the app.

```powershell
git clone https://github.com/XucroYuri/GPT-SoVITS.git repo/GPT-SoVITS
git clone https://github.com/XucroYuri/index-tts.git repo/index-tts
git clone https://github.com/XucroYuri/CosyVoice.git repo/CosyVoice
```

These forks are stable mirrors for TTS More integration. Compatible upstream deployments can also be used as long as their HTTP contract matches.

In the app, open `服务与资源 -> 开源接入` to choose one of four access paths:

- Local repo: bind a local project path and optionally configure start/stop/log commands. Inference still uses the endpoint URL.
- Local endpoint: connect to a service already running on `127.0.0.1` or `0.0.0.0`.
- LAN endpoint: connect to a trusted machine by IP and port.
- Public URL: connect to a cloud or public endpoint. Process control is not available for remote services.

Configurations created by the onboarding flow are written to `data/local/services.json`. Templates under `data/templates/` stay sanitized and should not contain local paths, LAN IPs, generated audio, or private role bindings.

The built-in provider order is:

`GPT-SoVITS -> IndexTTS -> CosyVoice -> TTS API`

TTS API providers are currently placeholders in the product flow. The main reliability work targets the three open-source TTS services.

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

The routing layer uses provider type, API contract, capabilities, voice bindings, health, priority, and resource group. Default priority keeps GPT-SoVITS first, IndexTTS second, and CosyVoice third, with TTS API or generic HTTP providers available as opt-in placeholders. VibeVoice is no longer a local core model; register it as an external generic HTTP endpoint only if you still want to use it.

For a deeper deployment and scheduling guide, see [docs/open-source-tts-services.md](docs/open-source-tts-services.md).

Reference audio and trained weights are loaded from local runtime configuration. Keep private paths in `.env.local` or `data/local/services.json`; the repository only ships neutral templates under `data/templates/`.

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
