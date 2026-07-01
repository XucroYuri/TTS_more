from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}
GPT_WEIGHT_SUFFIXES = {".ckpt", ".pth", ".pt"}
SOVITS_WEIGHT_SUFFIXES = {".pth", ".ckpt", ".pt"}


def collect_voice_candidates(
    reference_audio_root: Path,
    gpt_weights_roots: list[Path],
    sovits_weights_roots: list[Path],
    indextts_model_dir: Path,
    runtime_checks: dict[str, dict[str, Any]] | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    reference = _scan_reference_audio(reference_audio_root, limit=limit)
    gpt_weights, gpt_diags = _scan_files(gpt_weights_roots, GPT_WEIGHT_SUFFIXES, limit=limit)
    sovits_weights, sovits_diags = _scan_files(sovits_weights_roots, SOVITS_WEIGHT_SUFFIXES, limit=limit)
    index_model = _check_indextts_model(indextts_model_dir)
    ready = bool(reference["exists"] and reference["groups"] and gpt_weights and sovits_weights and index_model["ready"])
    return {
        "ready": ready,
        "runtimes": _check_python_runtimes(runtime_checks or {}),
        "reference_audio": reference,
        "gpt_sovits": {
            "gpt_weights": gpt_weights,
            "sovits_weights": sovits_weights,
            "diagnostics": [*gpt_diags, *sovits_diags],
        },
        "indextts": {
            "reference_audio": reference["groups"],
            "model": index_model,
            "diagnostics": [] if reference["exists"] else [{"path": str(reference_audio_root), "status": "missing"}],
        },
    }


def _check_python_runtimes(checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for name, check in checks.items():
        python = str(check.get("python", "python"))
        modules = [str(module) for module in check.get("modules", [])]
        missing: list[str] = []
        error: str | None = None
        for module in modules:
            try:
                completed = subprocess.run(
                    [python, "-c", f"import {module}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                missing.extend([module for module in modules if module not in missing])
                error = str(exc)
                break
            if completed.returncode != 0:
                missing.append(module)
                error = (completed.stderr or completed.stdout).strip()
        output[name] = {"python": python, "ready": not missing, "missing_modules": missing, "error": error}
    return output


def _scan_files(roots: list[Path], suffixes: set[str], limit: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    files: list[dict[str, str]] = []
    diagnostics: list[dict[str, str]] = []
    for root in roots:
        if not root.exists():
            diagnostics.append({"path": str(root), "status": "missing"})
            continue
        if not root.is_dir():
            diagnostics.append({"path": str(root), "status": "not a directory"})
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in suffixes:
                files.append({"name": path.name, "path": str(path)})
                if len(files) >= limit:
                    return files, diagnostics
    return files, diagnostics


def _scan_reference_audio(root: Path, limit: int) -> dict[str, Any]:
    if not root.exists():
        return {"path": str(root), "exists": False, "is_dir": False, "groups": []}
    groups: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        samples = [
            str(path)
            for path in child.rglob("*")
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
        ]
        if samples:
            groups.append({"id": child.name, "name": child.name, "path": str(child), "audio_count": len(samples), "samples": samples[:5]})
        if len(groups) >= limit:
            break
    return {"path": str(root), "exists": True, "is_dir": root.is_dir(), "groups": groups}


def _check_indextts_model(model_dir: Path) -> dict[str, Any]:
    required = [
        "config.yaml",
        "bpe.model",
        "gpt.pth",
        "s2mel.pth",
        "wav2vec2bert_stats.pt",
        "feat1.pt",
        "feat2.pt",
        "qwen0.6bemo4-merge",
        "hf_cache/semantic_codec_model.safetensors",
        "hf_cache/campplus_cn_common.bin",
        "hf_cache/bigvgan/config.json",
        "hf_cache/bigvgan/bigvgan_generator.pt",
        "hf_cache/w2v-bert-2.0",
    ]
    missing = [name for name in required if not (model_dir / name).exists()]
    return {"path": str(model_dir), "ready": not missing, "missing": missing}
