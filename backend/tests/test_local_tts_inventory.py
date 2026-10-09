"""Check operator-owned inventories without loading models or touching sources."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_inventory_keeps_complete_pairs_and_reports_both_orphan_weight_types(tmp_path):
    source = tmp_path / "native"
    for folder in ("GPT_weights_v2", "SoVITS_weights_v2", "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large", "GPT_SoVITS/pretrained_models/chinese-hubert-base"):
        (source / folder).mkdir(parents=True)
    for name in ("hero-e10.ckpt", "hero-e20.ckpt", "gpt-only-e5.ckpt"):
        (source / "GPT_weights_v2" / name).touch()
    for name in ("hero_e12_s120.pth", "sovits-only_e5_s50.pth"):
        (source / "SoVITS_weights_v2" / name).touch()
    logs = source / "logs" / "renamed-session"
    logs.mkdir(parents=True)
    (logs / "2-name2text.txt").write_text("annotated.wav\tspeaker\tzh\tReference text", encoding="utf-8")
    (logs / "notes.txt").write_text("This is not a training annotation", encoding="utf-8")
    output = tmp_path / "integration"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"version": 1, "integration_dir": str(output), "gpt_sovits_projects": [{"id": "native-one", "source_root": str(source), "python_executable": sys.executable, "logs_mapping": {"hero": str(logs)}}]}), encoding="utf-8")
    before = sorted(str(path.relative_to(source)) for path in source.rglob("*"))

    result = subprocess.run([sys.executable, str(ROOT / "scripts/inventory-local-tts.py"), "--config", str(config)], capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == {"registered": 1, "training_names": 3, "unpaired": 2}
    resources = json.loads((output / "resources.yaml").read_text(encoding="utf-8"))["resources"]
    resource = next(iter(resources.values()))
    assert Path(resource["gpt_weight"]).name == "hero-e20.ckpt"
    assert Path(resource["sovits_weight"]).name == "hero_e12_s120.pth"
    assert all(Path(value).is_absolute() for key, value in resource.items() if key not in {"engine", "version"})
    inventory = json.loads((output / "model-inventory.json").read_text(encoding="utf-8"))
    hero = next(item for item in inventory if item["name"] == "hero")
    assert hero["annotation_files"] == [str(logs / "2-name2text.txt")]
    assert sorted(str(path.relative_to(source)) for path in source.rglob("*")) == before


def test_relative_operator_paths_use_repository_root(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("local_tts_config_for_test", ROOT / "scripts/local_tts_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path / "workstation")
    assert module.source_path("../native/models") == tmp_path / "native" / "models"
    assert module.integration_folder({}) == tmp_path / "workstation" / "data/local/comfyui"
