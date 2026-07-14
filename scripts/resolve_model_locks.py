from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


MODELS = {
    "gpt-sovits": {
        "modelscope": "XXXXRT/GPT-SoVITS-Pretrained",
        "upstream": "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained",
        "license": "upstream-model-card",
        "required": lambda path: path.startswith("pretrained_models/") and not path.endswith(".gitignore"),
        "target": lambda path: f"GPT_SoVITS/{path}",
        "required_paths": [
            "GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin",
            "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin",
            "GPT_SoVITS/pretrained_models/s1v3.ckpt",
            "GPT_SoVITS/pretrained_models/s2Gv3.pth",
        ],
    },
    "indextts": {
        "modelscope": "IndexTeam/IndexTTS-2",
        "upstream": "https://huggingface.co/IndexTeam/IndexTTS-2",
        "license": "IndexTTS-2 model license; see checkpoints/LICENSE.txt",
        "required": lambda path: path != ".gitattributes" and path != "README.md",
        "target": lambda path: f"checkpoints/{path}",
        "required_paths": [
            "checkpoints/gpt.pth",
            "checkpoints/s2mel.pth",
            "checkpoints/hf_cache/semantic_codec_model.safetensors",
            "checkpoints/hf_cache/campplus_cn_common.bin",
            "checkpoints/hf_cache/bigvgan/config.json",
            "checkpoints/hf_cache/bigvgan/bigvgan_generator.pt",
            "checkpoints/hf_cache/w2v-bert-2.0/config.json",
        ],
    },
    "cosyvoice": {
        "modelscope": "iic/CosyVoice-300M",
        "upstream": "https://huggingface.co/FunAudioLLM/CosyVoice-300M",
        "license": "Apache-2.0",
        "required": lambda path: path
        in {
            "campplus.onnx",
            "configuration.json",
            "cosyvoice.yaml",
            "flow.pt",
            "hift.pt",
            "llm.pt",
            "speech_tokenizer_v1.onnx",
            "README.md",
        },
        "target": lambda path: f"pretrained_models/CosyVoice-300M/{path}",
        "required_paths": [
            "pretrained_models/CosyVoice-300M/cosyvoice.yaml",
            "pretrained_models/CosyVoice-300M/flow.pt",
            "pretrained_models/CosyVoice-300M/hift.pt",
            "pretrained_models/CosyVoice-300M/llm.pt",
            "pretrained_models/CosyVoice-300M/campplus.onnx",
            "pretrained_models/CosyVoice-300M/speech_tokenizer_v1.onnx",
        ],
    },
}


def resolve(component: str) -> dict[str, Any]:
    config = MODELS[component]
    model_id = str(config["modelscope"])
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in model_id.split("/"))
    api = f"https://www.modelscope.cn/api/v1/models/{encoded}/repo/files?Revision=master&Recursive=true"
    with urllib.request.urlopen(api, timeout=60) as response:
        files = json.load(response)["Data"]["Files"]
    assets = []
    for item in files:
        path = str(item.get("Path") or "")
        if item.get("Type") != "blob" or not config["required"](path):
            continue
        revision = str(item.get("Revision") or "")
        sha256 = str(item.get("Sha256") or "").lower()
        size = int(item.get("Size") or 0)
        if len(revision) != 40 or len(sha256) != 64 or size <= 0:
            raise RuntimeError(f"incomplete immutable metadata for {model_id}:{path}")
        url_path = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
        assets.append(
            {
                "id": hashlib.sha256(f"{component}\0{path}".encode()).hexdigest()[:24],
                "source_path": path,
                "source_revision": revision,
                "urls": [f"https://www.modelscope.cn/models/{model_id}/resolve/{revision}/{url_path}"],
                "sha256": sha256,
                "size_bytes": size,
                "target": config["target"](path),
            }
        )
    assets.sort(key=lambda asset: str(asset["target"]))
    material = "\n".join(
        f"{asset['source_path']}\0{asset['source_revision']}\0{asset['sha256']}\0{asset['size_bytes']}"
        for asset in assets
    )
    snapshot = hashlib.sha256(material.encode()).hexdigest()
    targets = {str(asset["target"]) for asset in assets}
    missing = [path for path in config["required_paths"] if path not in targets]
    return {
        "schema_version": 1,
        "component": component,
        "upstream_repository": config["upstream"],
        "resolved_via": f"https://www.modelscope.cn/models/{model_id}",
        "snapshot_revision": snapshot,
        "mutable_revisions_allowed": False,
        "fallback_policy": "hash-equivalent-assets-only",
        "license": config["license"],
        "required_free_bytes": sum(int(asset["size_bytes"]) for asset in assets) * 2,
        "complete": not missing,
        "missing_required_paths": missing,
        "required_paths": config["required_paths"],
        "assets": assets,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve mutable model listings into immutable per-file locks")
    parser.add_argument("--component", choices=[*MODELS, "all"], default="all")
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parents[1] / "integrations" / "components")
    args = parser.parse_args(argv)
    components = MODELS if args.component == "all" else [args.component]
    incomplete = []
    for component in components:
        payload = resolve(component)
        output = args.output_root / component / "models.lock.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{component}: {len(payload['assets'])} assets, snapshot {payload['snapshot_revision']}")
        if not payload["complete"]:
            incomplete.append(component)
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
