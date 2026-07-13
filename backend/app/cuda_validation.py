from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import quantiles
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import SynthesisRequest
from app.lan_evidence import LanOrchestrationPreflight
from app.lan_topology import LanPolicy, LanTopology, load_lan_policy
from app.models import ScriptLine, TTSServiceEndpoint
from app.services import HttpTTSServiceClient, ServiceRegistry, TTSServiceClient, build_service_client


VALIDATION_MODES = (
    "single-clean",
    "single-release",
    "distributed",
    "lan-shared",
    "lan-distributed",
)
LOCAL_SINGLE_MODES = frozenset({"single-clean", "single-release"})
STRICT_LAN_MODES = frozenset({"lan-shared", "lan-distributed"})
EXTERNAL_LAN_MODES = frozenset({"distributed", *STRICT_LAN_MODES})
DISTINCT_GPU_MODES = frozenset({"distributed", "lan-distributed"})
CERTIFICATION_STATUSES = frozenset(
    {
        "blocked",
        "core_failed",
        "diagnostic_core_passed",
        "core_passed_ui_pending",
        "automatic_passed_human_pending",
    }
)
POST_CORE_STAGES = frozenset({"fault-recovery", "evidence-collection"})
FORMAL_SERVICE_IDS = {
    "gpt_sovits": "local-gpt-sovits-main",
    "indextts": "local-indextts",
    "cosyvoice": "local-cosyvoice",
}

MIN_WAV_BYTES = 1024
MIN_DURATION_SECONDS = 0.5
MAX_DURATION_SECONDS = 30.0
MIN_RMS_DBFS = -50.0
MAX_CLIPPING_RATIO = 0.01
MAX_SILENCE_RATIO = 0.90
MAX_ITEM_CER = 0.40
MAX_AGGREGATE_CER = 0.25
MIN_FREE_MEMORY_MIB = 512.0
MIN_TOTAL_MEMORY_MIB = 16_000.0
REQUIRED_CUDA_RUNTIME = "12.8"
MAX_UNLOAD_MEMORY_DELTA_MIB = 1024.0
MAX_UNLOAD_SECONDS = 30.0
MAX_COLD_LOAD_SECONDS = 600.0
MAX_SHORT_SYNTHESIS_SECONDS = 300.0
MAX_WARM_P95_REGRESSION = 0.30
WARM_SYNTHESIS_REPEATS = 2
GPU_MONITOR_REQUIRED_ERROR = (
    "GPU monitor is unavailable; nvidia-smi evidence is required for certification"
)
ASR_BATCH_RELEASE_ERROR = (
    "ASR batch blocked: TTS unload and memory recovery were not confirmed"
)


class ServiceIds(BaseModel):
    gpt_sovits: Literal["local-gpt-sovits-main"]
    indextts: Literal["local-indextts"]
    cosyvoice: Literal["local-cosyvoice"]


class ReferencePaths(BaseModel):
    gpt_sovits: str
    indextts: str
    cosyvoice: str


class WeightPair(BaseModel):
    gpt: str
    sovits: str


class GPTWeights(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    v2_pro_plus: WeightPair = Field(alias="v2ProPlus")
    v2_pro: WeightPair = Field(alias="v2Pro")


class Prompt(BaseModel):
    text: str
    language: str = "zh"


class Prompts(BaseModel):
    gpt: Prompt
    cosyvoice: Prompt
    index_emotion: str


class TestTexts(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    gpt_v2_pro_plus: str = Field(alias="gpt_v2ProPlus")
    gpt_v2_pro: str = Field(alias="gpt_v2Pro")
    index_emotion: str
    cosyvoice_zero_shot: str
    cosyvoice_cross_lingual: str


class Reviewer(BaseModel):
    id: str
    name: str


class ASRSettings(BaseModel):
    required: Literal[True] = True
    model: Literal["large-v3"] = "large-v3"
    language: str = "zh"


class PerformanceBaseline(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    warm_p95_seconds: float = Field(gt=0, le=MAX_SHORT_SYNTHESIS_SECONDS)


class DistributedOrchestrationPreflight(BaseModel):
    schema_version: Literal[1]
    mode: Literal["distributed"]
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    controller_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class ValidationFixture(BaseModel):
    schema_version: Literal[1]
    name: str
    service_ids: ServiceIds
    references: ReferencePaths
    gpt_weights: GPTWeights
    prompts: Prompts
    test_texts: TestTexts
    reviewers: list[Reviewer] = Field(min_length=1)
    asr: ASRSettings = Field(default_factory=ASRSettings)
    performance_baseline: PerformanceBaseline | None = None
    worker_logs: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class ValidationCase:
    name: str
    service_id: str
    profile: str
    text: str
    language: str
    parameters: dict[str, Any]


class Transcriber(Protocol):
    def __call__(self, audio_path: Path, language: str | None = None) -> str:
        ...


def _load_fixture_bytes(raw_bytes: bytes) -> ValidationFixture:
    raw = json.loads(raw_bytes.decode("utf-8"))
    expanded = _expand_environment(raw)
    return ValidationFixture.model_validate(expanded)


def load_fixture(path: Path) -> ValidationFixture:
    return _load_fixture_bytes(path.read_bytes())


def _append_required_file_failure(
    failures: list[str], kind: str, label: str, raw_path: str
) -> None:
    if _contains_unresolved_environment(raw_path):
        failures.append(f"{kind} {label} contains an unresolved environment variable")
    elif not Path(raw_path).is_file():
        failures.append(f"{kind} {label} not found")


def _is_windows_absolute_path(value: str) -> bool:
    normalized = value.strip()
    if re.match(r"^[A-Za-z]:[\\/].+", normalized):
        return True
    return bool(re.match(r"^(?:\\\\|//)[^\\/]+[\\/][^\\/]+(?:[\\/].+)?$", normalized))


def _preflight_next_action(failures: list[str]) -> str:
    if not failures:
        return "继续部署和完整 CUDA 验证。"
    actions: list[str] = []
    categories = (
        (lambda message: message.startswith("fixture validation failed:"), "修复 fixture JSON 或 schema"),
        (
            lambda message: message.startswith(("reference ", "weight ")),
            "补齐或修正 reference audio 和 GPT/SoVITS weight 路径",
        ),
        (lambda message: message.startswith("ASR gate requires "), "安装 faster-whisper 并确保 large-v3 可用"),
        (
            lambda message: message.startswith("an approved performance baseline is required"),
            "补充已批准的 performance baseline",
        ),
        (lambda message: "listening reviewer" in message, "补充当前模式所需的 listening reviewers"),
    )
    for matches, action in categories:
        if any(matches(message) for message in failures):
            actions.append(action)
    if not actions:
        actions.append("解决 preflight 列出的阻塞项")
    return "；".join(actions) + "，然后重新运行相同命令。"


def validate_fixture_inputs(
    fixture_path: Path,
    *,
    mode: str,
    require_baseline: bool,
) -> tuple[ValidationFixture | None, list[str]]:
    fixture, _fixture_sha256_value, failures = _load_fixture_snapshot(
        fixture_path,
        mode=mode,
        require_baseline=require_baseline,
    )
    return fixture, failures


def _load_fixture_snapshot(
    fixture_path: Path,
    *,
    mode: str,
    require_baseline: bool,
) -> tuple[ValidationFixture | None, str | None, list[str]]:
    try:
        raw_bytes = fixture_path.read_bytes()
    except Exception as exc:
        return None, None, [f"fixture validation failed: {exc}"]
    fixture_sha256_value = hashlib.sha256(raw_bytes).hexdigest()
    try:
        fixture = _load_fixture_bytes(raw_bytes)
    except Exception as exc:
        return None, fixture_sha256_value, [f"fixture validation failed: {exc}"]
    failures: list[str] = []
    for label, raw_path in fixture.references.model_dump().items():
        _append_required_file_failure(failures, "reference", label, str(raw_path))
    for version, pair in fixture.gpt_weights.model_dump(by_alias=True).items():
        for kind, raw_path in pair.items():
            raw_path = str(raw_path)
            if mode in EXTERNAL_LAN_MODES:
                if not raw_path.strip():
                    failures.append(f"weight {version}.{kind} is empty")
                elif _contains_unresolved_environment(raw_path):
                    failures.append(
                        f"weight {version}.{kind} contains an unresolved environment variable"
                    )
                elif not _is_windows_absolute_path(raw_path):
                    failures.append(
                        f"weight {version}.{kind} must be a Windows absolute path"
                    )
            else:
                _append_required_file_failure(failures, "weight", f"{version}.{kind}", raw_path)
    if require_baseline and mode != "single-clean" and fixture.performance_baseline is None:
        failures.append("an approved performance baseline is required")
    if importlib.util.find_spec("faster_whisper") is None:
        failures.append("ASR gate requires faster-whisper with large-v3")
    required_reviewers = 2 if mode == "single-clean" else 1
    if len(fixture.reviewers) < required_reviewers:
        failures.append(f"{mode} requires {required_reviewers} listening reviewers")
    reviewer_ids = [reviewer.id.strip() for reviewer in fixture.reviewers]
    reviewer_names = [reviewer.name.strip() for reviewer in fixture.reviewers]
    if any(not value for value in [*reviewer_ids, *reviewer_names]):
        failures.append("listening reviewers require non-empty IDs and names")
    elif len({reviewer_id.casefold() for reviewer_id in reviewer_ids}) != len(
        reviewer_ids
    ):
        failures.append("listening reviewer IDs must be unique")
    return fixture, fixture_sha256_value, failures


def validation_cases(fixture: ValidationFixture) -> list[ValidationCase]:
    references = fixture.references
    prompts = fixture.prompts
    texts = fixture.test_texts
    weights = fixture.gpt_weights
    return [
        ValidationCase(
            name="gpt-v2ProPlus",
            service_id=fixture.service_ids.gpt_sovits,
            profile="gpt-v2ProPlus",
            text=texts.gpt_v2_pro_plus,
            language=prompts.gpt.language,
            parameters={
                "gpt_weights_path": weights.v2_pro_plus.gpt,
                "sovits_weights_path": weights.v2_pro_plus.sovits,
                "ref_audio_path": references.gpt_sovits,
                "prompt_text": prompts.gpt.text,
                "prompt_lang": prompts.gpt.language,
                "text_lang": prompts.gpt.language,
                "media_type": "wav",
            },
        ),
        ValidationCase(
            name="gpt-v2Pro",
            service_id=fixture.service_ids.gpt_sovits,
            profile="gpt-v2Pro",
            text=texts.gpt_v2_pro,
            language=prompts.gpt.language,
            parameters={
                "gpt_weights_path": weights.v2_pro.gpt,
                "sovits_weights_path": weights.v2_pro.sovits,
                "ref_audio_path": references.gpt_sovits,
                "prompt_text": prompts.gpt.text,
                "prompt_lang": prompts.gpt.language,
                "text_lang": prompts.gpt.language,
                "media_type": "wav",
            },
        ),
        ValidationCase(
            name="index-emotion-text",
            service_id=fixture.service_ids.indextts,
            profile="index-emotion-text",
            text=texts.index_emotion,
            language="zh",
            parameters={
                "voice": references.indextts,
                "emotion_mode": "emotion_text",
                "emotion_text": prompts.index_emotion,
            },
        ),
        ValidationCase(
            name="cosyvoice-zero-shot",
            service_id=fixture.service_ids.cosyvoice,
            profile="cosyvoice-zero-shot",
            text=texts.cosyvoice_zero_shot,
            language=prompts.cosyvoice.language,
            parameters={
                "mode": "zero_shot",
                "ref_audio_path": references.cosyvoice,
                "prompt_text": prompts.cosyvoice.text,
                "response_format": "wav",
            },
        ),
        ValidationCase(
            name="cosyvoice-cross-lingual",
            service_id=fixture.service_ids.cosyvoice,
            profile="cosyvoice-cross-lingual",
            text=texts.cosyvoice_cross_lingual,
            language="en",
            parameters={
                "mode": "cross_lingual",
                "ref_audio_path": references.cosyvoice,
                "response_format": "wav",
            },
        ),
    ]


def measure_wav(path: Path) -> dict[str, Any]:
    size_bytes = path.stat().st_size
    channels, sample_width, sample_rate, frame_count, samples, peak, encoding = _read_wav_audio(path)
    if sample_rate <= 0 or channels <= 0:
        raise ValueError(f"invalid WAV format: {path}")
    duration = frame_count / sample_rate
    if samples:
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        rms_dbfs = 20.0 * math.log10(rms / peak) if rms else -120.0
        clipping_ratio = sum(abs(sample) >= peak * 0.99 for sample in samples) / len(samples)
        silence_limit = peak * (10.0 ** (MIN_RMS_DBFS / 20.0))
        silence_ratio = sum(abs(sample) <= silence_limit for sample in samples) / len(samples)
    else:
        rms_dbfs = -120.0
        clipping_ratio = 0.0
        silence_ratio = 1.0
    checks = {
        "size": size_bytes > MIN_WAV_BYTES,
        "duration": MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS,
        "rms": rms_dbfs > MIN_RMS_DBFS,
        "clipping": clipping_ratio <= MAX_CLIPPING_RATIO,
        "silence": silence_ratio < MAX_SILENCE_RATIO,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "size_bytes": size_bytes,
        "duration_seconds": round(duration, 6),
        "rms_dbfs": round(rms_dbfs, 4),
        "clipping_ratio": round(clipping_ratio, 6),
        "silence_ratio": round(silence_ratio, 6),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "encoding": encoding,
    }


def _read_wav_audio(path: Path) -> tuple[int, int, int, int, list[int] | list[float], float, str]:
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            frames = source.readframes(frame_count)
        peak = float((1 << (sample_width * 8 - 1)) - 1)
        return channels, sample_width, sample_rate, frame_count, _decode_pcm(frames, sample_width), peak, "pcm"
    except wave.Error:
        return _read_ieee_float_wav(path)


def _read_ieee_float_wav(path: Path) -> tuple[int, int, int, int, list[float], float, str]:
    payload = path.read_bytes()
    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise ValueError(f"invalid WAV container: {path}")
    fmt: bytes | None = None
    frames = b""
    offset = 12
    while offset + 8 <= len(payload):
        chunk_id = payload[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", payload, offset + 4)[0]
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_size
        if chunk_end > len(payload):
            raise ValueError(f"truncated WAV chunk: {path}")
        if chunk_id == b"fmt ":
            fmt = payload[chunk_start:chunk_end]
        elif chunk_id == b"data":
            frames = payload[chunk_start:chunk_end]
        offset = chunk_end + (chunk_size % 2)
    if fmt is None or len(fmt) < 16 or not frames:
        raise ValueError(f"WAV is missing fmt or data: {path}")
    audio_format, channels, sample_rate, _byte_rate, block_align, bits_per_sample = struct.unpack_from(
        "<HHIIHH", fmt
    )
    if audio_format != 3 or bits_per_sample not in {32, 64}:
        raise ValueError(f"unsupported WAV encoding {audio_format}/{bits_per_sample}: {path}")
    sample_width = bits_per_sample // 8
    if block_align != channels * sample_width or block_align <= 0:
        raise ValueError(f"invalid WAV block alignment: {path}")
    usable = len(frames) - len(frames) % sample_width
    format_code = "<f" if sample_width == 4 else "<d"
    samples = [item[0] for item in struct.iter_unpack(format_code, frames[:usable])]
    if any(not math.isfinite(sample) for sample in samples):
        raise ValueError(f"WAV contains non-finite samples: {path}")
    return channels, sample_width, sample_rate, len(frames) // block_align, samples, 1.0, "ieee-float"


def character_error_rate(reference: str, hypothesis: str) -> float:
    reference_chars = list(_normalize_transcript(reference))
    hypothesis_chars = list(_normalize_transcript(hypothesis))
    if not reference_chars:
        return 0.0 if not hypothesis_chars else 1.0
    return _edit_distance(reference_chars, hypothesis_chars) / len(reference_chars)


def evaluate_cer(items: list[tuple[str, str, str]]) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    total_edits = 0
    total_reference_chars = 0
    for name, reference, hypothesis in items:
        normalized_reference = _normalize_transcript(reference)
        normalized_hypothesis = _normalize_transcript(hypothesis)
        edits = _edit_distance(list(normalized_reference), list(normalized_hypothesis))
        denominator = len(normalized_reference)
        cer = edits / denominator if denominator else (0.0 if not normalized_hypothesis else 1.0)
        total_edits += edits
        total_reference_chars += denominator
        reports.append(
            {
                "name": name,
                "reference": reference,
                "hypothesis": hypothesis,
                "cer": cer,
                "passed": cer <= MAX_ITEM_CER,
            }
        )
    aggregate = total_edits / total_reference_chars if total_reference_chars else 0.0
    return {
        "required": True,
        "items": reports,
        "aggregate_cer": aggregate,
        "thresholds": {"per_item": MAX_ITEM_CER, "aggregate": MAX_AGGREGATE_CER},
        "passed": all(item["passed"] for item in reports) and aggregate <= MAX_AGGREGATE_CER,
    }


class FasterWhisperTranscriber:
    def __init__(self, whisper_model: Any, model_name: str, language: str) -> None:
        self._whisper_model = whisper_model
        self._model_name = model_name
        self._language = language
        self._model: Any = None

    def __call__(self, audio_path: Path, language: str | None = None) -> str:
        if self._model is None:
            device = os.environ.get("TTS_MORE_VALIDATION_ASR_DEVICE", "cuda")
            compute_type = os.environ.get("TTS_MORE_VALIDATION_ASR_COMPUTE_TYPE", "float16")
            self._model = self._whisper_model(self._model_name, device=device, compute_type=compute_type)
        segments, _ = self._model.transcribe(
            str(audio_path), language=language or self._language, beam_size=5
        )
        return "".join(str(segment.text) for segment in segments).strip()

    def close(self) -> None:
        self._model = None
        try:
            import torch
        except ImportError:
            return
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def create_transcriber(*, required: bool, model_name: str, language: str) -> tuple[Transcriber | None, str]:
    if not required:
        return None, ""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        return None, (
            f"ASR gate requires faster-whisper with {model_name}; install faster-whisper "
            "in the validation environment before running the CUDA gate"
        )
    return FasterWhisperTranscriber(WhisperModel, model_name, language), ""


def evaluate_performance(metrics: dict[str, Any], baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    synthesis_times = [float(item) for item in metrics.get("short_synthesis_seconds") or []]
    minimum_free = _optional_float(metrics.get("minimum_free_mib"))
    baseline_memory = _optional_float(metrics.get("baseline_memory_mib"))
    unload_memory = _optional_float(metrics.get("unload_memory_mib"))
    unload_seconds = _optional_float(metrics.get("unload_recovery_seconds"))
    cold_load = _optional_float(metrics.get("cold_load_seconds"))
    warm_p95 = _optional_float(metrics.get("warm_p95_seconds"))
    approved_p95 = _optional_float((baseline or {}).get("warm_p95_seconds"))
    warm_passed = True if approved_p95 is None else warm_p95 is not None and warm_p95 <= approved_p95 * 1.30
    checks = {
        "no_oom": not bool(metrics.get("oom", False)),
        "free_memory_reserve": minimum_free is not None and minimum_free >= MIN_FREE_MEMORY_MIB,
        "unload_memory_return": (
            baseline_memory is not None
            and unload_memory is not None
            and unload_memory <= baseline_memory + MAX_UNLOAD_MEMORY_DELTA_MIB
            and unload_seconds is not None
            and unload_seconds <= MAX_UNLOAD_SECONDS
        ),
        "cold_load": cold_load is not None and cold_load <= MAX_COLD_LOAD_SECONDS,
        "short_synthesis": bool(synthesis_times) and max(synthesis_times) <= MAX_SHORT_SYNTHESIS_SECONDS,
        "warm_p95_regression": warm_passed,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "metrics": metrics,
        "baseline": baseline or {},
        "thresholds": {
            "minimum_free_mib": MIN_FREE_MEMORY_MIB,
            "unload_memory_delta_mib": MAX_UNLOAD_MEMORY_DELTA_MIB,
            "unload_seconds": MAX_UNLOAD_SECONDS,
            "cold_load_seconds": MAX_COLD_LOAD_SECONDS,
            "short_synthesis_seconds": MAX_SHORT_SYNTHESIS_SECONDS,
            "warm_p95_regression": MAX_WARM_P95_REGRESSION,
        },
    }


class NvidiaSmiMonitor:
    HEADER = [
        "captured_at",
        "gpu_timestamp",
        "index",
        "uuid",
        "memory_total_mib",
        "memory_free_mib",
        "memory_used_mib",
        "utilization_gpu_percent",
    ]

    def __init__(self, output_path: Path, interval_seconds: float = 2.0) -> None:
        self.output_path = output_path
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sample_count = 0
        self._error = ""

    @property
    def health(self) -> bool:
        with self._lock:
            return self._sample_count > 0 and not self._error

    @property
    def sample_count(self) -> int:
        with self._lock:
            return self._sample_count

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    def _mark_failed(self) -> None:
        with self._lock:
            self._error = GPU_MONITOR_REQUIRED_ERROR

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(self.HEADER)
        if shutil.which("nvidia-smi") is None:
            self._mark_failed()
            return
        if not self.capture_once():
            return
        self._thread = threading.Thread(target=self._run, name="nvidia-smi-validation", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.interval_seconds * 2))
            if self._thread.is_alive():
                self._mark_failed()

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval_seconds):
                if not self.capture_once():
                    return
        except Exception:
            self._mark_failed()

    def capture_once(self) -> bool:
        command = [
            "nvidia-smi",
            "--query-gpu=timestamp,index,uuid,memory.total,memory.free,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
        except (OSError, subprocess.SubprocessError):
            self._mark_failed()
            return False
        captured_at = datetime.now(timezone.utc).isoformat()
        rows = []
        for line in completed.stdout.splitlines():
            fields = [field.strip() for field in next(csv.reader([line]))]
            if len(fields) != 7 or not fields[0] or not fields[2]:
                continue
            try:
                int(fields[1])
                for value in fields[3:]:
                    float(value)
            except ValueError:
                continue
            rows.append([captured_at, *fields])
        if rows:
            with self.output_path.open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(rows)
            with self._lock:
                self._sample_count += len(rows)
            return True
        self._mark_failed()
        return False


class CUDAValidationRunner:
    def __init__(
        self,
        *,
        mode: str,
        services_path: Path,
        fixture_path: Path,
        output_dir: Path,
        topology_path: Path | None = None,
        node: str | None = None,
        client_factory: Callable[[TTSServiceEndpoint], TTSServiceClient] = build_service_client,
        status_probe: Callable[[TTSServiceEndpoint], dict[str, Any]] | None = None,
        transcriber: Transcriber | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        monitor_factory: Callable[[Path], NvidiaSmiMonitor] = NvidiaSmiMonitor,
        expected_commit: str | None = None,
        require_baseline: bool = False,
        diagnostic: bool = False,
        distributed_preflight_path: Path | None = None,
        distributed_orchestration_token: str | None = None,
        orchestration_preflight_path: Path | None = None,
        orchestration_token: str | None = None,
        controller_identity_provider: Callable[[], str] | None = None,
    ) -> None:
        if mode not in VALIDATION_MODES:
            raise ValueError(f"mode must be one of: {', '.join(VALIDATION_MODES)}")
        self.mode = mode
        self.services_path = services_path
        self.fixture_path = fixture_path
        self.output_dir = output_dir
        self.topology_path = topology_path
        self.node = node
        self.client_factory = client_factory
        self.status_probe = status_probe or _http_status_probe
        self.transcriber = transcriber
        self.clock = clock
        self.sleeper = sleeper
        self.monitor_factory = monitor_factory
        self.expected_commit = expected_commit or os.environ.get("TTS_MORE_EXPECTED_APP_COMMIT") or None
        self.require_baseline = require_baseline and mode != "single-clean"
        self.diagnostic = diagnostic
        self.orchestration_preflight_path = (
            orchestration_preflight_path or distributed_preflight_path
        )
        self.orchestration_token = (
            orchestration_token
            or distributed_orchestration_token
            or os.environ.get("TTS_MORE_ORCHESTRATION_TOKEN")
            or os.environ.get("TTS_MORE_DISTRIBUTED_ORCHESTRATION_TOKEN")
            or None
        )
        self.distributed_preflight_path = self.orchestration_preflight_path
        self.distributed_orchestration_token = self.orchestration_token
        self.controller_identity_provider = controller_identity_provider or (
            lambda: os.environ.get("TTS_MORE_CONTROLLER_IDENTITY", "")
        )
        self.distributed_orchestration_verified = False
        self.orchestration_verified = False
        self._evidence_hash_key: bytes | None = None
        self._lan_topology: LanTopology | None = None
        self._lan_policy: LanPolicy | None = None

    def _new_report(
        self, *, stage: str, fixture_sha256: str | None = None
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": "cuda-e2e-validation",
            "stage": stage,
            "mode": self.mode,
            "topology": str(self.topology_path) if self.topology_path else None,
            "node": self.node,
            "distributed_orchestration_verified": (
                self.distributed_orchestration_verified if self.mode == "distributed" else None
            ),
            "distributed_preflight": (
                str(self.distributed_preflight_path)
                if self.mode == "distributed" and self.distributed_preflight_path
                else None
            ),
            "orchestration_verified": (
                self.orchestration_verified if self.mode in STRICT_LAN_MODES else None
            ),
            "orchestration_workers": (
                [] if self.mode in STRICT_LAN_MODES else None
            ),
            "orchestration_preflight": (
                str(self.orchestration_preflight_path)
                if self.mode in STRICT_LAN_MODES and self.orchestration_preflight_path
                else None
            ),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "fixture_sha256": fixture_sha256,
            "passed": False,
            "core_started": False,
            "certifiable": False,
            "certification_status": "blocked",
            "preflight": [],
            "services": [],
            "cases": [],
            "cer": {"required": False, "items": [], "aggregate_cer": None, "passed": True},
            "asr_batch": {"required": False, "passed": True, "error": ""},
            "performance": {"passed": False, "checks": {}, "metrics": {}},
            "gpu_monitor": {"healthy": False, "sample_count": 0, "error": ""},
        }

    def run_input_preflight(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        fixture, fixture_sha256, failures = _load_fixture_snapshot(
            self.fixture_path,
            mode=self.mode,
            require_baseline=self.require_baseline,
        )
        return self._write_input_preflight(fixture, fixture_sha256, failures)

    def _write_input_preflight(
        self,
        fixture: ValidationFixture | None,
        fixture_sha256: str | None,
        failures: list[str],
    ) -> dict[str, Any]:
        report = self._new_report(
            stage="input-preflight", fixture_sha256=fixture_sha256
        )
        report["preflight"] = [
            {"passed": False, "message": message} for message in failures
        ]
        report["passed"] = not failures
        report["blocker_count"] = len(failures)
        report["next_action"] = _preflight_next_action(failures)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report_files(
            self.output_dir,
            report,
            fixture,
            {},
            reset_nvidia=True,
            hash_key=self._evidence_hash_key,
        )
        return report

    def write_blocker_report(
        self,
        *,
        stage: str,
        message: str,
        preserve_existing: bool = False,
    ) -> dict[str, Any]:
        if preserve_existing:
            if stage not in POST_CORE_STAGES:
                raise ValueError("preserve-existing is only allowed for post-core stages")
            summary_path = self.output_dir / "summary.json"
            try:
                report = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"post-core blocker requires an existing core summary: {exc}") from exc
            required_core_fields = ("services", "cases", "cer", "performance")
            if (
                not isinstance(report, dict)
                or report.get("stage") != "cuda-validation"
                or report.get("passed") is not True
                or any(field not in report for field in required_core_fields)
            ):
                raise ValueError("post-core blocker requires a completed passing core summary")
            pipeline_failures = list(report.get("pipeline_failures") or [])
            pipeline_failures.append(
                {"stage": stage, "message": message, "passed": False}
            )
            report["stage"] = stage
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            report["passed"] = False
            report["certifiable"] = False
            report["certification_status"] = "core_failed"
            report.setdefault("fixture_sha256", None)
            report["pipeline_failures"] = pipeline_failures
            report["post_core"] = {"passed": False, "failed_stage": stage}
            report["blocker_count"] = len(pipeline_failures)
            report["next_action"] = f"修复 {stage} 流水线阶段后重新运行完整 CUDA 验证。"
            fixture, current_fixture_sha256, _failures = _load_fixture_snapshot(
                self.fixture_path,
                mode=self.mode,
                require_baseline=self.require_baseline,
            )
            if current_fixture_sha256 != report.get("fixture_sha256"):
                fixture = None
            _write_report_files(
                self.output_dir,
                report,
                fixture,
                {},
                preserve_worker_references=True,
                hash_key=self._evidence_hash_key,
            )
            return report
        fixture, fixture_sha256, _failures = _load_fixture_snapshot(
            self.fixture_path,
            mode=self.mode,
            require_baseline=self.require_baseline,
        )
        report = self._new_report(stage=stage, fixture_sha256=fixture_sha256)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["passed"] = False
        report["certifiable"] = False
        report["certification_status"] = "blocked"
        report["preflight"] = [
            {"passed": False, "message": f"{stage} failed: {message}"}
        ]
        report["blocker_count"] = 1
        report["next_action"] = f"修复 {stage} 阶段错误后重新运行完整 CUDA 验证。"
        _write_report_files(
            self.output_dir,
            report,
            fixture,
            {},
            reset_nvidia=True,
            hash_key=self._evidence_hash_key,
        )
        return report

    def run(self) -> dict[str, Any]:
        fixture, fixture_sha256, input_failures = _load_fixture_snapshot(
            self.fixture_path,
            mode=self.mode,
            require_baseline=self.require_baseline,
        )
        input_report = self._write_input_preflight(
            fixture, fixture_sha256, input_failures
        )
        if not input_report["passed"]:
            return input_report
        assert fixture is not None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "wav").mkdir(parents=True, exist_ok=True)
        report = self._new_report(
            stage="cuda-validation", fixture_sha256=fixture_sha256
        )
        endpoints: dict[str, TTSServiceEndpoint] = {}
        monitor: NvidiaSmiMonitor | None = None
        try:
            try:
                monitor = self.monitor_factory(self.output_dir / "nvidia-smi.csv")
                monitor.start()
            except Exception:
                report["preflight"].append(
                    {"passed": False, "message": GPU_MONITOR_REQUIRED_ERROR}
                )
                return self._finish(report, fixture, endpoints)
            self._record_gpu_monitor(report, monitor)
            if not report["gpu_monitor"]["healthy"]:
                report["preflight"].append(
                    {"passed": False, "message": GPU_MONITOR_REQUIRED_ERROR}
                )
                return self._finish(report, fixture, endpoints)
            fixture, endpoints = self._preflight(report, fixture)
            if report["preflight"]:
                return self._finish(report, fixture, endpoints)
            report["core_started"] = True
            clients, ready_services = self._check_service_contracts(report, fixture, endpoints)
            perf_source: dict[str, Any] = {
                "oom": False,
                "minimum_free_mib": None,
                "maximum_reserved_mib": None,
                "maximum_allocated_mib": None,
                "baseline_memory_mib": None,
                "unload_memory_mib": None,
                "unload_recovery_seconds": None,
                "cold_load_seconds": None,
                "short_synthesis_seconds": [],
                "warm_synthesis_seconds": [],
                "warm_p95_seconds": None,
            }
            cer_items: list[tuple[str, str, str]] = []
            asr_queue: list[tuple[dict[str, Any], Path, str, str]] = []
            for case in validation_cases(fixture):
                client = clients.get(case.service_id)
                endpoint = endpoints[case.service_id]
                if client is None or case.service_id not in ready_services:
                    report["cases"].append(
                        {"name": case.name, "service_id": case.service_id, "passed": False, "errors": ["service contract preflight failed"]}
                    )
                    continue
                case_report = self._run_case(case, endpoint, client, perf_source)
                report["cases"].append(case_report)
                if case_report.get("audio", {}).get("passed"):
                    asr_queue.append((case_report, self.output_dir / case_report["output_path"], case.text, case.language))
            if self.mode in LOCAL_SINGLE_MODES and fixture.service_ids.gpt_sovits in ready_services:
                base_case = validation_cases(fixture)[0]
                artifact_case = ValidationCase(
                    name=f"{base_case.name}-artifact",
                    service_id=base_case.service_id,
                    profile=base_case.profile,
                    text=base_case.text,
                    language=base_case.language,
                    parameters=base_case.parameters,
                )
                base_endpoint = endpoints[artifact_case.service_id]
                artifact_endpoint = base_endpoint.model_copy(
                    update={"default_params": {**base_endpoint.default_params, "delivery": "artifact"}}
                )
                artifact_client = self.client_factory(artifact_endpoint)
                artifact_report = self._run_case(
                    artifact_case, artifact_endpoint, artifact_client, perf_source
                )
                report["cases"].append(artifact_report)
                if artifact_report.get("audio", {}).get("passed"):
                    asr_queue.append(
                        (
                            artifact_report,
                            self.output_dir / artifact_report["output_path"],
                            artifact_case.text,
                            artifact_case.language,
                        )
                    )
            release_failures = [
                case_report
                for case_report in report["cases"]
                if case_report.get("tts_attempted")
                and not (
                    case_report.get("unload_confirmed")
                    and case_report.get("memory_recovered")
                )
            ]
            if fixture.asr.required:
                report["asr_batch"]["required"] = True
                if release_failures:
                    report["asr_batch"].update(
                        {"passed": False, "error": ASR_BATCH_RELEASE_ERROR}
                    )
                    report["preflight"].append(
                        {"passed": False, "message": ASR_BATCH_RELEASE_ERROR}
                    )
                    for case_report in release_failures:
                        if ASR_BATCH_RELEASE_ERROR not in case_report["errors"]:
                            case_report["errors"].append(ASR_BATCH_RELEASE_ERROR)
                        case_report["passed"] = False
                else:
                    for case_report, wav_path, reference, language in asr_queue:
                        try:
                            assert self.transcriber is not None
                            hypothesis = self.transcriber(wav_path, language)
                            cer = character_error_rate(reference, hypothesis)
                            case_report["asr"] = {
                                "reference": reference,
                                "hypothesis": hypothesis,
                                "cer": cer,
                                "passed": cer <= MAX_ITEM_CER,
                            }
                            cer_items.append((case_report["name"], reference, hypothesis))
                            if cer > MAX_ITEM_CER:
                                case_report["errors"].append(
                                    f"CER {cer:.4f} exceeds {MAX_ITEM_CER:.2f}"
                                )
                        except Exception as exc:
                            case_report["errors"].append(
                                f"ASR failed: {type(exc).__name__}: {exc}"
                            )
                        case_report["passed"] = not case_report["errors"]
                report["cer"] = evaluate_cer(cer_items)
                if len(cer_items) != len(report["cases"]):
                    report["cer"]["passed"] = False
                    report["cer"]["missing_items"] = len(report["cases"]) - len(cer_items)
            warm_synthesis_times = list(perf_source["warm_synthesis_seconds"])
            perf_source["warm_p95_seconds"] = (
                _p95(warm_synthesis_times) if warm_synthesis_times else None
            )
            baseline = fixture.performance_baseline.model_dump(exclude_none=True) if fixture.performance_baseline else None
            report["performance"] = evaluate_performance(perf_source, baseline=baseline)
            return self._finish(report, fixture, endpoints)
        except Exception as exc:
            report["preflight"].append({"passed": False, "message": f"validator error: {type(exc).__name__}: {exc}"})
            return self._finish(report, fixture, endpoints)
        finally:
            close = getattr(self.transcriber, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    report["preflight"].append(
                        {"passed": False, "message": f"ASR cleanup failed: {type(exc).__name__}: {exc}"}
                    )
            if monitor is not None:
                monitor_stop_failed = False
                try:
                    monitor.stop()
                except Exception:
                    monitor_stop_failed = True
                self._record_gpu_monitor(report, monitor)
                if monitor_stop_failed:
                    report["gpu_monitor"].update(
                        {"healthy": False, "error": GPU_MONITOR_REQUIRED_ERROR}
                    )
                if not report["gpu_monitor"]["healthy"] and not any(
                    item.get("message") == GPU_MONITOR_REQUIRED_ERROR
                    for item in report["preflight"]
                ):
                    report["preflight"].append(
                        {"passed": False, "message": GPU_MONITOR_REQUIRED_ERROR}
                    )
            self._finish(report, fixture, endpoints)

    @staticmethod
    def _record_gpu_monitor(report: dict[str, Any], monitor: Any) -> None:
        sample_count = int(getattr(monitor, "sample_count", 0) or 0)
        healthy = bool(getattr(monitor, "health", False)) and sample_count > 0
        report["gpu_monitor"] = {
            "healthy": healthy,
            "sample_count": sample_count,
            "error": str(getattr(monitor, "error", "") or ""),
        }

    def _preflight(
        self,
        report: dict[str, Any],
        fixture: ValidationFixture | None = None,
    ) -> tuple[ValidationFixture | None, dict[str, TTSServiceEndpoint]]:
        if fixture is None:
            try:
                fixture = load_fixture(self.fixture_path)
            except Exception as exc:
                report["preflight"].append(
                    {"passed": False, "message": f"fixture validation failed: {exc}"}
                )
                return None, {}
        if self.mode == "distributed":
            orchestration_error = self._verify_distributed_orchestration()
            if orchestration_error:
                report["preflight"].append({"passed": False, "message": orchestration_error})
                return fixture, {}
            self.distributed_orchestration_verified = True
            report["distributed_orchestration_verified"] = True
        elif self.mode in STRICT_LAN_MODES:
            orchestration_error = self._verify_orchestration()
            if orchestration_error:
                report["preflight"].append(
                    {"passed": False, "message": orchestration_error}
                )
                return fixture, {}
            self.orchestration_verified = True
            report["orchestration_verified"] = True
            assert self._lan_policy is not None
            report["orchestration_workers"] = sorted(self._lan_policy.workers)
            assert self.orchestration_token is not None
            self._evidence_hash_key = hashlib.sha256(
                self.orchestration_token.encode("utf-8")
            ).digest()
        if not self.services_path.is_file():
            report["preflight"].append({"passed": False, "message": f"services file not found: {self.services_path}"})
            return fixture, {}
        try:
            registry = ServiceRegistry.load(self.services_path)
            endpoint_by_id = {endpoint.service_id: endpoint for endpoint in registry.services}
        except Exception as exc:
            report["preflight"].append({"passed": False, "message": f"services validation failed: {exc}"})
            return fixture, {}
        required_ids = set(FORMAL_SERVICE_IDS.values())
        missing_services = sorted(required_ids - set(endpoint_by_id))
        if missing_services:
            report["preflight"].append(
                {"passed": False, "message": f"services file is missing formal service IDs: {', '.join(missing_services)}"}
            )
        selected = {service_id: endpoint_by_id[service_id] for service_id in required_ids if service_id in endpoint_by_id}
        for endpoint in selected.values():
            if endpoint.api_contract != "tts-more-v1":
                report["preflight"].append(
                    {"passed": False, "message": f"{endpoint.service_id} must use tts-more-v1 for CUDA validation"}
                )
            if self.mode in EXTERNAL_LAN_MODES and (
                endpoint.mode != "external" or endpoint.network_scope != "lan" or endpoint.managed
            ):
                report["preflight"].append(
                    {"passed": False, "message": f"{endpoint.service_id} is not an unmanaged external LAN worker"}
                )
            if self.mode in STRICT_LAN_MODES:
                assert self._lan_topology is not None and self._lan_policy is not None
                owner = self._lan_policy.service_owners[endpoint.service_id]
                owner_node = self._lan_topology.nodes[owner]
                endpoint_host = urlsplit(endpoint.base_url).hostname or ""
                if (
                    _canonical_host(endpoint_host) != _canonical_host(owner_node.host)
                    or endpoint.resource_group != owner_node.resource_group
                ):
                    report["preflight"].append(
                        {
                            "passed": False,
                            "message": f"{endpoint.service_id} does not match its topology owner",
                        }
                    )
            if "artifact-transfer" not in {
                item.replace("_", "-").casefold() for item in endpoint.capabilities
            }:
                report["preflight"].append(
                    {"passed": False, "message": f"{endpoint.service_id} lacks artifact-transfer capability"}
                )
        if self.transcriber is None:
            self.transcriber, asr_error = create_transcriber(
                required=fixture.asr.required,
                model_name=fixture.asr.model,
                language=fixture.asr.language,
            )
            if asr_error:
                report["preflight"].append({"passed": False, "message": asr_error})
        return fixture, selected

    def _verify_distributed_orchestration(self) -> str:
        if self.distributed_preflight_path is None or not self.distributed_preflight_path.is_file():
            return "distributed validation requires the PowerShell orchestration preflight"
        if self.topology_path is None or not self.topology_path.is_file():
            return "distributed validation requires an existing topology file"
        if not self.distributed_orchestration_token:
            return "distributed validation requires the current PowerShell orchestration token"
        if not self.expected_commit:
            return "distributed validation requires the controller commit identity"
        try:
            payload = DistributedOrchestrationPreflight.model_validate_json(
                self.distributed_preflight_path.read_text(encoding="utf-8-sig")
            )
        except Exception as exc:
            return f"distributed orchestration preflight is invalid: {exc}"
        actual_topology_hash = hashlib.sha256(self.topology_path.read_bytes()).hexdigest()
        if not hmac.compare_digest(payload.topology_sha256, actual_topology_hash):
            return "distributed orchestration topology hash does not match"
        if not hmac.compare_digest(payload.controller_commit, self.expected_commit):
            return "distributed orchestration controller commit does not match"
        actual_token_hash = hashlib.sha256(self.distributed_orchestration_token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(payload.token_sha256, actual_token_hash):
            return "distributed orchestration token does not match the current run"
        if payload.created_at.tzinfo is None:
            return "distributed orchestration preflight timestamp must include a timezone"
        age_seconds = (datetime.now(timezone.utc) - payload.created_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds < -300 or age_seconds > 12 * 60 * 60:
            return "distributed orchestration preflight is outside the allowed execution window"
        return ""

    def _verify_orchestration(self) -> str:
        if (
            self.orchestration_preflight_path is None
            or not self.orchestration_preflight_path.is_file()
        ):
            return "LAN validation requires the orchestration preflight"
        if self.topology_path is None or not self.topology_path.is_file():
            return "LAN validation requires an existing topology file"
        if not self.orchestration_token:
            return "LAN validation requires the current orchestration token"
        if not self.expected_commit:
            return "LAN validation requires the controller commit identity"
        try:
            controller_identity = self.controller_identity_provider().strip()
        except Exception:
            return "LAN controller identity is unavailable"
        if not controller_identity:
            return "LAN validation requires the controller identity"
        try:
            topology, policy = load_lan_policy(self.topology_path, self.mode)
        except Exception:
            return "LAN topology policy is invalid"
        try:
            payload = LanOrchestrationPreflight.model_validate_json(
                self.orchestration_preflight_path.read_text(encoding="utf-8-sig")
            )
        except Exception:
            return "LAN orchestration preflight schema-v2 is invalid"
        if payload.mode != self.mode:
            return "LAN orchestration mode does not match"
        bindings = (
            (payload.topology_sha256, hashlib.sha256(self.topology_path.read_bytes()).hexdigest(), "topology"),
            (payload.fixture_sha256, hashlib.sha256(self.fixture_path.read_bytes()).hexdigest(), "fixture"),
            (payload.controller_commit, self.expected_commit, "controller commit"),
            (
                payload.controller_id_sha256,
                hashlib.sha256(controller_identity.encode("utf-8")).hexdigest(),
                "controller identity",
            ),
            (
                payload.token_sha256,
                hashlib.sha256(self.orchestration_token.encode("utf-8")).hexdigest(),
                "token",
            ),
        )
        for supplied, actual, label in bindings:
            if not hmac.compare_digest(supplied, actual):
                return f"LAN orchestration {label} hash does not match"
        if set(payload.nodes) != set(policy.workers):
            return "LAN orchestration node set does not match topology policy"
        if any(
            not hmac.compare_digest(node.commit, self.expected_commit)
            for node in payload.nodes.values()
        ):
            return "LAN orchestration worker commit does not match the controller"
        if payload.created_at.tzinfo is None:
            return "LAN orchestration preflight timestamp must include a timezone"
        age_seconds = (
            datetime.now(timezone.utc) - payload.created_at.astimezone(timezone.utc)
        ).total_seconds()
        if age_seconds < -300 or age_seconds > 12 * 60 * 60:
            return "LAN orchestration preflight is outside the allowed execution window"
        self._lan_topology = topology
        self._lan_policy = policy
        return ""

    def _check_service_contracts(
        self,
        report: dict[str, Any],
        fixture: ValidationFixture,
        endpoints: dict[str, TTSServiceEndpoint],
    ) -> tuple[dict[str, TTSServiceClient], set[str]]:
        clients: dict[str, TTSServiceClient] = {}
        ready: set[str] = set()
        for service_id in FORMAL_SERVICE_IDS.values():
            endpoint = endpoints[service_id]
            service_report: dict[str, Any] = {"service_id": service_id, "passed": False, "errors": []}
            try:
                client = self.client_factory(endpoint)
                clients[service_id] = client
                health = client.health()
                capabilities = (
                    _http_capabilities_probe(endpoint)
                    if isinstance(client, HttpTTSServiceClient)
                    else client.capabilities()
                )
                status = self.status_probe(endpoint)
                service_report.update({"health": health, "capabilities": capabilities, "status": status})
                if not health.get("ready"):
                    service_report["errors"].append("health did not report ready=true")
                if self.expected_commit and health.get("tts_more_commit") != self.expected_commit:
                    service_report["errors"].append("worker TTS More commit does not match the controller")
                live_caps = {str(item).replace("_", "-").casefold() for item in capabilities.get("capabilities", [])}
                if "tts" not in live_caps:
                    service_report["errors"].append("worker does not advertise tts capability")
                if "artifact-transfer" not in live_caps:
                    service_report["errors"].append("worker does not advertise artifact-transfer capability")
                service_report["errors"].extend(_status_errors(status, expected_loaded=False))
            except Exception as exc:
                service_report["errors"].append(f"contract request failed: {type(exc).__name__}: {exc}")
            service_report["passed"] = not service_report["errors"]
            if service_report["passed"]:
                ready.add(service_id)
            report["services"].append(service_report)
        if self.mode in DISTINCT_GPU_MODES:
            uuid_owners: dict[str, list[dict[str, Any]]] = {}
            for service_report in report["services"]:
                device_uuid = str((service_report.get("status") or {}).get("device_uuid") or "").strip()
                if not device_uuid:
                    service_report["errors"].append("status is missing CUDA device UUID")
                    continue
                uuid_owners.setdefault(device_uuid, []).append(service_report)
            for owners in uuid_owners.values():
                if len(owners) < 2:
                    continue
                for service_report in owners:
                    service_report["errors"].append(
                        "distributed workers must report a distinct CUDA device UUID"
                        if self.mode == "distributed"
                        else "lan-distributed workers must report a distinct CUDA device UUID"
                    )
            for service_report in report["services"]:
                service_report["passed"] = not service_report["errors"]
                if not service_report["passed"]:
                    ready.discard(str(service_report["service_id"]))
        elif self.mode == "lan-shared":
            uuids: set[str] = set()
            for service_report in report["services"]:
                device_uuid = str(
                    (service_report.get("status") or {}).get("device_uuid") or ""
                ).strip()
                if not device_uuid:
                    service_report["errors"].append("status is missing CUDA device UUID")
                else:
                    uuids.add(device_uuid)
            if len(uuids) != 1:
                for service_report in report["services"]:
                    service_report["errors"].append(
                        "lan-shared workers must report one shared CUDA device UUID"
                    )
            for service_report in report["services"]:
                service_report["passed"] = not service_report["errors"]
                if not service_report["passed"]:
                    ready.discard(str(service_report["service_id"]))
        return clients, ready

    def _run_case(
        self,
        case: ValidationCase,
        endpoint: TTSServiceEndpoint,
        client: TTSServiceClient,
        perf_source: dict[str, Any],
    ) -> dict[str, Any]:
        output_path = self.output_dir / "wav" / f"{case.name}.wav"
        case_report: dict[str, Any] = {
            "name": case.name,
            "service_id": case.service_id,
            "profile": case.profile,
            "output_path": f"wav/{output_path.name}",
            "tts_attempted": True,
            "unload_confirmed": False,
            "memory_recovered": False,
            "warm_synthesis_seconds": [],
            "warm_repeats": [],
            "passed": False,
            "errors": [],
        }
        baseline_status: dict[str, Any] | None = None
        load_started = self.clock()
        try:
            baseline_status = self.status_probe(endpoint)
            _record_memory(perf_source, baseline_status, baseline=True)
            client.load(case.profile, case.parameters)
            load_seconds = max(0.0, self.clock() - load_started)
            case_report["load_seconds"] = load_seconds
            current_cold = perf_source.get("cold_load_seconds")
            perf_source["cold_load_seconds"] = max(current_cold or 0.0, load_seconds)
            loaded_status = self.status_probe(endpoint)
            case_report["loaded_status"] = loaded_status
            case_report["errors"].extend(_status_errors(loaded_status, expected_loaded=True))
            _record_memory(perf_source, loaded_status)
            loaded_model = loaded_status.get("model")

            synthesis_started = self.clock()
            line = ScriptLine(
                id=case.name,
                character_id="cuda-validation",
                text=case.text,
                language=case.language,
            )
            output_path.unlink(missing_ok=True)
            result = client.synthesize(
                SynthesisRequest(
                    line=line,
                    profile=case.profile,
                    output_path=output_path,
                    parameters=case.parameters,
                )
            )
            synthesis_seconds = max(0.0, self.clock() - synthesis_started)
            case_report["synthesis_seconds"] = synthesis_seconds
            perf_source["short_synthesis_seconds"].append(synthesis_seconds)
            if result.audio_path.resolve(strict=False) != output_path.resolve(strict=False):
                raise RuntimeError(f"worker returned unexpected output path: {result.audio_path}")
            try:
                audio_metrics = measure_wav(output_path)
            except (OSError, ValueError, wave.Error) as exc:
                raise RuntimeError(
                    "primary WAV evidence is missing or invalid"
                ) from exc
            case_report["audio"] = audio_metrics
            if not audio_metrics["passed"]:
                failed_checks = [name for name, passed in audio_metrics["checks"].items() if not passed]
                case_report["errors"].append(f"audio quality failed: {', '.join(failed_checks)}")
            after_synthesis = self.status_probe(endpoint)
            case_report["synthesis_status"] = after_synthesis
            _record_memory(perf_source, after_synthesis)
            primary_residency_errors = _residency_errors(
                after_synthesis, expected_model=loaded_model
            )
            if primary_residency_errors:
                raise RuntimeError(
                    "warm synthesis requires the original loaded model: "
                    + "; ".join(primary_residency_errors)
                )

            warm_times: list[float] = case_report["warm_synthesis_seconds"]
            warm_dir = self.output_dir / "wav" / "warm"
            warm_dir.mkdir(parents=True, exist_ok=True)
            for repeat_index in range(1, WARM_SYNTHESIS_REPEATS + 1):
                warm_output_path = warm_dir / f"{case.name}-repeat-{repeat_index}.wav"
                warm_output_path.unlink(missing_ok=True)
                warm_started = self.clock()
                warm_result = client.synthesize(
                    SynthesisRequest(
                        line=line,
                        profile=case.profile,
                        output_path=warm_output_path,
                        parameters=case.parameters,
                    )
                )
                warm_seconds = max(0.0, self.clock() - warm_started)
                if warm_result.audio_path.resolve(strict=False) != warm_output_path.resolve(
                    strict=False
                ):
                    raise RuntimeError(
                        f"worker returned unexpected warm output path: {warm_result.audio_path}"
                    )
                warm_status = self.status_probe(endpoint)
                _record_memory(perf_source, warm_status)
                warm_residency_errors = _residency_errors(
                    warm_status, expected_model=loaded_model
                )
                if warm_residency_errors:
                    raise RuntimeError(
                        "warm synthesis requires the original loaded model: "
                        + "; ".join(warm_residency_errors)
                    )
                try:
                    warm_audio = measure_wav(warm_output_path)
                except (OSError, ValueError, wave.Error) as exc:
                    raise RuntimeError(
                        "warm WAV evidence is missing or invalid"
                    ) from exc
                if not warm_audio["passed"]:
                    failed_checks = [
                        name
                        for name, passed in warm_audio["checks"].items()
                        if not passed
                    ]
                    raise RuntimeError(
                        "warm WAV evidence failed audio quality: "
                        + ", ".join(failed_checks)
                    )
                warm_times.append(warm_seconds)
                perf_source["warm_synthesis_seconds"].append(warm_seconds)
                case_report["warm_repeats"].append(
                    {
                        "repeat": repeat_index,
                        "output_path": f"wav/warm/{warm_output_path.name}",
                        "synthesis_seconds": warm_seconds,
                        "audio": warm_audio,
                        "status": warm_status,
                    }
                )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            case_report["errors"].append(message)
            if re.search(r"(?:out of memory|\boom\b)", message, flags=re.IGNORECASE):
                perf_source["oom"] = True
        finally:
            unload_started = self.clock()
            try:
                client.unload()
                baseline_reserved = _memory_mib(baseline_status or {}, "reserved_bytes") or 0.0
                unload_status, recovery_seconds = self._wait_for_unload(endpoint, baseline_reserved, unload_started)
                case_report["unload_status"] = unload_status
                case_report["unload_recovery_seconds"] = recovery_seconds
                case_report["unload_confirmed"] = unload_status.get("loaded") is False
                unload_reserved = _memory_mib(unload_status, "reserved_bytes")
                case_report["memory_recovered"] = (
                    unload_reserved is not None
                    and unload_reserved <= baseline_reserved + MAX_UNLOAD_MEMORY_DELTA_MIB
                )
                case_report["errors"].extend(_status_errors(unload_status, expected_loaded=False))
                perf_source["unload_recovery_seconds"] = max(
                    perf_source.get("unload_recovery_seconds") or 0.0, recovery_seconds
                )
                if unload_reserved is not None:
                    perf_source["unload_memory_mib"] = max(perf_source.get("unload_memory_mib") or 0.0, unload_reserved)
            except Exception as exc:
                case_report["errors"].append(f"unload failed: {type(exc).__name__}: {exc}")
        case_report["passed"] = not case_report["errors"]
        return case_report

    def _wait_for_unload(
        self, endpoint: TTSServiceEndpoint, baseline_reserved_mib: float, started: float
    ) -> tuple[dict[str, Any], float]:
        last_status: dict[str, Any] = {}
        for attempt in range(31):
            last_status = self.status_probe(endpoint)
            reserved = _memory_mib(last_status, "reserved_bytes")
            recovered = reserved is not None and reserved <= baseline_reserved_mib + MAX_UNLOAD_MEMORY_DELTA_MIB
            if last_status.get("loaded") is False and recovered:
                return last_status, min(MAX_UNLOAD_SECONDS, max(0.0, self.clock() - started))
            if attempt < 30:
                self.sleeper(1.0)
        return last_status, max(MAX_UNLOAD_SECONDS, self.clock() - started)

    def _finish(
        self,
        report: dict[str, Any],
        fixture: ValidationFixture | None,
        endpoints: dict[str, TTSServiceEndpoint],
    ) -> dict[str, Any]:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["passed"] = (
            not report["preflight"]
            and bool(report["services"])
            and all(item.get("passed") for item in report["services"])
            and bool(report["cases"])
            and all(item.get("passed") for item in report["cases"])
            and bool(report["cer"].get("passed"))
            and bool(report["performance"].get("passed"))
        )
        if self.diagnostic:
            report["certifiable"] = False
            if report["passed"]:
                report["certification_status"] = "diagnostic_core_passed"
            else:
                report["certification_status"] = (
                    "core_failed"
                    if report.get("core_started")
                    or report["services"]
                    or report["cases"]
                    else "blocked"
                )
        else:
            report["certifiable"] = False
            if report["passed"]:
                report["certification_status"] = "core_passed_ui_pending"
            else:
                report["certification_status"] = (
                    "core_failed"
                    if report.get("core_started")
                    or report["services"]
                    or report["cases"]
                    else "blocked"
                )
        _write_report_files(
            self.output_dir,
            report,
            fixture,
            endpoints,
            hash_key=self._evidence_hash_key,
        )
        return report


def _http_status_probe(endpoint: TTSServiceEndpoint) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        response = client.get(
            endpoint.base_url.rstrip("/") + "/status", headers=_endpoint_headers(endpoint)
        )
        response.raise_for_status()
        return response.json()


def _http_capabilities_probe(endpoint: TTSServiceEndpoint) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        response = client.get(
            endpoint.base_url.rstrip("/") + "/capabilities", headers=_endpoint_headers(endpoint)
        )
        response.raise_for_status()
        return response.json()


def _endpoint_headers(endpoint: TTSServiceEndpoint) -> dict[str, str]:
    if endpoint.auth_header_env and os.environ.get(endpoint.auth_header_env):
        return {"Authorization": os.environ[endpoint.auth_header_env]}
    api_key_env = endpoint.auth_profile.get("api_key_env")
    if api_key_env and os.environ.get(api_key_env):
        return {"Authorization": f"Bearer {os.environ[api_key_env]}"}
    return {}


def _write_report_files(
    output_dir: Path,
    report: dict[str, Any],
    fixture: ValidationFixture | None,
    endpoints: dict[str, TTSServiceEndpoint],
    *,
    reset_nvidia: bool = False,
    preserve_worker_references: bool = False,
    hash_key: bytes | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_report = _sanitize_evidence(report, hash_key=hash_key)
    _atomic_write_report_file(
        output_dir / "summary.json",
        lambda path: path.write_text(
            json.dumps(evidence_report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        ),
    )
    _atomic_write_report_file(
        output_dir / "junit.xml", lambda path: _write_junit(path, evidence_report)
    )
    _atomic_write_report_file(
        output_dir / "human-listening-review.md",
        lambda path: _write_listening_template(path, evidence_report, fixture),
    )
    log_references = []
    for service_id in FORMAL_SERVICE_IDS.values():
        endpoint = endpoints.get(service_id)
        log_references.append(
            {
                "service_id": service_id,
                "configured_log": _path_label(fixture.worker_logs.get(service_id)) if fixture else None,
                "status_path": "/status" if endpoint else None,
            }
        )
    worker_references_path = output_dir / "worker-log-references.json"
    if not preserve_worker_references or not worker_references_path.exists():
        _atomic_write_report_file(
            worker_references_path,
            lambda path: path.write_text(
                json.dumps(log_references, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            ),
        )
    nvidia_path = output_dir / "nvidia-smi.csv"
    if reset_nvidia or not nvidia_path.exists():
        _atomic_write_report_file(nvidia_path, _write_nvidia_header)


def _write_nvidia_header(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(NvidiaSmiMonitor.HEADER)


def _atomic_write_report_file(path: Path, writer: Callable[[Path], Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        writer(temporary_path)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _sanitize_evidence(
    value: Any, key: str = "", hash_key: bytes | None = None
) -> Any:
    if isinstance(value, dict):
        return {
            item_key: _sanitize_evidence(item, item_key, hash_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_evidence(item, key, hash_key) for item in value]
    if not isinstance(value, str):
        return value
    lowered = key.casefold()
    if lowered in {"device_uuid", "machine_id", "controller_id"}:
        if re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", value):
            return value
        key_bytes = hash_key or b"tts-more-local-evidence"
        digest = hmac.new(key_bytes, value.encode("utf-8"), hashlib.sha256).hexdigest()
        return "hmac-sha256:" + digest
    if "url" in lowered or lowered == "host":
        parsed = urlsplit(value)
        return parsed.path or "<redacted>"
    if lowered in {
        "topology",
        "fixture",
        "distributed_preflight",
        "orchestration_preflight",
    } or any(
        token in lowered for token in ("path", "root", "cli")
    ):
        return _path_label(value)
    return _sanitize_evidence_text(value)


def _sanitize_evidence_text(value: str) -> str:
    def redact_url(match: re.Match[str]) -> str:
        parsed = urlsplit(match.group(0).rstrip(".,;)"))
        return parsed.path or "<redacted-url>"

    sanitized = re.sub(r"https?://[^\s\"'<>]+", redact_url, value, flags=re.IGNORECASE)
    sanitized = re.sub(r"\\\\[^\\\s]+\\[^\\\s]+(?:\\[^\\\s]+)*", "<redacted-path>", sanitized)
    sanitized = re.sub(r"(?<!\w)[A-Za-z]:[\\/][^\s\"'<>|,;]+", "<redacted-path>", sanitized)
    sanitized = re.sub(
        r"(?<![\w:])/(?:Users|home|root|tmp|opt|var|workspace|workspaces|mnt|srv)/[^\s\"'<>|,;]+",
        "<redacted-path>",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<host>", sanitized)
    return re.sub(r"\b(?:[A-Za-z0-9-]+\.)+(?:lan|local)\b", "<host>", sanitized, flags=re.IGNORECASE)


def _path_label(value: str | None) -> str | None:
    if not value:
        return value
    normalized = str(value).replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] or "<redacted>"


def _canonical_host(value: str) -> str:
    normalized = value.strip().casefold().rstrip(".").strip("[]")
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        return normalized


def _write_junit(path: Path, report: dict[str, Any]) -> None:
    testcases: list[tuple[str, str, list[str]]] = []
    for index, item in enumerate(report.get("preflight") or []):
        testcases.append(("preflight", f"preflight-{index + 1}", [str(item.get("message") or "preflight failed")]))
    for service in report.get("services") or []:
        testcases.append(("service-contract", str(service["service_id"]), [str(item) for item in service.get("errors") or []]))
    for case in report.get("cases") or []:
        testcases.append(("cuda-synthesis", str(case["name"]), [str(item) for item in case.get("errors") or []]))
    for index, failure in enumerate(report.get("pipeline_failures") or []):
        stage = str(failure.get("stage") or "post-core")
        errors = [] if failure.get("passed") else [str(failure.get("message") or "pipeline failed")]
        testcases.append(("pipeline", f"{stage}-{index + 1}", errors))
    if report.get("performance", {}).get("checks"):
        errors = [name for name, passed in report["performance"]["checks"].items() if not passed]
        testcases.append(("quality-gates", "performance", errors))
    if report.get("cer", {}).get("required"):
        errors = [] if report["cer"].get("passed") else ["ASR CER thresholds failed"]
        testcases.append(("quality-gates", "asr-cer", errors))
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": "tts-more-cuda-validation",
            "tests": str(len(testcases)),
            "failures": str(sum(bool(errors) for _, _, errors in testcases)),
        },
    )
    for classname, name, errors in testcases:
        testcase = ElementTree.SubElement(suite, "testcase", {"classname": classname, "name": name})
        if errors:
            failure = ElementTree.SubElement(testcase, "failure", {"message": errors[0]})
            failure.text = "\n".join(errors)
    ElementTree.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _write_listening_template(
    path: Path, report: dict[str, Any], fixture: ValidationFixture | None
) -> None:
    reviewers = fixture.reviewers if fixture else []
    reviewer_lines = (
        "\n".join(
            f"- `{_markdown_reviewer(reviewer.id)}`: {_markdown_reviewer(reviewer.name)}"
            for reviewer in reviewers
        )
        or "- Not configured"
    )
    rows: list[str] = []
    row_reviewers: list[Reviewer | None] = list(reviewers) or [None]
    for reviewer in row_reviewers:
        for case in report.get("cases") or []:
            columns = [
                _markdown_inline(case["name"]),
                f"`{_markdown_inline(case.get('output_path', ''))}`",
                f"`{_markdown_reviewer(reviewer.id)}`" if reviewer else "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
            rows.append("| " + " | ".join(columns) + " |")
    if not rows:
        rows.append("| " + " | ".join(["No synthesis output", "", "", "", "", "", "", "", ""]) + " |")
    signature_blocks = "\n\n".join(
        f"""### Reviewer `{_markdown_reviewer(reviewer.id)}` — {_markdown_reviewer(reviewer.name)}

- Decision: PASS / FAIL
- Signature / timestamp:
- Release or certification reference:"""
        for reviewer in reviewers
    ) or "- Reviewers are not configured."
    content = f"""# CUDA Human Listening Review

Validation mode: `{report.get('mode')}`
Automated gate: `{'PASS' if report.get('passed') else 'FAIL'}`

## Reviewers

{reviewer_lines}

Score clarity, timbre similarity, emotion/prosody, and artifacts from 1 to 5. Each row must score at least 3 in every category and at least 3.5 overall.

| Case | WAV | Reviewer | Clarity | Timbre | Emotion / prosody | Artifact control | Overall | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---|
{chr(10).join(rows)}

## Independent decisions

{signature_blocks}
"""
    path.write_text(content, encoding="utf-8")


def _markdown_inline(value: Any) -> str:
    normalized = " ".join(str(value).splitlines())
    return (
        normalized.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("`", "&#96;")
        .replace("#", "&#35;")
    )


def _markdown_reviewer(value: Any) -> str:
    sanitized = _markdown_inline(value)
    for character in "\\![]()*_~:/@.":
        sanitized = sanitized.replace(character, f"&#{ord(character)};")
    return sanitized


def _status_errors(status: dict[str, Any], *, expected_loaded: bool) -> list[str]:
    errors = []
    if not str(status.get("device") or "").startswith("cuda"):
        errors.append("status device is not CUDA")
    if str(status.get("cuda_runtime") or "") != REQUIRED_CUDA_RUNTIME:
        errors.append(f"status CUDA runtime must be {REQUIRED_CUDA_RUNTIME}")
    if status.get("loaded") is not expected_loaded:
        errors.append(f"status loaded must be {str(expected_loaded).lower()}")
    if "model" not in status:
        errors.append("status is missing model")
    elif expected_loaded and not status.get("model"):
        errors.append("status model must identify the loaded profile")
    memory = status.get("memory")
    if not isinstance(memory, dict):
        errors.append("status is missing memory")
    else:
        for field in ("allocated_bytes", "reserved_bytes", "free_bytes", "total_bytes"):
            if field not in memory:
                errors.append(f"status memory is missing {field}")
        total_mib = _memory_mib(status, "total_bytes")
        if total_mib is None or total_mib < MIN_TOTAL_MEMORY_MIB:
            errors.append(f"CUDA device has less than {int(MIN_TOTAL_MEMORY_MIB)} MiB total memory")
    return errors


def _residency_errors(
    status: dict[str, Any], *, expected_model: Any
) -> list[str]:
    errors = _status_errors(status, expected_loaded=True)
    if status.get("model") != expected_model:
        errors.append("status model changed since the explicit load")
    return errors


def _record_memory(metrics: dict[str, Any], status: dict[str, Any], *, baseline: bool = False) -> None:
    free_mib = _memory_mib(status, "free_bytes")
    if free_mib is not None:
        current = metrics.get("minimum_free_mib")
        metrics["minimum_free_mib"] = free_mib if current is None else min(current, free_mib)
    for field, metric_name in (
        ("reserved_bytes", "maximum_reserved_mib"),
        ("allocated_bytes", "maximum_allocated_mib"),
    ):
        value_mib = _memory_mib(status, field)
        if value_mib is not None:
            metrics[metric_name] = max(metrics.get(metric_name) or 0.0, value_mib)
    if baseline:
        reserved_mib = _memory_mib(status, "reserved_bytes")
        if reserved_mib is not None:
            metrics["baseline_memory_mib"] = max(metrics.get("baseline_memory_mib") or 0.0, reserved_mib)


def _memory_mib(status: dict[str, Any], field: str) -> float | None:
    try:
        raw = status["memory"][field]
        return None if raw is None else float(raw) / (1024.0 * 1024.0)
    except (KeyError, TypeError, ValueError):
        return None


def _decode_pcm(frames: bytes, sample_width: int) -> list[int]:
    if sample_width == 1:
        return [value - 128 for value in frames]
    if sample_width == 2:
        usable = len(frames) - len(frames) % 2
        return [value[0] for value in struct.iter_unpack("<h", frames[:usable])]
    if sample_width == 3:
        return [int.from_bytes(frames[index : index + 3], "little", signed=True) for index in range(0, len(frames) - 2, 3)]
    if sample_width == 4:
        usable = len(frames) - len(frames) % 4
        return [value[0] for value in struct.iter_unpack("<i", frames[:usable])]
    raise ValueError(f"unsupported WAV sample width: {sample_width}")


def _edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_item in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_item in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_item != right_item),
                )
            )
        previous = current
    return previous[-1]


def _normalize_transcript(value: str) -> str:
    return "".join(value.split()).casefold()


def _expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, str):
        return os.path.expandvars(value)
    return value


def _contains_unresolved_environment(value: str) -> bool:
    return bool(re.search(r"\$\{[^}]+\}|%[^%]+%|\$[A-Za-z_][A-Za-z0-9_]*", value))


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[94]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Windows CUDA TTS More validation gate")
    parser.add_argument("--mode", required=True, choices=VALIDATION_MODES)
    parser.add_argument("--services", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--topology", type=Path)
    parser.add_argument("--node")
    parser.add_argument(
        "--orchestration-preflight",
        "--distributed-preflight",
        dest="orchestration_preflight",
        type=Path,
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--write-blocker-stage")
    parser.add_argument("--blocker-message")
    parser.add_argument("--preserve-existing", action="store_true")
    parser.add_argument(
        "--require-baseline",
        action="store_true",
        help="require an approved performance baseline (ignored for single-clean certification)",
    )
    args = parser.parse_args(argv)
    if bool(args.write_blocker_stage) != bool(args.blocker_message):
        parser.error("--write-blocker-stage and --blocker-message must be provided together")
    if args.preserve_existing and args.write_blocker_stage not in POST_CORE_STAGES:
        parser.error("--preserve-existing is only allowed for post-core blocker stages")
    runner = CUDAValidationRunner(
        mode=args.mode,
        services_path=args.services,
        fixture_path=args.fixture,
        output_dir=args.output,
        topology_path=args.topology,
        node=args.node,
        orchestration_preflight_path=args.orchestration_preflight,
        require_baseline=args.require_baseline,
        diagnostic=args.diagnostic,
    )
    if args.write_blocker_stage:
        report = runner.write_blocker_report(
            stage=args.write_blocker_stage,
            message=args.blocker_message,
            preserve_existing=args.preserve_existing,
        )
    else:
        report = runner.run_input_preflight() if args.preflight_only else runner.run()
    if report.get("stage") == "input-preflight" and not report["passed"]:
        print(
            f"阻塞：input-preflight 有 {report['blocker_count']} 个未解决项；证据：summary.json"
        )
    else:
        print(json.dumps({"passed": report["passed"], "summary": str(args.output / "summary.json")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
