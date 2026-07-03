from __future__ import annotations

import sys
from pathlib import Path

from app.resources import collect_voice_candidates
from app.role_library import candidate_to_character, scan_logs_index_candidates, scan_role_library_candidates


def test_collect_voice_candidates_scans_core_weights_reference_audio_and_index_model(tmp_path: Path) -> None:
    gpt_root = tmp_path / "models" / "GPT_weights_v2ProPlus"
    sovits_root = tmp_path / "models" / "SoVITS_weights_v2ProPlus"
    ref_root = tmp_path / "refs"
    index_model = tmp_path / "IndexTTS-2"
    gpt_root.mkdir(parents=True)
    sovits_root.mkdir(parents=True)
    (ref_root / "小美").mkdir(parents=True)
    index_model.mkdir()
    (gpt_root / "xiao.ckpt").write_bytes(b"gpt")
    (sovits_root / "xiao.pth").write_bytes(b"sovits")
    (ref_root / "小美" / "ref.wav").write_bytes(b"wav")
    for name in [
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
    ]:
        target = index_model / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"model")

    result = collect_voice_candidates(
        reference_audio_root=ref_root,
        gpt_weights_roots=[gpt_root],
        sovits_weights_roots=[sovits_root],
        indextts_model_dir=index_model,
    )

    assert result["reference_audio"]["exists"] is True
    assert result["reference_audio"]["groups"][0]["name"] == "小美"
    assert result["gpt_sovits"]["gpt_weights"][0]["path"].endswith("xiao.ckpt")
    assert result["gpt_sovits"]["sovits_weights"][0]["path"].endswith("xiao.pth")
    assert "vibevoice" not in result
    assert result["ready"] is True


def test_reference_audio_scan_keeps_nested_leaf_group_and_sidecar_text(tmp_path: Path) -> None:
    ref_root = tmp_path / "refs"
    leaf = ref_root / "00未检查音-不要删" / "3功夫毒角-李坤" / "音频"
    leaf.mkdir(parents=True)
    (leaf / "line.wav").write_bytes(b"wav")
    (leaf / "line.txt").write_text("别回头。", encoding="utf-8")

    result = collect_voice_candidates(
        reference_audio_root=ref_root,
        gpt_weights_roots=[],
        sovits_weights_roots=[],
        indextts_model_dir=tmp_path / "missing-index",
    )

    group = result["reference_audio"]["groups"][0]
    assert group["name"] == "00未检查音-不要删 / 3功夫毒角-李坤 / 音频"
    assert group["samples"][0].endswith("line.wav")
    assert group["sample_details"][0]["text"] == "别回头。"
    assert group["sample_details"][0]["text_source"] == "sidecar"


def test_collect_voice_candidates_reports_missing_roots_without_crashing(tmp_path: Path) -> None:
    result = collect_voice_candidates(
        reference_audio_root=tmp_path / "missing-refs",
        gpt_weights_roots=[tmp_path / "missing-gpt"],
        sovits_weights_roots=[tmp_path / "missing-sovits"],
        indextts_model_dir=tmp_path / "missing-index",
    )

    assert result["ready"] is False
    assert result["reference_audio"]["exists"] is False
    assert result["gpt_sovits"]["diagnostics"][0]["status"] == "missing"
    assert result["indextts"]["model"]["ready"] is False


def test_collect_voice_candidates_reports_python_runtime_modules(tmp_path: Path) -> None:
    ref_root = tmp_path / "refs"
    (ref_root / "role").mkdir(parents=True)
    (ref_root / "role" / "a.wav").write_bytes(b"wav")

    result = collect_voice_candidates(
        reference_audio_root=ref_root,
        gpt_weights_roots=[],
        sovits_weights_roots=[],
        indextts_model_dir=tmp_path / "missing-index",
        runtime_checks={
            "python-ok": {"python": sys.executable, "modules": ["sys"]},
            "python-missing": {"python": sys.executable, "modules": ["definitely_missing_tts_more_module"]},
        },
    )

    assert result["runtimes"]["python-ok"]["ready"] is True
    assert result["runtimes"]["python-missing"]["ready"] is False
    assert result["runtimes"]["python-missing"]["missing_modules"] == ["definitely_missing_tts_more_module"]


def test_scan_role_library_candidates_pairs_weights_and_reads_sidecar_text(tmp_path: Path) -> None:
    gpt_root = tmp_path / "GPT_weights_v2ProPlus"
    sovits_root = tmp_path / "SoVITS_weights_v2ProPlus"
    ref_root = tmp_path / "refs"
    gpt_root.mkdir()
    sovits_root.mkdir()
    (ref_root / "1小品-斯月学杨师版-25.11.25【已拆】-已训练1r").mkdir(parents=True)
    (gpt_root / "1小品-斯月学杨师版-2r-e10.ckpt").write_bytes(b"old")
    (gpt_root / "1小品-斯月学杨师版-2r-e50.ckpt").write_bytes(b"new")
    (sovits_root / "1小品-斯月学杨师版-2r_e4_s60.pth").write_bytes(b"old")
    (sovits_root / "1小品-斯月学杨师版-2r_e24_s360.pth").write_bytes(b"new")
    (ref_root / "1小品-斯月学杨师版-25.11.25【已拆】-已训练1r" / "ref.wav").write_bytes(b"wav")
    (ref_root / "1小品-斯月学杨师版-25.11.25【已拆】-已训练1r" / "ref.txt").write_text("参考文本", encoding="utf-8")

    candidates = scan_role_library_candidates(
        reference_audio_root=ref_root,
        gpt_weights_roots=[gpt_root],
        sovits_weights_roots=[sovits_root],
        limit=20,
    )

    assert candidates[0]["name"] == "小品"
    assert candidates[0]["recommended_gpt_weights_path"].endswith("e50.ckpt")
    assert candidates[0]["recommended_sovits_weights_path"].endswith("e24_s360.pth")
    sample = candidates[0]["reference_audio_groups"][0]["samples"][0]
    assert sample["text"] == "参考文本"
    assert sample["text_source"] == "sidecar"


def test_logs_candidates_preserve_full_logs_name_from_weight_files(tmp_path: Path) -> None:
    gpt_root = tmp_path / "GPT_weights_v2ProPlus"
    sovits_root = tmp_path / "SoVITS_weights_v2ProPlus"
    ref_root = tmp_path / "refs"
    gpt_root.mkdir()
    sovits_root.mkdir()
    ref_root.mkdir()
    (gpt_root / "demo-hero-logs-e50.ckpt").write_bytes(b"gpt")
    (sovits_root / "demo-hero-logs_e24_s264.pth").write_bytes(b"sovits")

    candidates = scan_logs_index_candidates(
        reference_audio_root=ref_root,
        gpt_weights_roots=[gpt_root],
        sovits_weights_roots=[sovits_root],
        service_id="local-gpt-sovits-proplus",
        limit=20,
    )

    by_name = {candidate["name"]: candidate for candidate in candidates}
    assert by_name["主角"]["logs_name"] == "demo-hero-logs"
    assert by_name["主角"]["id"] == "zhu-jue"
    assert candidate_to_character(by_name["主角"]).id == "zhu-jue"
    assert by_name["主角"]["recommended_gpt_weights_path"].endswith("demo-hero-logs-e50.ckpt")
    assert by_name["主角"]["recommended_sovits_weights_path"].endswith("demo-hero-logs_e24_s264.pth")
