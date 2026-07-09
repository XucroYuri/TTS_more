# TTS More — cross-platform development Makefile.
# Works on macOS, Linux, and Windows. On Windows it targets native cmd /
# PowerShell (make is uncommon there; PowerShell users can use scripts/*.ps1
# directly). Under Git Bash/MSYS2 the .sh scripts work but $(OS) is still
# Windows_NT, so the Windows venv path is selected — run via the .ps1 scripts
# if the MSYS path translation causes trouble.

ROOT := $(shell pwd)
BACKEND_PY := $(ROOT)/.venv/bin/python
ifeq ($(OS),Windows_NT)
	BACKEND_PY := $(ROOT)/.venv/Scripts/python.exe
endif

.PHONY: help install install-backend install-frontend dev workers test test-backend test-frontend build lint clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: install-backend install-frontend ## Install all dependencies

install-backend: ## Create .venv and install backend[dev] (uses uv if available)
	@if command -v uv >/dev/null 2>&1; then \
	  uv venv --python 3.11 .venv; \
	  uv pip install --python $(BACKEND_PY) -e 'backend[dev]'; \
	else \
	  python3.11 -m venv .venv; \
	  $(BACKEND_PY) -m pip install -e 'backend[dev]'; \
	fi

install-frontend: ## Install frontend dependencies with pnpm
	cd frontend && pnpm install

dev: ## Start backend and frontend (cross-platform)
	@ifeq ($(OS),Windows_NT)
		powershell -ExecutionPolicy Bypass -File scripts/start-dev.ps1
	@else
		scripts/start-dev.sh
	@endif

workers: ## Start the three non-invasive TTS workers (GPT-SoVITS/IndexTTS/CosyVoice)
	@ifeq ($(OS),Windows_NT)
		powershell -ExecutionPolicy Bypass -File scripts/start-service-workers.ps1
	@else
		scripts/start-service-workers.sh
	@endif

test: test-backend test-frontend ## Run all tests

test-backend: ## Run backend pytest suite
	$(BACKEND_PY) -m pytest backend -q

test-frontend: ## Run frontend vitest suite
	cd frontend && pnpm test

build: ## Build the frontend production bundle
	cd frontend && pnpm build

lint: ## Type-check the frontend
	cd frontend && pnpm exec tsc --noEmit

clean: ## Remove build artifacts and caches
	rm -rf frontend/dist backend/.pytest_cache .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
