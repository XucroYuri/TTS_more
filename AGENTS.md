# TTS More -- AI Agent Onboarding Instructions

## Project Identity
- **Name**: TTS More (tts-more-workstation / tts-more-backend)
- **Type**: Original project -- script dubbing workstation on top of GPT-SoVITS, IndexTTS, CosyVoice
- **Package**: backend `tts-more-backend` v0.1.0, frontend `tts-more-workstation` v0.1.0
- **Stack**: Python 3.11 (FastAPI backend) + React 19/Vite 7/TypeScript 5.9 (frontend), pnpm
- **Remote**: `origin` = XucroYuri/TTS_more, default branch = **master** (NOT main)
- **License**: See LICENSE file

## Quick Reference (after initial setup)

```bash
# Backend
cd backend
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
pytest                      # Run backend tests
uvicorn app.main:app        # Start backend on 127.0.0.1:8000

# Frontend
cd frontend
pnpm install                # Install dependencies
pnpm run dev                # Start Vite dev server
pnpm run build              # Production build
pnpm run test               # Run frontend tests
pnpm exec vitest run        # Alternative test runner
```

## Architecture

```
flowchart LR
    Browser["React Workstation"] -- "HTTP /api" --> Backend["FastAPI Backend"]
    Backend -- "Schedule/Synthesize" --> ComfyUI["ComfyUI Task Queue / TTS-Audio-Suite"]
    ComfyUI --> Models["GPT-SoVITS / IndexTTS / CosyVoice"]
    Backend -- "Schedule/Synthesize" --> Remote["Remote APIs"]
    Backend -- "Read/Write" --> Data[("data/ Projects/Roles/Config")]
    ComfyUI -- "Output Audio" --> Data
    Remote -- "Return Audio" --> Backend
```

Main workflow: `Script -> Extract Lines -> Character Voices -> TTS Provider -> Generate Lines -> Preview History`. Left panel handles scripts, center handles line generation, right handles voice/reference/actions for the current line. Bilingual i18n (zh/en), Chinese fallback.

Current production path: TTS More calls ComfyUI HTTP API, with `XucroYuri/TTS-Audio-Suite` hosting the TTS engines and real task queue. The three upstream TTS projects are retained only as model/data resources.

### Backend Module Dependencies

```
flowchart TD
    main[main.py: FastAPI app + routes]
    main --> storage[storage.py: Project/manifest/role storage]
    main --> services[services.py: Service registry + routing + clients]
    main --> queue[queue.py: Generation queue + job management]
    main --> parser[parser.py: Multi-provider script parsing]
    main --> supervisor[supervisor.py: Local service lifecycle]
    main --> open[open_source_tts.py: Open-source TTS integration]
    main --> auth[auth.py: Optional token middleware]
    main --> net[net_guard.py: SSRF protection + sanitization]
    main --> comfyui[comfyui/: ComfyUI HTTP Client + Workflow Builder]
    services --> models[models.py: Pydantic data models]
    services --> comfyui
    queue --> services
```

Three core boundaries:
- **Frontend** (`frontend/`): Single-page React app, main workflow left-to-right panel layout, i18n
- **Backend** (`backend/app`): FastAPI, project/role storage, parsers, service routing, generation queue, service supervision. Binds `127.0.0.1:8000`
- **Workers/External Services**: Local repo workers (Gradio WebUI or standard worker contract) and commercial APIs, all called via `base_url` registered in `data/services.json`

## Critical Files

| File | Role |
|------|------|
| `backend/app/main.py` | FastAPI app entry point -- all routes registered here |
| `backend/app/services.py` | Service registry, routing, and TTS client implementations |
| `backend/app/queue.py` | Generation queue, job lifecycle management |
| `backend/app/storage.py` | Project, manifest, and character library storage (JSON file-based) |
| `backend/app/parser.py` | Multi-provider script parsing (extract lines from screenplay formats) |
| `backend/app/models.py` | Pydantic data models shared across the backend |
| `backend/app/supervisor.py` | Local service process lifecycle (start/stop/monitor) |
| `backend/app/auth.py` | Optional token-based authentication middleware |
| `backend/app/net_guard.py` | SSRF protection, URL sanitization, network safety |
| `backend/app/comfyui/` | ComfyUI HTTP client and workflow builder module |
| `frontend/src/App.tsx` | React root component, workspace layout |
| `frontend/src/api.ts` | Frontend API client for backend communication |
| `frontend/src/i18n.ts` | Internationalization setup (zh/en, Chinese fallback) |
| `frontend/src/types.ts` | TypeScript type definitions |
| `frontend/src/components/` | React component library (workspace panels, controls) |
| `docs/architecture.md` | Full architecture documentation |
| `docs/comfyui-integration.md` | ComfyUI deployment and integration guide |
| `docs/security.md` | Security model documentation |

## Development Rules

1. **Local-first**: Backend binds `127.0.0.1` by default. Never expose to public networks without explicit configuration.
2. **Python version**: Must use Python >=3.11,<3.12. Use `uv` or standard `venv` for virtual environment management.
3. **Frontend tooling**: Use `pnpm` exclusively for frontend package management. Node >=20 required.
4. **Branch strategy**: Default branch is **master** (NOT main). Push directly to master for this project.
5. **i18n**: All user-facing text must support zh/en bilingual with Chinese as fallback.
6. **File-based storage**: Project data, character libraries, and service configs are stored as JSON files under `data/`. No database required.
7. **ComfyUI integration**: TTS generation flows through ComfyUI's HTTP API with `XucroYuri/TTS-Audio-Suite` plugin. See `docs/comfyui-integration.md`.
8. **Security**: SSRF protection via `net_guard.py`, path safety via `path_safety.py`, optional token auth.
9. **Testing**: `pytest` for backend, `vitest` for frontend. Backend tests in `backend/tests/`, frontend tests co-located or in `frontend/e2e/`.
10. **Makefile available**: `make install` for cross-platform dependency setup.

## Avoid

- Exposing the backend on non-localhost interfaces without explicit review
- Using npm or yarn for frontend -- pnpm only
- Storing audio files (*.wav, *.mp3, *.flac, etc.) in git (already gitignored)
- Committing large model files (*.safetensors, *.ckpt, *.pth, *.pt)
- Hardcoding credentials in config files -- use `.env` or environment variables
- Modifying upstream TTS project repos under `integrations/` unless explicitly asked
