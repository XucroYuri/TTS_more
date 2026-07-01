from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.models import EngineName


class IndexTTSSubprocessAdapter:
    engine = EngineName.INDEX_TTS

    def __init__(self, repo_dir: Path, python_exe: str = "python") -> None:
        self.repo_dir = repo_dir.resolve(strict=False)
        self.python_exe = python_exe
        self.loaded_profile: str | None = None

    def health(self) -> dict[str, Any]:
        cli = self.repo_dir / "indextts" / "cli_v2.py"
        return {"engine": self.engine.value, "ready": cli.exists(), "cli": str(cli)}

    def load(self, profile: str) -> None:
        self.loaded_profile = profile

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        voice = request.parameters.get("voice")
        if not voice:
            raise RuntimeError("IndexTTS voice reference path is required")
        output_path = request.output_path.resolve(strict=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        project_root = Path(__file__).resolve().parents[3]
        launcher = project_root / "backend" / "app" / "workers" / "indextts_line_launcher.py"
        command = [
            self.python_exe,
            str(launcher),
            "--text",
            request.line.text,
            "--voice",
            str(voice),
            "--output",
            str(output_path),
            "--repo-dir",
            str(self.repo_dir),
        ]
        command += self._parameter_args(request)
        completed = subprocess.run(
            command,
            cwd=self.repo_dir,
            text=True,
            capture_output=True,
            check=False,
            timeout=float(request.parameters.get("timeout_seconds", 900)),
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip())
        return SynthesisResult(audio_path=output_path, metadata={"stdout": completed.stdout.strip()})

    def unload(self) -> None:
        self.loaded_profile = None

    def _parameter_args(self, request: SynthesisRequest) -> list[str]:
        params = request.parameters
        args: list[str] = []
        model_dir = params.get("model_dir")
        if model_dir:
            args += ["--model-dir", str(model_dir)]
        emotion_mode = str(params.get("emotion_mode", "emotion_text" if request.line.note else "same_as_voice"))
        if emotion_mode == "emotion_audio" and params.get("emotion_audio"):
            args += ["--emotion-audio", str(params["emotion_audio"])]
        elif emotion_mode == "emotion_vector" and params.get("emotion_vector") is not None:
            args += ["--emotion-vector", ",".join(str(item) for item in params.get("emotion_vector", []))]
        elif emotion_mode == "emotion_text":
            emotion_text = str(params.get("emotion_text") or request.line.note or "")
            if emotion_text:
                args += ["--emotion-text", emotion_text]
        if params.get("emotion_weight") is not None:
            args += ["--emotion-weight", str(params["emotion_weight"])]
        if params.get("emotion_random"):
            args.append("--emotion-random")
        bool_map = {
            "do_sample": "--do-sample",
        }
        for key, flag in bool_map.items():
            if params.get(key) is not None:
                args.append(flag if params.get(key) else f"--no-{flag[2:]}")
        numeric_flags = {
            "top_p": "--top-p",
            "top_k": "--top-k",
            "temperature": "--temperature",
            "length_penalty": "--length-penalty",
            "num_beams": "--num-beams",
            "repetition_penalty": "--repetition-penalty",
            "max_mel_tokens": "--max-mel-tokens",
            "max_text_tokens_per_segment": "--max-text-tokens-per-segment",
        }
        for key, flag in numeric_flags.items():
            if params.get(key) is not None:
                args += [flag, str(params[key])]
        return args
